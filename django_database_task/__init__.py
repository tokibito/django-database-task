"""
django-database-task: A database-backed task queue backend for Django 6.0's task framework.
"""

__version__ = "0.2.2"


def __getattr__(name):
    """Lazy import to avoid AppRegistryNotReady errors."""
    if name in (
        "fetch_task",
        "get_pending_task_count",
        "process_one_task",
        "process_tasks",
        "run_task_by_id",
    ):
        from . import executor

        return getattr(executor, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "fetch_task",
    "get_pending_task_count",
    "process_one_task",
    "process_tasks",
    "run_task_by_id",
]
