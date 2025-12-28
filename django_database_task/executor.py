"""
Public API for executing database tasks.

This module provides functions to process tasks stored in the database
without using management commands.

Example usage:
    from django_database_task import process_tasks, process_one_task

    # Process a single task
    result = process_one_task()

    # Process multiple tasks
    results = process_tasks(max_tasks=10)

    # Process tasks from a specific queue
    results = process_tasks(queue_name="emails", max_tasks=5)
"""

import socket
import uuid

from django.db import transaction
from django.db.models import Q
from django.tasks import task_backends
from django.tasks.base import TaskResultStatus
from django.utils import timezone

from .models import DatabaseTask


def _generate_worker_id():
    """Generate a unique worker ID."""
    return f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"


def fetch_task(queue_name=None, backend_name="default"):
    """
    Fetch and lock a single pending task with exclusive lock.

    This function uses SELECT FOR UPDATE SKIP LOCKED to safely
    fetch a task without conflicts in multi-worker environments.

    Args:
        queue_name: Optional queue name to filter tasks.
        backend_name: Backend name (default: "default").

    Returns:
        DatabaseTask instance if a task is available, None otherwise.
    """
    now = timezone.now()

    with transaction.atomic():
        queryset = DatabaseTask.objects.select_for_update(skip_locked=True).filter(
            status=TaskResultStatus.READY,
            backend_name=backend_name,
        )

        # run_after condition: NULL or before current time
        queryset = queryset.filter(Q(run_after__isnull=True) | Q(run_after__lte=now))

        if queue_name:
            queryset = queryset.filter(queue_name=queue_name)

        # Order by priority descending, enqueued_at ascending
        task = queryset.order_by("-priority", "enqueued_at").first()

        return task


def process_one_task(queue_name=None, backend_name="default", worker_id=None):
    """
    Fetch and execute a single pending task.

    Args:
        queue_name: Optional queue name to filter tasks.
        backend_name: Backend name (default: "default").
        worker_id: Optional worker ID. If not provided, one will be generated.

    Returns:
        TaskResult if a task was processed, None if no task was available.

    Example:
        >>> from django_database_task import process_one_task
        >>> result = process_one_task()
        >>> if result:
        ...     print(f"Processed: {result.id}, status: {result.status}")
        ... else:
        ...     print("No tasks available")
    """
    if worker_id is None:
        worker_id = _generate_worker_id()

    task = fetch_task(queue_name=queue_name, backend_name=backend_name)

    if task is None:
        return None

    backend = task_backends[backend_name]
    return backend.run_task(task, worker_id=worker_id)


def process_tasks(
    queue_name=None,
    backend_name="default",
    max_tasks=0,
    worker_id=None,
):
    """
    Process multiple pending tasks.

    Args:
        queue_name: Optional queue name to filter tasks.
        backend_name: Backend name (default: "default").
        max_tasks: Maximum number of tasks to process (0 = unlimited).
        worker_id: Optional worker ID. If not provided, one will be generated.

    Returns:
        List of TaskResult objects for all processed tasks.

    Example:
        >>> from django_database_task import process_tasks
        >>> results = process_tasks(max_tasks=10)
        >>> print(f"Processed {len(results)} tasks")
        >>> for result in results:
        ...     print(f"  {result.id}: {result.status}")
    """
    if worker_id is None:
        worker_id = _generate_worker_id()

    results = []
    tasks_processed = 0

    while True:
        result = process_one_task(
            queue_name=queue_name,
            backend_name=backend_name,
            worker_id=worker_id,
        )

        if result is None:
            break

        results.append(result)
        tasks_processed += 1

        if max_tasks and tasks_processed >= max_tasks:
            break

    return results


def get_pending_task_count(queue_name=None, backend_name="default"):
    """
    Get the count of pending tasks.

    Args:
        queue_name: Optional queue name to filter tasks.
        backend_name: Backend name (default: "default").

    Returns:
        Number of pending tasks.

    Example:
        >>> from django_database_task import get_pending_task_count
        >>> count = get_pending_task_count()
        >>> print(f"Pending tasks: {count}")
    """
    now = timezone.now()

    queryset = DatabaseTask.objects.filter(
        status=TaskResultStatus.READY,
        backend_name=backend_name,
    )

    queryset = queryset.filter(Q(run_after__isnull=True) | Q(run_after__lte=now))

    if queue_name:
        queryset = queryset.filter(queue_name=queue_name)

    return queryset.count()


def run_task_by_id(task_id, worker_id=None, allow_retry=False):
    """
    Execute a specific task by its ID.

    This function is designed for external trigger systems (e.g., Cloud Tasks,
    webhooks) that need to execute a specific task by ID rather than fetching
    the next available task.

    By default, only tasks in READY status can be executed. Use allow_retry=True
    to also execute FAILED tasks (useful for Cloud Tasks retry mechanism).

    Args:
        task_id: UUID or string ID of the task to execute.
        worker_id: Optional worker ID. If not provided, one will be generated.
        allow_retry: If True, also allow execution of FAILED tasks.
                     The task will be reset to READY before execution.

    Returns:
        TaskResult if the task was executed, None if the task was not found
        or not in an executable status.

    Raises:
        DatabaseTask.DoesNotExist: If no task with the given ID exists.

    Example:
        >>> from django_database_task import run_task_by_id
        >>> result = run_task_by_id("550e8400-e29b-41d4-a716-446655440000")
        >>> if result:
        ...     print(f"Executed: {result.id}, status: {result.status}")
        ... else:
        ...     print("Task not in executable status")

        # Retry a failed task
        >>> result = run_task_by_id("...", allow_retry=True)
    """
    if worker_id is None:
        worker_id = _generate_worker_id()

    allowed_statuses = [TaskResultStatus.READY]
    if allow_retry:
        allowed_statuses.append(TaskResultStatus.FAILED)

    with transaction.atomic():
        # Use select_for_update to ensure exclusive access
        try:
            task = DatabaseTask.objects.select_for_update(skip_locked=True).get(
                id=task_id,
                status__in=allowed_statuses,
            )
        except DatabaseTask.DoesNotExist as e:
            # Task doesn't exist or is not in allowed status
            # Check if task exists at all for better error handling
            if not DatabaseTask.objects.filter(id=task_id).exists():
                raise DatabaseTask.DoesNotExist(
                    f"DatabaseTask with id={task_id} does not exist"
                ) from e
            # Task exists but is not in allowed status
            return None

        # Reset FAILED task to READY for retry
        if task.status == TaskResultStatus.FAILED:
            task.status = TaskResultStatus.READY
            task.finished_at = None
            task.save(update_fields=["status", "finished_at", "updated_at"])

    backend = task_backends[task.backend_name]
    return backend.run_task(task, worker_id=worker_id)
