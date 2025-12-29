import asyncio
import traceback
from importlib import import_module
from inspect import iscoroutinefunction

from django.tasks.backends.base import BaseTaskBackend
from django.tasks.base import Task, TaskContext, TaskError, TaskResult, TaskResultStatus
from django.tasks.exceptions import TaskResultDoesNotExist
from django.tasks.signals import task_enqueued, task_finished, task_started
from django.utils import timezone
from django.utils.json import normalize_json


class DatabaseTaskBackend(BaseTaskBackend):
    """A task backend that persists tasks in the database."""

    supports_defer = True
    supports_async_task = True
    supports_get_result = True
    supports_priority = True

    def get_auth_handler(self):
        """
        Get the authentication handler for task execution endpoints.

        Subclasses can override this to provide custom authentication.
        The handler should be a callable that takes a request and returns:
        - None if authentication succeeds
        - A JsonResponse with error details if authentication fails

        Returns:
            Callable or None
        """
        return None

    def enqueue(self, task, args, kwargs):
        """Enqueue a task to the database.

        Args and kwargs must be JSON-serializable. Supported types are:
        - str, int, float, bool, None
        - dict (with JSON-serializable keys and values)
        - list, tuple (with JSON-serializable elements)
        - bytes (UTF-8 decodable)

        Raises:
            TypeError: If args or kwargs contain non-JSON-serializable types.
        """
        from .models import DatabaseTask

        self.validate_task(task)

        # Normalize args and kwargs to ensure JSON serialization
        # This will raise TypeError for unsupported types (e.g., datetime, UUID)
        normalized_args = normalize_json(list(args))
        normalized_kwargs = normalize_json(dict(kwargs))

        now = timezone.now()
        db_task = DatabaseTask.objects.create(
            task_path=self._get_task_path(task),
            queue_name=task.queue_name,
            priority=task.priority,
            args_json=normalized_args,
            kwargs_json=normalized_kwargs,
            status=TaskResultStatus.READY,
            run_after=task.run_after,
            enqueued_at=now,
            backend_name=self.alias,
        )

        task_result = self._db_task_to_result(db_task, task)
        task_enqueued.send(sender=self.__class__, task_result=task_result)

        return task_result

    def get_result(self, result_id):
        """Retrieve a task result from the database."""
        from .models import DatabaseTask

        try:
            db_task = DatabaseTask.objects.get(id=result_id)
        except DatabaseTask.DoesNotExist as e:
            raise TaskResultDoesNotExist(result_id) from e

        task = self._resolve_task(db_task.task_path)
        return self._db_task_to_result(db_task, task)

    def _get_task_path(self, task):
        """Get the module path of the task function."""
        func = task.func
        return f"{func.__module__}.{func.__qualname__}"

    def _resolve_task(self, task_path):
        """Resolve a Task object from its module path."""
        module_path, func_name = task_path.rsplit(".", 1)
        module = import_module(module_path)
        func = getattr(module, func_name)
        if isinstance(func, Task):
            return func
        return func

    def _db_task_to_result(self, db_task, task):
        """Convert a DatabaseTask model to a TaskResult."""
        errors = [
            TaskError(
                exception_class_path=e.get("exception_class_path", ""),
                traceback=e.get("traceback", ""),
            )
            for e in db_task.errors_json
        ]

        result = TaskResult(
            task=task if isinstance(task, Task) else task,
            id=str(db_task.id),
            status=TaskResultStatus(db_task.status),
            enqueued_at=db_task.enqueued_at,
            started_at=db_task.started_at,
            finished_at=db_task.finished_at,
            last_attempted_at=db_task.last_attempted_at,
            args=db_task.args_json,
            kwargs=db_task.kwargs_json,
            backend=db_task.backend_name,
            errors=errors,
            worker_ids=db_task.worker_ids_json,
        )

        if db_task.return_value_json is not None:
            object.__setattr__(result, "_return_value", db_task.return_value_json)

        return result

    def run_task(self, db_task, worker_id=None):
        """Execute a task (called from management command)."""

        now = timezone.now()

        # Update status to RUNNING
        worker_ids = db_task.worker_ids_json.copy()
        if worker_id:
            worker_ids.append(worker_id)

        db_task.status = TaskResultStatus.RUNNING
        db_task.started_at = db_task.started_at or now
        db_task.last_attempted_at = now
        db_task.worker_ids_json = worker_ids
        db_task.save(
            update_fields=[
                "status",
                "started_at",
                "last_attempted_at",
                "worker_ids_json",
                "updated_at",
            ]
        )

        task = self._resolve_task(db_task.task_path)
        task_result = self._db_task_to_result(db_task, task)
        task_started.send(sender=self.__class__, task_result=task_result)

        try:
            # Get task function
            if isinstance(task, Task):
                func = task.func
                takes_context = task.takes_context
            else:
                func = task
                takes_context = False

            # Prepare arguments
            args = db_task.args_json
            kwargs = db_task.kwargs_json.copy()

            # Execute task
            # If takes_context, pass TaskContext as first positional argument
            if takes_context:
                context = TaskContext(task_result=task_result)
                if iscoroutinefunction(func):
                    return_value = asyncio.run(func(context, *args, **kwargs))
                else:
                    return_value = func(context, *args, **kwargs)
            else:
                if iscoroutinefunction(func):
                    return_value = asyncio.run(func(*args, **kwargs))
                else:
                    return_value = func(*args, **kwargs)

            # Normalize return value for JSON serialization
            # This will raise TypeError for unsupported types
            normalized_return_value = normalize_json(return_value)

            # Success
            db_task.status = TaskResultStatus.SUCCESSFUL
            db_task.return_value_json = normalized_return_value
            db_task.finished_at = timezone.now()
            db_task.save(
                update_fields=[
                    "status",
                    "return_value_json",
                    "finished_at",
                    "updated_at",
                ]
            )

        except Exception as e:
            # Failure
            error = TaskError(
                exception_class_path=f"{type(e).__module__}.{type(e).__qualname__}",
                traceback=traceback.format_exc(),
            )
            errors = db_task.errors_json.copy()
            errors.append(
                {
                    "exception_class_path": error.exception_class_path,
                    "traceback": error.traceback,
                }
            )

            db_task.status = TaskResultStatus.FAILED
            db_task.errors_json = errors
            db_task.finished_at = timezone.now()
            db_task.save(
                update_fields=["status", "errors_json", "finished_at", "updated_at"]
            )

        # Get final result and send signal
        db_task.refresh_from_db()
        final_result = self._db_task_to_result(db_task, task)
        task_finished.send(sender=self.__class__, task_result=final_result)

        return final_result
