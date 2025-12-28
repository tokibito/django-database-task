"""
URL configuration for django_database_task.

Optional URL patterns for HTTP-based task execution.
Include these in your project's urls.py if you need HTTP endpoints.

Example:
    # Basic usage (no authentication)
    from django.urls import path, include

    urlpatterns = [
        path("tasks/", include("django_database_task.urls")),
    ]

    # With authentication (recommended for production)
    from django.contrib.admin.views.decorators import staff_member_required
    from django_database_task.views import RunTasksView, RunOneTaskView, TaskStatusView

    urlpatterns = [
        path(
            "tasks/run/",
            staff_member_required(RunTasksView.as_view()),
            name="run_tasks",
        ),
        path(
            "tasks/run-one/",
            staff_member_required(RunOneTaskView.as_view()),
            name="run_one_task",
        ),
        path(
            "tasks/status/",
            staff_member_required(TaskStatusView.as_view()),
            name="task_status",
        ),
    ]

    # With token-based authentication
    from django.http import HttpResponseForbidden

    def require_token(view_func):
        def wrapper(request, *args, **kwargs):
            token = request.headers.get("Authorization", "").replace("Bearer ", "")
            if token != settings.TASK_API_TOKEN:
                return HttpResponseForbidden("Invalid token")
            return view_func(request, *args, **kwargs)
        return wrapper

    urlpatterns = [
        path(
            "tasks/run/",
            require_token(RunTasksView.as_view()),
            name="run_tasks",
        ),
    ]
"""

from django.urls import path

from .views import (
    ExecuteTaskView,
    PurgeCompletedTasksView,
    RunOneTaskView,
    RunTasksView,
    TaskStatusView,
)

app_name = "django_database_task"

urlpatterns = [
    path("run/", RunTasksView.as_view(), name="run_tasks"),
    path("run-one/", RunOneTaskView.as_view(), name="run_one_task"),
    path("status/", TaskStatusView.as_view(), name="task_status"),
    path("execute/<uuid:task_id>/", ExecuteTaskView.as_view(), name="execute_task"),
    path("purge/", PurgeCompletedTasksView.as_view(), name="purge_completed_tasks"),
]
