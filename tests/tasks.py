"""テスト用タスク定義"""

from django.tasks import task


@task()
def simple_task(x, y):
    """単純な加算タスク"""
    return x + y


@task()
def failing_task():
    """常に失敗するタスク"""
    raise ValueError("This task always fails")


@task(priority=10)
def high_priority_task():
    """高優先度タスク"""
    return "high priority"


@task(priority=-10)
def low_priority_task():
    """低優先度タスク"""
    return "low priority"


@task(queue_name="special")
def special_queue_task():
    """特別なキューのタスク"""
    return "special queue"


@task(takes_context=True)
def context_task(context):
    """コンテキストを受け取るタスク"""
    return f"task_id: {context.task_result.id}"


@task()
def slow_task(seconds=1):
    """時間のかかるタスク"""
    import time

    time.sleep(seconds)
    return f"slept for {seconds} seconds"
