import uuid

from django.db import models
from django.tasks.base import TaskResultStatus
from django.utils.translation import gettext_lazy as _


class DatabaseTask(models.Model):
    """Task model persisted in the database."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task_path = models.CharField(
        max_length=512,
        verbose_name=_("task path"),
        help_text=_("Module path of the task function"),
    )
    queue_name = models.CharField(
        max_length=255,
        default="default",
        verbose_name=_("queue name"),
        help_text=_("Queue name"),
    )
    priority = models.IntegerField(
        default=0,
        verbose_name=_("priority"),
        help_text=_("Priority (-100 to 100)"),
    )
    args_json = models.JSONField(
        default=list,
        verbose_name=_("arguments"),
        help_text=_("Arguments (JSON format)"),
    )
    kwargs_json = models.JSONField(
        default=dict,
        verbose_name=_("keyword arguments"),
        help_text=_("Keyword arguments (JSON format)"),
    )
    status = models.CharField(
        max_length=20,
        choices=TaskResultStatus.choices,
        default=TaskResultStatus.READY,
        verbose_name=_("status"),
        help_text=_("Task status"),
    )
    run_after = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("run after"),
        help_text=_("Earliest execution time"),
    )
    enqueued_at = models.DateTimeField(
        verbose_name=_("enqueued at"),
        help_text=_("Time when task was enqueued"),
    )
    started_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("started at"),
        help_text=_("Execution start time"),
    )
    finished_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("finished at"),
        help_text=_("Execution end time"),
    )
    last_attempted_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("last attempted at"),
        help_text=_("Last attempt time"),
    )
    return_value_json = models.JSONField(
        null=True,
        blank=True,
        verbose_name=_("return value"),
        help_text=_("Return value (JSON format)"),
    )
    errors_json = models.JSONField(
        default=list,
        verbose_name=_("errors"),
        help_text=_("Error information (JSON format)"),
    )
    worker_ids_json = models.JSONField(
        default=list,
        verbose_name=_("worker IDs"),
        help_text=_("List of worker IDs (JSON format)"),
    )
    backend_name = models.CharField(
        max_length=255,
        verbose_name=_("backend name"),
        help_text=_("Backend name"),
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name=_("created at"),
        help_text=_("Record creation time"),
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        verbose_name=_("updated at"),
        help_text=_("Record update time"),
    )

    class Meta:
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
        verbose_name = _("database task")
        verbose_name_plural = _("database tasks")

    def __str__(self):
        return f"{self.task_path} ({self.status}) - {self.id}"
