"""
django-database-task: A database-backed task queue backend for Django's task framework.
"""

__version__ = "0.3.1"

_EXECUTOR_EXPORTS = (
    "fetch_task",
    "get_pending_task_count",
    "process_one_task",
    "process_tasks",
    "run_task_by_id",
)

_SHUTDOWN_EXPORTS = (
    "GracefulShutdown",
    "get_active_shutdown",
    "is_shutdown_requested",
)


def __getattr__(name):
    """Lazy import to avoid AppRegistryNotReady errors."""
    if name in _EXECUTOR_EXPORTS:
        from . import executor

        return getattr(executor, name)
    if name in _SHUTDOWN_EXPORTS:
        from . import shutdown

        return getattr(shutdown, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "GracefulShutdown",
    "fetch_task",
    "get_active_shutdown",
    "get_pending_task_count",
    "is_shutdown_requested",
    "process_one_task",
    "process_tasks",
    "run_task_by_id",
]
