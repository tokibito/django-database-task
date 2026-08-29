"""Exceptions used by django-database-task."""


class DatabaseTaskError(Exception):
    """Base class for the errors this library reports about a task."""


class WorkerLost(DatabaseTaskError):
    """
    The worker that took a task disappeared without writing a result.

    A task is left in ``RUNNING`` status when the worker running it is killed
    outright (SIGKILL, OOM killer, node failure), because no process is left
    to record the outcome. ``requeue_stale_tasks()`` records this class
    against such a task when it gives up on it instead of queueing it again.

    It is never raised: the task did not fail on its own, so there is no
    traceback to capture. It exists to give the recorded error a name that
    can be recognised and filtered.
    """
