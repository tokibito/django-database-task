from django.contrib import admin
from django.utils.html import format_html

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
            {
                "fields": ["id", "task_path", "backend_name", "queue_name", "priority"]
            },
        ),
        (
            "Status",
            {
                "fields": ["status", "run_after"]
            },
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

    def has_add_permission(self, request):
        """Disable adding tasks from admin."""
        return False
