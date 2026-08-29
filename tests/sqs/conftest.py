"""Shared helpers for the SQS tests."""

from unittest.mock import MagicMock


def make_task_result(
    task_id="3f2a9c11-0000-4000-8000-000000000000", queue_name="default", run_after=None
):
    """A stand-in for the TaskResult the backend hands to the broker."""
    result = MagicMock()
    result.id = task_id
    result.task.queue_name = queue_name
    result.task.run_after = run_after
    return result
