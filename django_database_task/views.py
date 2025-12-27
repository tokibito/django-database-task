"""
HTTP endpoints for task execution.

These views provide an alternative way to trigger task processing
when cron or direct command execution is not available.

Usage:
    # In your project's urls.py
    from django.urls import path, include

    urlpatterns = [
        path("tasks/", include("django_database_task.urls")),
    ]

    # Then POST to /tasks/run/ to process tasks
"""

import json

from django.http import JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from .executor import get_pending_task_count, process_one_task, process_tasks


@method_decorator(csrf_exempt, name="dispatch")
class RunTasksView(View):
    """
    Process pending tasks via HTTP POST.

    This view is useful when you need to trigger task processing
    from external systems (e.g., cloud schedulers, webhooks) that
    cannot execute management commands directly.

    POST parameters (JSON body):
        max_tasks: Maximum number of tasks to process (default: 10)
        queue_name: Optional queue name to filter tasks
        backend_name: Backend name (default: "default")

    Response:
        {
            "processed": 3,
            "results": [
                {"id": "...", "status": "SUCCESSFUL", "task_path": "..."},
                ...
            ]
        }

    Security:
        - Only accepts POST requests
        - CSRF exempt (intended for API/webhook use)
        - Consider adding authentication in your URL configuration:

            from django.contrib.admin.views.decorators import staff_member_required

            urlpatterns = [
                path(
                    "tasks/run/",
                    staff_member_required(RunTasksView.as_view()),
                ),
            ]
    """

    http_method_names = ["post"]

    def post(self, request):
        # Parse JSON body if present
        try:
            if request.body:
                data = json.loads(request.body)
            else:
                data = {}
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)

        max_tasks = data.get("max_tasks", 10)
        queue_name = data.get("queue_name")
        backend_name = data.get("backend_name", "default")

        # Validate max_tasks
        if not isinstance(max_tasks, int) or max_tasks < 1:
            return JsonResponse(
                {"error": "max_tasks must be a positive integer"}, status=400
            )
        if max_tasks > 100:
            return JsonResponse({"error": "max_tasks cannot exceed 100"}, status=400)

        results = process_tasks(
            queue_name=queue_name,
            backend_name=backend_name,
            max_tasks=max_tasks,
        )

        return JsonResponse(
            {
                "processed": len(results),
                "results": [
                    {
                        "id": str(r.id),
                        "status": r.status.value,
                        "task_path": r.task.func.__module__
                        + "."
                        + r.task.func.__qualname__
                        if hasattr(r.task, "func")
                        else str(r.task),
                    }
                    for r in results
                ],
            }
        )


@method_decorator(csrf_exempt, name="dispatch")
class RunOneTaskView(View):
    """
    Process a single pending task via HTTP POST.

    POST parameters (JSON body):
        queue_name: Optional queue name to filter tasks
        backend_name: Backend name (default: "default")

    Response (task processed):
        {
            "processed": true,
            "result": {
                "id": "...",
                "status": "SUCCESSFUL",
                "task_path": "..."
            }
        }

    Response (no task available):
        {
            "processed": false,
            "result": null
        }
    """

    http_method_names = ["post"]

    def post(self, request):
        # Parse JSON body if present
        try:
            if request.body:
                data = json.loads(request.body)
            else:
                data = {}
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)

        queue_name = data.get("queue_name")
        backend_name = data.get("backend_name", "default")

        result = process_one_task(
            queue_name=queue_name,
            backend_name=backend_name,
        )

        if result is None:
            return JsonResponse({"processed": False, "result": None})

        return JsonResponse(
            {
                "processed": True,
                "result": {
                    "id": str(result.id),
                    "status": result.status.value,
                    "task_path": result.task.func.__module__
                    + "."
                    + result.task.func.__qualname__
                    if hasattr(result.task, "func")
                    else str(result.task),
                },
            }
        )


class TaskStatusView(View):
    """
    Get pending task count via HTTP GET.

    Query parameters:
        queue_name: Optional queue name to filter tasks
        backend_name: Backend name (default: "default")

    Response:
        {
            "pending_count": 5
        }
    """

    http_method_names = ["get"]

    def get(self, request):
        queue_name = request.GET.get("queue_name")
        backend_name = request.GET.get("backend_name", "default")

        count = get_pending_task_count(
            queue_name=queue_name,
            backend_name=backend_name,
        )

        return JsonResponse({"pending_count": count})
