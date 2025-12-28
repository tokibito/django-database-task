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
from django.tasks.base import TaskResultStatus
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from .executor import (
    get_pending_task_count,
    process_one_task,
    process_tasks,
    run_task_by_id,
)
from .models import DatabaseTask


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


@method_decorator(csrf_exempt, name="dispatch")
class ExecuteTaskView(View):
    """
    Execute a specific task by ID via HTTP POST.

    This endpoint is designed for external trigger systems (e.g., Cloud Tasks,
    webhooks) that need to execute a specific task by ID.

    URL pattern:
        POST /tasks/execute/<task_id>/

    Query parameters:
        fail_on_error: If "true", return HTTP 500 on task failure to trigger
                       external retry mechanisms (e.g., Cloud Tasks).
                       Default: "false" (always return HTTP 200).
        allow_retry: If "true", allow execution of FAILED tasks by resetting
                     them to READY status first. This enables Cloud Tasks
                     retry mechanism. Default: "false".

    Response (task executed successfully):
        HTTP 200
        {
            "executed": true,
            "result": {
                "id": "...",
                "status": "SUCCESSFUL",
                "task_path": "..."
            }
        }

    Response (task failed, fail_on_error=false):
        HTTP 200
        {
            "executed": true,
            "result": {
                "id": "...",
                "status": "FAILED",
                "task_path": "..."
            }
        }

    Response (task failed, fail_on_error=true):
        HTTP 500
        {
            "executed": true,
            "failed": true,
            "result": {
                "id": "...",
                "status": "FAILED",
                "task_path": "..."
            }
        }

    Response (task not in READY status):
        HTTP 200
        {
            "executed": false,
            "reason": "Task is not in READY status"
        }

    Response (task not found):
        HTTP 404
        {
            "error": "Task not found"
        }

    Cloud Tasks Integration:
        To enable Cloud Tasks automatic retry on task failure:

        1. Create Cloud Tasks with URL:
           /tasks/execute/<task_id>/?fail_on_error=true&allow_retry=true
        2. Configure retry policy in Cloud Tasks
        3. On task failure, this endpoint returns HTTP 500
        4. Cloud Tasks will retry based on its retry policy
        5. On retry, allow_retry=true allows the FAILED task to be re-executed

    Security:
        - Only accepts POST requests
        - CSRF exempt (intended for external trigger systems)
        - Consider adding authentication in your URL configuration:

            # For Cloud Tasks with OIDC
            from django_database_task.views import ExecuteTaskView

            def verify_cloud_tasks_token(view_func):
                def wrapper(request, *args, **kwargs):
                    # Verify OIDC token from Cloud Tasks
                    token = request.headers.get("Authorization", "").replace("Bearer ", "")
                    # ... token verification logic ...
                    return view_func(request, *args, **kwargs)
                return wrapper

            urlpatterns = [
                path(
                    "tasks/execute/<uuid:task_id>/",
                    verify_cloud_tasks_token(ExecuteTaskView.as_view()),
                ),
            ]
    """

    http_method_names = ["post"]

    def post(self, request, task_id):
        fail_on_error = request.GET.get("fail_on_error", "").lower() == "true"
        allow_retry = request.GET.get("allow_retry", "").lower() == "true"

        try:
            result = run_task_by_id(task_id, allow_retry=allow_retry)
        except DatabaseTask.DoesNotExist:
            return JsonResponse({"error": "Task not found"}, status=404)

        if result is None:
            return JsonResponse(
                {"executed": False, "reason": "Task is not in READY status"}
            )

        task_path = (
            result.task.func.__module__ + "." + result.task.func.__qualname__
            if hasattr(result.task, "func")
            else str(result.task)
        )

        response_data = {
            "executed": True,
            "result": {
                "id": str(result.id),
                "status": result.status.value,
                "task_path": task_path,
            },
        }

        # If task failed and fail_on_error is enabled, return 500 for external retry
        if result.status == TaskResultStatus.FAILED and fail_on_error:
            response_data["failed"] = True
            return JsonResponse(response_data, status=500)

        return JsonResponse(response_data)
