"""Demo app views."""

from datetime import timedelta

from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from django_database_task.models import DatabaseTask

from . import tasks


def index(request):
    """Task list and enqueue form."""
    recent_tasks = DatabaseTask.objects.order_by("-created_at")[:20]
    return render(request, "demo_app/index.html", {"tasks": recent_tasks})


@require_POST
def enqueue_add(request):
    """Enqueue add numbers task."""
    x = int(request.POST.get("x", 0))
    y = int(request.POST.get("y", 0))
    result = tasks.add_numbers.enqueue(x, y)
    return redirect("result", task_id=result.id)


@require_POST
def enqueue_email(request):
    """Enqueue email task."""
    to = request.POST.get("to", "test@example.com")
    subject = request.POST.get("subject", "Test Subject")
    body = request.POST.get("body", "Test Body")
    result = tasks.send_email_task.enqueue(to, subject, body)
    return redirect("result", task_id=result.id)


@require_POST
def enqueue_process(request):
    """Enqueue data processing task."""
    data_size = int(request.POST.get("data_size", 100))
    result = tasks.process_data.enqueue(data_size)
    return redirect("result", task_id=result.id)


@require_POST
def enqueue_failing(request):
    """Enqueue failing task."""
    result = tasks.failing_task.enqueue()
    return redirect("result", task_id=result.id)


@require_POST
def enqueue_priority(request):
    """Enqueue priority test tasks."""
    # 3 low priority tasks
    tasks.low_priority_cleanup.enqueue()
    tasks.low_priority_cleanup.enqueue()
    tasks.low_priority_cleanup.enqueue()
    # 1 high priority task
    result = tasks.high_priority_report.enqueue("Priority Test Report")
    return redirect("result", task_id=result.id)


@require_POST
def enqueue_delayed(request):
    """Enqueue delayed task."""
    delay_seconds = int(request.POST.get("delay", 30))
    run_after = timezone.now() + timedelta(seconds=delay_seconds)
    delayed_task = tasks.add_numbers.using(run_after=run_after)
    result = delayed_task.enqueue(100, 200)
    return redirect("result", task_id=result.id)


@require_POST
def enqueue_context(request):
    """Enqueue context-aware task."""
    message = request.POST.get("message", "Hello from context task!")
    result = tasks.context_aware_task.enqueue(message)
    return redirect("result", task_id=result.id)


@require_POST
def enqueue_newsletter(request):
    """Enqueue newsletter task (uses 'emails' queue)."""
    subscriber_count = int(request.POST.get("subscriber_count", 100))
    result = tasks.newsletter_task.enqueue(subscriber_count)
    return redirect("result", task_id=result.id)


def result(request, task_id):
    """Display task result."""
    try:
        db_task = DatabaseTask.objects.get(id=task_id)
    except DatabaseTask.DoesNotExist:
        db_task = None
    return render(request, "demo_app/result.html", {"task": db_task})


def task_list_json(request):
    """Return all tasks status as JSON."""
    tasks_data = []
    for t in DatabaseTask.objects.order_by("-created_at")[:50]:
        tasks_data.append(
            {
                "id": str(t.id),
                "task_path": t.task_path,
                "status": t.status,
                "priority": t.priority,
                "queue_name": t.queue_name,
                "enqueued_at": t.enqueued_at.isoformat() if t.enqueued_at else None,
                "started_at": t.started_at.isoformat() if t.started_at else None,
                "finished_at": t.finished_at.isoformat() if t.finished_at else None,
                "return_value": t.return_value_json,
                "errors": t.errors_json,
            }
        )
    return JsonResponse({"tasks": tasks_data})
