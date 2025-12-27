from django.contrib import admin, messages
from django.db import transaction
from django.tasks import task_backends
from django.tasks.base import TaskResultStatus
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from .models import DatabaseTask


@admin.register(DatabaseTask)
class DatabaseTaskAdmin(admin.ModelAdmin):
    list_display = [
        "id_short",
        "task_path_short",
        "status_badge",
        "priority",
        "queue_name",
        "enqueued_at",
        "started_at",
        "finished_at",
    ]
    list_filter = ["status", "queue_name", "backend_name"]
    search_fields = ["id", "task_path"]
    ordering = ["-created_at"]
    readonly_fields = [
        "id",
        "created_at",
        "updated_at",
        "enqueued_at",
        "started_at",
        "finished_at",
        "last_attempted_at",
        "return_value_json",
        "errors_json",
        "worker_ids_json",
    ]
    fieldsets = [
        (
            "Basic Information",
            {"fields": ["id", "task_path", "backend_name", "queue_name", "priority"]},
        ),
        (
            "Status",
            {"fields": ["status", "run_after"]},
        ),
        (
            "Arguments",
            {
                "fields": ["args_json", "kwargs_json"],
                "classes": ["collapse"],
            },
        ),
        (
            "Execution Result",
            {
                "fields": ["return_value_json", "errors_json", "worker_ids_json"],
                "classes": ["collapse"],
            },
        ),
        (
            "Timestamps",
            {
                "fields": [
                    "enqueued_at",
                    "started_at",
                    "finished_at",
                    "last_attempted_at",
                    "created_at",
                    "updated_at",
                ],
                "classes": ["collapse"],
            },
        ),
    ]

    def id_short(self, obj):
        """Display shortened ID."""
        return str(obj.id)[:8]

    id_short.short_description = "ID"

    def task_path_short(self, obj):
        """Display shortened task path."""
        path = obj.task_path
        if len(path) > 40:
            return f"...{path[-37:]}"
        return path

    task_path_short.short_description = "Task"

    def status_badge(self, obj):
        """Display status as a colored badge."""
        colors = {
            "READY": "#6c757d",
            "RUNNING": "#007bff",
            "SUCCESSFUL": "#28a745",
            "FAILED": "#dc3545",
        }
        color = colors.get(obj.status, "#6c757d")
        return format_html(
            '<span style="background-color: {}; color: white; padding: 3px 8px; '
            'border-radius: 3px; font-size: 11px;">{}</span>',
            color,
            obj.status,
        )

    status_badge.short_description = "Status"

    actions = ["run_selected_tasks", "retry_failed_tasks"]

    def has_add_permission(self, request):
        """Disable adding tasks from admin."""
        return False

    @admin.action(description=_("Run selected tasks"))
    def run_selected_tasks(self, request, queryset):
        """Execute selected tasks that are in READY status."""
        ready_tasks = queryset.filter(status=TaskResultStatus.READY)
        ready_count = ready_tasks.count()

        if ready_count == 0:
            self.message_user(
                request,
                "No tasks in READY status were selected.",
                messages.WARNING,
            )
            return

        success_count = 0
        fail_count = 0

        for db_task in ready_tasks:
            try:
                backend = task_backends[db_task.backend_name]
                result = backend.run_task(db_task, worker_id="admin")
                if result.status == TaskResultStatus.SUCCESSFUL:
                    success_count += 1
                else:
                    fail_count += 1
            except Exception:
                fail_count += 1

        skipped_count = queryset.count() - ready_count
        msg_parts = []
        if success_count:
            msg_parts.append(f"{success_count} succeeded")
        if fail_count:
            msg_parts.append(f"{fail_count} failed")
        if skipped_count:
            msg_parts.append(f"{skipped_count} skipped (not READY)")

        self.message_user(
            request,
            f"Task execution completed: {', '.join(msg_parts)}.",
            messages.SUCCESS if fail_count == 0 else messages.WARNING,
        )

    @admin.action(description=_("Retry failed tasks"))
    def retry_failed_tasks(self, request, queryset):
        """Reset failed tasks to READY status and re-execute them."""
        failed_tasks = queryset.filter(status=TaskResultStatus.FAILED)
        failed_count = failed_tasks.count()

        if failed_count == 0:
            self.message_user(
                request,
                "No tasks in FAILED status were selected.",
                messages.WARNING,
            )
            return

        success_count = 0
        fail_count = 0

        for db_task in failed_tasks:
            try:
                with transaction.atomic():
                    # Reset task status to READY
                    db_task.status = TaskResultStatus.READY
                    db_task.errors_json = []
                    db_task.started_at = None
                    db_task.finished_at = None
                    db_task.return_value_json = None
                    db_task.save()

                    # Execute the task
                    backend = task_backends[db_task.backend_name]
                    result = backend.run_task(db_task, worker_id="admin-retry")

                if result.status == TaskResultStatus.SUCCESSFUL:
                    success_count += 1
                else:
                    fail_count += 1
            except Exception:
                fail_count += 1

        skipped_count = queryset.count() - failed_count
        msg_parts = []
        if success_count:
            msg_parts.append(f"{success_count} succeeded")
        if fail_count:
            msg_parts.append(f"{fail_count} failed again")
        if skipped_count:
            msg_parts.append(f"{skipped_count} skipped (not FAILED)")

        self.message_user(
            request,
            f"Retry completed: {', '.join(msg_parts)}.",
            messages.SUCCESS if fail_count == 0 else messages.WARNING,
        )
