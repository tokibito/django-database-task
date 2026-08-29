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

import logging
import socket
import uuid

from django.db import transaction
from django.db.models import Q
from django.tasks import task_backends
from django.tasks.base import TaskResultStatus
from django.utils import timezone

from .exceptions import WorkerLost
from .models import DatabaseTask

logger = logging.getLogger("django_database_task")

#: How many times a task may be handed to a worker before
#: :func:`requeue_stale_tasks` stops queueing it again. A task that kills the
#: worker running it (by exhausting its memory, say) would otherwise be
#: requeued forever, killing one worker after another.
DEFAULT_MAX_ATTEMPTS = 3

#: Recorded against a task whose worker never came back.
WORKER_LOST_PATH = f"{WorkerLost.__module__}.{WorkerLost.__qualname__}"


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
    stop_event=None,
):
    """
    Process multiple pending tasks.

    Args:
        queue_name: Optional queue name to filter tasks.
        backend_name: Backend name (default: "default").
        max_tasks: Maximum number of tasks to process (0 = unlimited).
        worker_id: Optional worker ID. If not provided, one will be generated.
        stop_event: Optional object with an ``is_set()`` method (for example a
            :class:`threading.Event` or a
            :class:`~django_database_task.GracefulShutdown`). No new task is
            started once it is set; the task already running is not
            interrupted.

    Returns:
        List of TaskResult objects for all processed tasks.

    Example:
        >>> from django_database_task import process_tasks
        >>> results = process_tasks(max_tasks=10)
        >>> print(f"Processed {len(results)} tasks")
        >>> for result in results:
        ...     print(f"  {result.id}: {result.status}")

        # Stop starting new tasks on SIGTERM/SIGINT
        >>> from django_database_task import GracefulShutdown
        >>> with GracefulShutdown() as shutdown:
        ...     results = process_tasks(stop_event=shutdown)
    """
    if worker_id is None:
        worker_id = _generate_worker_id()

    results = []
    tasks_processed = 0

    while True:
        if stop_event is not None and stop_event.is_set():
            break

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


def requeue_stale_tasks(
    older_than,
    queue_name=None,
    backend_name=None,
    max_attempts=DEFAULT_MAX_ATTEMPTS,
    mark_failed=False,
    notify_broker=False,
    batch_size=1000,
    dry_run=False,
):
    """
    Recover tasks left in RUNNING status by a worker that died.

    A task is set to RUNNING before it runs and only leaves that status when
    the worker writes its result. A worker killed outright (SIGKILL, the OOM
    killer, node failure) never gets to, so the task stays RUNNING and no
    other worker picks it up. This function finds those tasks and puts them
    back in READY, so a worker runs them again.

    A task is only requeued when it is safe to run twice: the work already
    done by the killed attempt is not undone. Use mark_failed for tasks that
    are not idempotent.

    Args:
        older_than: :class:`datetime.timedelta`. Only tasks that have been
            RUNNING for longer than this are touched. Keep it comfortably
            above the longest a task takes to run - a task still running when
            its threshold passes is requeued and ends up running twice.
        queue_name: Optional queue name to filter tasks.
        backend_name: Optional backend name to filter tasks. All backends by
            default.
        max_attempts: Give up on a task handed to this many workers already
            and mark it FAILED instead of requeueing it (0 = no limit).
        mark_failed: Mark every stale task FAILED instead of requeueing it.
        notify_broker: Tell the backend's broker about each requeued task.
            The message the broker delivered for the killed attempt is gone,
            so a worker that only receives from the broker needs this.
        batch_size: Number of tasks to process at a time.
        dry_run: Count what would happen without changing anything.

    Returns:
        dict with the counts: ``{"found": int, "requeued": int, "failed": int}``

    Example:
        >>> from datetime import timedelta
        >>> from django_database_task import requeue_stale_tasks
        >>> requeue_stale_tasks(timedelta(minutes=15))
        {'found': 2, 'requeued': 2, 'failed': 0}
    """
    cutoff = timezone.now() - older_than

    queryset = DatabaseTask.objects.filter(
        status=TaskResultStatus.RUNNING,
        last_attempted_at__lt=cutoff,
    )

    if queue_name:
        queryset = queryset.filter(queue_name=queue_name)
    if backend_name:
        queryset = queryset.filter(backend_name=backend_name)

    queryset = queryset.order_by("last_attempted_at")

    summary = {"found": 0, "requeued": 0, "failed": 0}

    if dry_run:
        for db_task in queryset.iterator(chunk_size=batch_size):
            summary["found"] += 1
            key = (
                "failed"
                if _is_given_up_on(db_task, max_attempts, mark_failed)
                else "requeued"
            )
            summary[key] += 1
        return summary

    while True:
        requeued_ids = []

        with transaction.atomic():
            # Rows another recovery run is already holding are skipped
            # rather than waited for; whoever holds them is doing the work.
            batch = list(queryset.select_for_update(skip_locked=True)[:batch_size])
            if not batch:
                break

            for db_task in batch:
                summary["found"] += 1
                if _is_given_up_on(db_task, max_attempts, mark_failed):
                    if _give_up_on_task(db_task, cutoff):
                        summary["failed"] += 1
                elif _requeue_task(db_task):
                    summary["requeued"] += 1
                    requeued_ids.append(db_task.id)

        # Notified once the requeue is committed, so a worker the broker
        # wakes up finds the task in READY rather than still RUNNING.
        if notify_broker:
            for task_id in requeued_ids:
                _notify_broker_of_requeue(task_id)

    return summary


def _is_given_up_on(db_task, max_attempts, mark_failed):
    """Whether a stale task is marked FAILED rather than queued again."""
    if mark_failed:
        return True
    if not max_attempts:
        return False
    return len(db_task.worker_ids_json or []) >= max_attempts


def _requeue_task(db_task):
    """
    Put a stale task back in READY.

    ``worker_ids_json`` is deliberately kept: it records which workers the
    task has been handed to, which is what max_attempts counts.

    Returns:
        True if the task was requeued, False if it was no longer RUNNING.
    """
    return bool(
        DatabaseTask.objects.filter(
            id=db_task.id,
            # Re-checked in the UPDATE itself: on a database without row
            # locking the worker may have come back to life and written its
            # result between the SELECT above and here.
            status=TaskResultStatus.RUNNING,
        ).update(
            status=TaskResultStatus.READY,
            started_at=None,
            finished_at=None,
            return_value_json=None,
            updated_at=timezone.now(),
        )
    )


def _give_up_on_task(db_task, cutoff):
    """
    Mark a stale task FAILED, recording that its worker was lost.

    Returns:
        True if the task was marked FAILED, False if it was no longer RUNNING.
    """
    errors = list(db_task.errors_json or [])
    errors.append(
        {
            "exception_class_path": WORKER_LOST_PATH,
            "traceback": _worker_lost_traceback(db_task, cutoff),
        }
    )

    return bool(
        DatabaseTask.objects.filter(
            id=db_task.id,
            status=TaskResultStatus.RUNNING,
        ).update(
            status=TaskResultStatus.FAILED,
            errors_json=errors,
            # Without this the task is invisible to
            # purge_completed_database_tasks --days.
            finished_at=timezone.now(),
            updated_at=timezone.now(),
        )
    )


def _worker_lost_traceback(db_task, cutoff):
    """Describe a lost worker in the place a traceback would go."""
    worker_ids = db_task.worker_ids_json or []
    worker = worker_ids[-1] if worker_ids else "an unknown worker"
    return (
        f"{WORKER_LOST_PATH}: {db_task.task_path} was handed to {worker} at "
        f"{db_task.last_attempted_at.isoformat()} and was still RUNNING at "
        f"{cutoff.isoformat()}, so the worker is presumed dead. "
        f"Attempts so far: {len(worker_ids)}. No exception was raised by the "
        f"task itself."
    )


def _notify_broker_of_requeue(task_id):
    """
    Tell the backend's broker about a task that was just requeued.

    A failure is logged and swallowed, matching how a broker failure is
    handled on enqueue: the task is READY in the database, so a worker
    polling it still picks it up.
    """
    try:
        db_task = DatabaseTask.objects.get(id=task_id)
        backend = task_backends[db_task.backend_name]
        backend.notify_broker(backend.get_result(str(task_id)))
    except Exception:
        logger.exception("Could not notify the broker of requeued task %s", task_id)
