import asyncio
import logging
import time
import traceback
from functools import cached_property
from importlib import import_module
from inspect import iscoroutinefunction

from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.db.models import Count, Max, Min, Q
from django.db.models.functions import Coalesce, Greatest
from django.tasks.backends.base import BaseTaskBackend
from django.tasks.base import Task, TaskContext, TaskError, TaskResult, TaskResultStatus
from django.tasks.exceptions import TaskResultDoesNotExist
from django.tasks.signals import task_enqueued, task_finished, task_started
from django.utils import timezone
from django.utils.json import normalize_json
from django.utils.module_loading import import_string

from django_database_task.supervisor import worker_log_fields

logger = logging.getLogger("django_database_task")


def task_log_fields(db_task, worker_id=None, **extra):
    """
    Build the ``extra`` mapping attached to a task's log records.

    These are the fields an operator filters on once the records go through
    a structured (JSON) formatter, so they are kept flat and named apart
    from LogRecord's own attributes. ``pid`` and, under ``--workers``,
    ``worker_index`` join them with the supervisor's records.
    """
    fields = {
        "task_id": str(db_task.id),
        "task_path": db_task.task_path,
        "queue_name": db_task.queue_name,
        "priority": db_task.priority,
        "backend_alias": db_task.backend_name,
        "worker_id": worker_id,
        **worker_log_fields(),
    }
    fields.update(extra)
    return fields


def _elapsed_ms(started_monotonic):
    """Milliseconds since a time.monotonic() reading, rounded to the ms."""
    return round((time.monotonic() - started_monotonic) * 1000)


class DatabaseTaskBackend(BaseTaskBackend):
    """
    A task backend that persists tasks in the database.

    A broker can be attached to notify an external service (Cloud Tasks,
    for example) whenever a task is saved. Subclasses name theirs with
    broker_class; projects can also name one with the BROKER option:

        TASKS = {
            "default": {
                "BACKEND": "django_database_task.backends.DatabaseTaskBackend",
                "OPTIONS": {"BROKER": "myproject.brokers.MyBroker"},
            },
        }

    Without a broker the tasks are only picked up by run_database_tasks or
    the HTTP endpoints, which is the default.
    """

    supports_defer = True
    supports_async_task = True
    supports_get_result = True
    supports_priority = True

    # Dotted path to the broker class, or the class itself. The BROKER
    # option takes precedence over it.
    broker_class = None

    def __init__(self, alias, params):
        super().__init__(alias, params)
        # Built eagerly so a misconfigured broker is reported when the
        # backend is set up rather than on the first enqueue.
        self.broker = self.create_broker()

    def create_broker(self):
        """
        Build the broker this backend notifies, or None for no broker.

        Returns:
            TaskBroker or None
        """
        broker_class = self.options.get("BROKER", self.broker_class)
        if broker_class is None:
            return None

        if isinstance(broker_class, str):
            try:
                broker_class = import_string(broker_class)
            except ImportError as e:
                raise ImproperlyConfigured(
                    f"Could not import task broker {broker_class!r}: {e}"
                ) from e

        return broker_class(self, self.options)

    def notify_broker(self, task_result):
        """
        Tell the broker about a task that was just saved.

        A broker failure is logged and swallowed: the task is in the
        database, so run_database_tasks and the HTTP endpoints can still
        pick it up. That makes them the fallback when the broker is down.
        """
        if self.broker is None:
            return

        try:
            self.broker.notify(task_result)
        except Exception:
            logger.exception(
                "Broker %s failed to notify about task %s",
                type(self.broker).__name__,
                task_result.id,
            )

    def get_auth_handlers(self, endpoint=None):
        """
        Get the authentication handlers for the task HTTP endpoints.

        A request is accepted as soon as one handler accepts it, so a backend
        can let both the service that calls the endpoints (Cloud Tasks, for
        example) and an external cron job in, each with its own credentials.

        Each handler is a callable that takes a request and returns:
        - None if authentication succeeds
        - A JsonResponse with error details if authentication fails

        An empty list means the endpoints are not authenticated.

        Args:
            endpoint: Name of the endpoint being called ("run", "run_one",
                "status", "execute" or "purge"), or None to get every handler
                regardless of the endpoint it applies to.

        Returns:
            list of callables
        """
        handlers = list(self.get_broker_auth_handlers(endpoint))
        handlers.extend(self.get_configured_auth_handlers(endpoint))
        return handlers

    def get_broker_auth_handlers(self, endpoint=None):
        """
        Get the handlers that authenticate the service calling the endpoints.

        These come from the broker, which knows how the service it talks to
        signs its requests.

        Returns:
            list of callables
        """
        if self.broker is None:
            return []

        return list(self.broker.get_auth_handlers(endpoint) or [])

    def get_configured_auth_handlers(self, endpoint=None):
        """
        Get the handlers built from the AUTH_HANDLERS backend option.

        Returns:
            list of callables
        """
        return [
            handler
            for handler, endpoints in self._auth_handler_specs
            if endpoint is None or endpoints is None or endpoint in endpoints
        ]

    @cached_property
    def _auth_handler_specs(self):
        """Load AUTH_HANDLERS once per backend instance."""
        from .auth import load_auth_handlers

        return load_auth_handlers(
            self.options.get("AUTH_HANDLERS"),
            self.options.get("AUTH_HANDLER_OPTIONS"),
        )

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
        self.notify_broker(task_result)

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

    def _stored_tasks(self, queue_name=None):
        """The stored tasks of this backend, optionally of one queue."""
        from .models import DatabaseTask

        queryset = DatabaseTask.objects.filter(backend_name=self.alias)
        if queue_name:
            queryset = queryset.filter(queue_name=queue_name)
        return queryset

    def get_status_counts(self, queue_name=None):
        """
        Get task counts by status.

        Args:
            queue_name: Optional queue name filter.

        Returns:
            Dict mapping each status to the number of tasks in it. READY
            includes the delayed tasks whose ``run_after`` has not come.
        """
        return self._stored_tasks(queue_name).aggregate(
            **{
                status.value: Count("pk", filter=Q(status=status))
                for status in TaskResultStatus
            }
        )

    def get_queue_stats(self, queue_name=None):
        """
        Get queue statistics for a dashboard or an alert.

        The same keys as the ``get_queue_stats()`` of django-tasks-redis, read
        in one aggregate query.

        Args:
            queue_name: Optional queue name filter.

        Returns:
            Dict with the counts per status (``pending_count``,
            ``running_count``, ``successful_count``, ``failed_count``), the
            number of delayed tasks not yet due (``delayed_count``), and the
            time the oldest and newest pending task started waiting
            (``oldest_pending_waiting_since``, ``newest_pending_waiting_since``):
            ``max(enqueued_at, run_after)``, None when there is none.

            A pending task is one a worker would pick up now: a READY task
            whose ``run_after`` is unset or has passed, as counted by
            ``get_pending_task_count()``. A READY task whose ``run_after``
            lies in the future is counted in ``delayed_count`` instead, so
            the two add up to the READY count of :meth:`get_status_counts`.
        """
        now = timezone.now()
        due = Q(run_after__isnull=True) | Q(run_after__lte=now)
        pending = Q(status=TaskResultStatus.READY) & due
        # A task delayed with run_after starts waiting when it becomes due,
        # not when it was enqueued.
        waiting_since = Greatest("enqueued_at", Coalesce("run_after", "enqueued_at"))

        return self._stored_tasks(queue_name).aggregate(
            pending_count=Count("pk", filter=pending),
            running_count=Count("pk", filter=Q(status=TaskResultStatus.RUNNING)),
            successful_count=Count("pk", filter=Q(status=TaskResultStatus.SUCCESSFUL)),
            failed_count=Count("pk", filter=Q(status=TaskResultStatus.FAILED)),
            delayed_count=Count(
                "pk",
                filter=Q(status=TaskResultStatus.READY, run_after__gt=now),
            ),
            oldest_pending_waiting_since=Min(waiting_since, filter=pending),
            newest_pending_waiting_since=Max(waiting_since, filter=pending),
        )

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

    def claim_task(self, db_task, worker_id=None):
        """
        Move a READY task to RUNNING and record the attempt on it.

        The first of the two steps of :meth:`run_task`; the second is
        :meth:`run_claimed_task`. A worker that fetches tasks in one thread
        and runs them in another claims here, in the fetching thread, so
        that its next fetch does not hand it the same READY row again.

        The row is locked and its status re-checked inside one transaction,
        so of two workers holding the same READY row exactly one gets to
        write RUNNING. The worker id is appended to the list stored in the
        database rather than to the copy on ``db_task``, so an attempt
        recorded after ``db_task`` was loaded is kept.

        Args:
            db_task: The :class:`~django_database_task.models.DatabaseTask`
                to claim. It must be READY.
            worker_id: Optional worker identifier, recorded on the task.

        Returns:
            True if this call claimed the task, and ``db_task`` carries what
            was written. False if it was no longer READY (another worker
            claimed it, or it was deleted); nothing was written and the task
            must not be run.
        """
        return self._claim_task(db_task, worker_id)

    def run_task(self, db_task, worker_id=None):
        """
        Claim a task and execute it (called from the executor and the
        management command).

        The task is claimed with :meth:`claim_task` first, so two workers
        that fetched the same READY row cannot both run it: whichever claims
        second gets None and runs nothing. The run itself is
        :meth:`run_claimed_task`; the two are separate so that a worker can
        claim in one thread and run in another.

        A task whose function cannot be imported (the module was renamed,
        the function removed, the worker runs older code than the enqueuer)
        is recorded as FAILED with the traceback, the same as a task that
        raised, so it does not sit in RUNNING with nothing to explain it. The
        ``task_started`` and ``task_finished`` signals are not sent for it,
        since there is no task object to send them with; the ERROR record
        ``Task could not be started`` reports it instead.

        Args:
            db_task: The :class:`~django_database_task.models.DatabaseTask`
                to run. It must be READY.
            worker_id: Optional worker identifier, recorded on the task.

        Returns:
            TaskResult after execution, or None if the task was no longer
            READY (another worker claimed it, or it was deleted) and so was
            not run here. The result of a task that could not be started has
            ``task`` set to None.
        """
        if not self.claim_task(db_task, worker_id):
            logger.info(
                "Task not run: id=%s path=%s is no longer READY",
                db_task.id,
                db_task.task_path,
                extra=task_log_fields(db_task, worker_id),
            )
            return None
        return self.run_claimed_task(db_task, worker_id)

    def run_claimed_task(self, db_task, worker_id=None):
        """
        Execute a task that :meth:`claim_task` has moved to RUNNING.

        The second of the two steps of :meth:`run_task`, which documents
        what is recorded, logged and signalled. ``db_task`` must be the
        instance the claim was made with, as the claim wrote the attempt
        onto it, and ``worker_id`` the one it was claimed with.

        Returns:
            TaskResult after execution. The result of a task that could not
            be started has ``task`` set to None.
        """
        # Past the RUNNING write but before the block that records failures,
        # so an error here would otherwise leave the task RUNNING with no
        # error, and no other worker would take it.
        task = None
        try:
            task = self._resolve_task(db_task.task_path)
            task_result = self._db_task_to_result(db_task, task)
            logger.info(
                "Task started: id=%s path=%s",
                db_task.id,
                db_task.task_path,
                extra=task_log_fields(db_task, worker_id),
            )
            task_started.send(sender=self.__class__, task_result=task_result)
        except Exception as e:
            error = self._record_error(db_task, e)
            db_task.refresh_from_db()
            logger.exception(
                "Task could not be started: id=%s path=%s error=%s",
                db_task.id,
                db_task.task_path,
                error.exception_class_path,
                extra=task_log_fields(
                    db_task,
                    worker_id,
                    status=str(TaskResultStatus.FAILED),
                    error_class=error.exception_class_path,
                ),
            )
            return self._db_task_to_result(db_task, task)

        # Wall time of the run itself, kept apart from started_at/finished_at
        # because those are database timestamps and can be rewritten.
        started_monotonic = time.monotonic()

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

            # Send signal for success
            db_task.refresh_from_db()
            final_result = self._db_task_to_result(db_task, task)
            # Note: Django's task_finished handler logs "NoneType: None" after this
            # due to exc_info=sys.exc_info() being called outside exception context.
            # This is a known Django issue and will be fixed upstream.
            logger.info(
                "Task completed successfully: id=%s path=%s",
                final_result.id,
                db_task.task_path,
                extra=task_log_fields(
                    db_task,
                    worker_id,
                    status=str(TaskResultStatus.SUCCESSFUL),
                    duration_ms=_elapsed_ms(started_monotonic),
                ),
            )
            task_finished.send(sender=self.__class__, task_result=final_result)
            return final_result

        except Exception as e:
            error = self._record_error(db_task, e)

            # Send signal for failure (with exception context for Django's logging)
            db_task.refresh_from_db()
            final_result = self._db_task_to_result(db_task, task)
            logger.error(
                "Task failed: id=%s path=%s error=%s",
                final_result.id,
                db_task.task_path,
                error.exception_class_path,
                extra=task_log_fields(
                    db_task,
                    worker_id,
                    status=str(TaskResultStatus.FAILED),
                    duration_ms=_elapsed_ms(started_monotonic),
                    error_class=error.exception_class_path,
                ),
            )
            task_finished.send(sender=self.__class__, task_result=final_result)
            return final_result

    def _record_error(self, db_task, exc):
        """
        Mark ``db_task`` FAILED with ``exc`` appended to its errors.

        Must be called from the ``except`` block handling ``exc``, so the
        traceback recorded is the one being handled.

        Returns:
            TaskError describing ``exc``, as stored on the task.
        """
        error = TaskError(
            exception_class_path=f"{type(exc).__module__}.{type(exc).__qualname__}",
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
        return error

    def _claim_task(self, db_task, worker_id):
        """The claim behind :meth:`claim_task`, which documents it."""
        from .models import DatabaseTask

        now = timezone.now()

        with transaction.atomic():
            stored = (
                DatabaseTask.objects.select_for_update(skip_locked=True)
                .filter(id=db_task.id, status=TaskResultStatus.READY)
                .first()
            )
            if stored is None:
                return False

            worker_ids = list(stored.worker_ids_json)
            if worker_id:
                worker_ids.append(worker_id)
            started_at = stored.started_at or now

            claimed = DatabaseTask.objects.filter(
                id=db_task.id,
                # Re-checked in the UPDATE itself: on a database without row
                # locking another worker may have claimed the task between
                # the SELECT above and here.
                status=TaskResultStatus.READY,
            ).update(
                status=TaskResultStatus.RUNNING,
                started_at=started_at,
                last_attempted_at=now,
                worker_ids_json=worker_ids,
                updated_at=now,
            )

        if not claimed:
            return False

        db_task.status = TaskResultStatus.RUNNING
        db_task.started_at = started_at
        db_task.last_attempted_at = now
        db_task.worker_ids_json = worker_ids
        db_task.updated_at = now
        return True
