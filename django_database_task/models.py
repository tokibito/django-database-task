import uuid

from django.db import models
from django.tasks.base import TaskResultStatus


class DatabaseTask(models.Model):
    """Task model persisted in the database."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task_path = models.CharField(
        max_length=512, help_text="Module path of the task function"
    )
    queue_name = models.CharField(
        max_length=255, default="default", help_text="Queue name"
    )
    priority = models.IntegerField(default=0, help_text="Priority (-100 to 100)")
    args_json = models.JSONField(default=list, help_text="Arguments (JSON format)")
    kwargs_json = models.JSONField(
        default=dict, help_text="Keyword arguments (JSON format)"
    )
    status = models.CharField(
        max_length=20,
        choices=TaskResultStatus.choices,
        default=TaskResultStatus.READY,
        help_text="Task status",
    )
    run_after = models.DateTimeField(
        null=True, blank=True, help_text="Earliest execution time"
    )
    enqueued_at = models.DateTimeField(help_text="Time when task was enqueued")
    started_at = models.DateTimeField(
        null=True, blank=True, help_text="Execution start time"
    )
    finished_at = models.DateTimeField(
        null=True, blank=True, help_text="Execution end time"
    )
    last_attempted_at = models.DateTimeField(
        null=True, blank=True, help_text="Last attempt time"
    )
    return_value_json = models.JSONField(
        null=True, blank=True, help_text="Return value (JSON format)"
    )
    errors_json = models.JSONField(
        default=list, help_text="Error information (JSON format)"
    )
    worker_ids_json = models.JSONField(
        default=list, help_text="List of worker IDs (JSON format)"
    )
    backend_name = models.CharField(max_length=255, help_text="Backend name")
    created_at = models.DateTimeField(
        auto_now_add=True, help_text="Record creation time"
    )
    updated_at = models.DateTimeField(auto_now=True, help_text="Record update time")

    class Meta:
        db_table = "django_database_task"
        ordering = ["-priority", "enqueued_at"]
        indexes = [
            models.Index(
                fields=["status", "run_after", "priority"],
                name="ddt_status_run_priority_idx",
            ),
            models.Index(fields=["queue_name"], name="ddt_queue_name_idx"),
            models.Index(fields=["status"], name="ddt_status_idx"),
            models.Index(fields=["created_at"], name="ddt_created_at_idx"),
        ]
        verbose_name = "Database Task"
        verbose_name_plural = "Database Tasks"

    def __str__(self):
        return f"{self.task_path} ({self.status}) - {self.id}"
