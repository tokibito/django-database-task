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
from datetime import timedelta

from django.http import JsonResponse
from django.tasks import default_task_backend
from django.tasks.base import TaskResultStatus
from django.utils import timezone
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


def get_backend(backend_name="default"):
    """Get a task backend by name."""
    if backend_name == "default":
        return default_task_backend
    from django.tasks import tasks

    return tasks[backend_name]


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
        backend_name: Backend name for authentication handler lookup.
                      Default: "default".

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

    Authentication:
        The backend can provide an authentication handler via get_auth_handler().
        When using CloudTasksDatabaseBackend with OIDC configuration, the
        OIDC token is automatically verified before task execution.

        For custom authentication, subclass the backend and override
        get_auth_handler() to return a callable that takes a request and returns:
        - None if authentication succeeds
        - A JsonResponse with error details if authentication fails
    """

    http_method_names = ["post"]

    def post(self, request, task_id):
        backend_name = request.GET.get("backend_name", "default")

        # Get backend and check for auth handler
        backend = get_backend(backend_name)
        if hasattr(backend, "get_auth_handler"):
            auth_handler = backend.get_auth_handler()
            if auth_handler:
                error_response = auth_handler(request)
                if error_response:
                    return error_response

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


@method_decorator(csrf_exempt, name="dispatch")
class PurgeCompletedTasksView(View):
    """
    Delete completed tasks via HTTP POST.

    This endpoint is useful for cron-based cleanup of completed tasks
    when management commands cannot be executed directly.

    POST parameters (JSON body):
        days: Delete tasks completed more than N days ago (0=all, default: 0)
        status: Target statuses, comma-separated (default: "SUCCESSFUL,FAILED")
        batch_size: Number of tasks to delete at once (default: 1000, max: 10000)
        dry_run: If true, return count without deleting (default: false)

    Response:
        {
            "deleted": 150,
            "dry_run": false
        }

    Response (dry run):
        {
            "count": 150,
            "dry_run": true
        }

    Response (error):
        {
            "error": "Error message"
        }

    Security:
        - Only accepts POST requests
        - CSRF exempt (intended for API/cron use)
        - Consider adding authentication in your URL configuration
    """

    http_method_names = ["post"]

    def post(self, request):
        backend_name = request.GET.get("backend_name", "default")

        # Get backend and check for auth handler
        backend = get_backend(backend_name)
        if hasattr(backend, "get_auth_handler"):
            auth_handler = backend.get_auth_handler()
            if auth_handler:
                error_response = auth_handler(request)
                if error_response:
                    return error_response

        # Parse JSON body if present
        try:
            if request.body:
                data = json.loads(request.body)
            else:
                data = {}
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)

        days = data.get("days", 0)
        status_str = data.get("status", "SUCCESSFUL,FAILED")
        batch_size = data.get("batch_size", 1000)
        dry_run = data.get("dry_run", False)

        # Validate parameters
        if not isinstance(days, int) or days < 0:
            return JsonResponse(
                {"error": "days must be a non-negative integer"}, status=400
            )

        if not isinstance(batch_size, int) or batch_size < 1:
            return JsonResponse(
                {"error": "batch_size must be a positive integer"}, status=400
            )
        if batch_size > 10000:
            return JsonResponse({"error": "batch_size cannot exceed 10000"}, status=400)

        # Parse statuses
        statuses = [s.strip().upper() for s in status_str.split(",")]
        valid_statuses = [TaskResultStatus.SUCCESSFUL, TaskResultStatus.FAILED]
        statuses = [s for s in statuses if s in [v.value for v in valid_statuses]]

        if not statuses:
            return JsonResponse({"error": "No valid statuses specified"}, status=400)

        # Build query
        queryset = DatabaseTask.objects.filter(status__in=statuses)

        if days > 0:
            cutoff_date = timezone.now() - timedelta(days=days)
            queryset = queryset.filter(finished_at__lt=cutoff_date)

        total_count = queryset.count()

        if dry_run:
            return JsonResponse({"count": total_count, "dry_run": True})

        if total_count == 0:
            return JsonResponse({"deleted": 0, "dry_run": False})

        # Batch delete
        deleted_total = 0
        while True:
            task_ids = list(queryset.values_list("id", flat=True)[:batch_size])
            if not task_ids:
                break

            deleted_count, _ = DatabaseTask.objects.filter(id__in=task_ids).delete()
            deleted_total += deleted_count

        return JsonResponse({"deleted": deleted_total, "dry_run": False})
