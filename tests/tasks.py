"""Task definitions for testing."""

from django.tasks import task


@task
def simple_task(x, y):
    """Simple addition task."""
    return x + y


@task
def failing_task():
    """Task that always fails."""
    raise ValueError("This task always fails")


@task(priority=10)
def high_priority_task():
    """High priority task."""
    return "high priority"


@task(priority=-10)
def low_priority_task():
    """Low priority task."""
    return "low priority"


@task(queue_name="special")
def special_queue_task():
    """Task for special queue."""
    return "special queue"


@task(takes_context=True)
def context_task(context):
    """Task that receives context."""
    return f"task_id: {context.task_result.id}"


@task
def slow_task(seconds=1):
    """Task that takes time to complete."""
    import time

    time.sleep(seconds)
    return f"slept for {seconds} seconds"


@task
def dict_task(data):
    """Task that receives a dictionary."""
    return data


@task
async def async_task(x, y):
    """Async task for testing."""
    import asyncio

    await asyncio.sleep(0.01)
    return x + y


@task
async def async_failing_task():
    """Async task that always fails."""
    import asyncio

    await asyncio.sleep(0.01)
    raise ValueError("Async task failed")
