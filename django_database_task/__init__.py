"""
django-database-task: A database-backed task queue backend for Django 6.0's task framework.
"""

__version__ = "0.1.0"


def __getattr__(name):
    """Lazy import to avoid AppRegistryNotReady errors."""
    if name in (
        "fetch_task",
        "get_pending_task_count",
        "process_one_task",
        "process_tasks",
    ):
        from . import executor

        return getattr(executor, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "fetch_task",
    "get_pending_task_count",
    "process_one_task",
    "process_tasks",
]
