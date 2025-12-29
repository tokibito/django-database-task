"""
Cloud Tasks Database Backend.

This backend extends DatabaseTaskBackend to automatically create
Cloud Tasks when tasks are enqueued.
"""

import logging
from urllib.parse import urlparse

from django.core.exceptions import ImproperlyConfigured
from google.cloud import tasks_v2
from google.protobuf import timestamp_pb2

from django_database_task.backends import DatabaseTaskBackend

from .detection import (
    detect_gcp_location,
    detect_gcp_project,
    detect_task_handler_host,
)

logger = logging.getLogger(__name__)


class CloudTasksDatabaseBackend(DatabaseTaskBackend):
    """
    A task backend that persists tasks in the database
    and creates Cloud Tasks for execution.

    This backend inherits all functionality from DatabaseTaskBackend
    and adds automatic Cloud Tasks creation on enqueue.

    Requires: pip install django-database-task[cloudtasks]

    Minimal configuration (GAE/Cloud Run with auto-detection):
        TASKS = {
            "default": {
                "BACKEND": "django_database_task.cloudtasks.CloudTasksDatabaseBackend",
            },
        }

    The Cloud Tasks queue name is determined by the task's queue_name attribute.
    For example:
        @task(queue="high-priority")
        def urgent_task():
            ...
    will use the "high-priority" Cloud Tasks queue.

    Tasks without explicit queue use Django's DEFAULT_TASK_QUEUE_NAME ("default").
    """

    def __init__(self, alias, params):
        super().__init__(alias, params)

        options = params.get("OPTIONS", {})

        # Auto-detected or explicit: Project ID
        self.project = options.get("CLOUD_TASKS_PROJECT") or detect_gcp_project()
        if not self.project:
            raise ImproperlyConfigured(
                "Could not detect GCP project. "
                "Set CLOUD_TASKS_PROJECT in TASKS OPTIONS or "
                "ensure GOOGLE_CLOUD_PROJECT environment variable is set."
            )

        # Auto-detected or explicit: Location (region)
        self.location = options.get("CLOUD_TASKS_LOCATION") or detect_gcp_location()
        if not self.location:
            raise ImproperlyConfigured(
                "Could not detect GCP location. "
                "Set CLOUD_TASKS_LOCATION in TASKS OPTIONS or "
                "ensure CLOUD_RUN_REGION environment variable is set."
            )

        # Task handler URL configuration
        self.task_handler_url = options.get("TASK_HANDLER_URL")
        self.task_handler_path = options.get(
            "TASK_HANDLER_PATH", "/tasks/execute/{task_id}/"
        )

        # OIDC configuration (optional)
        self.oidc_service_account = options.get("OIDC_SERVICE_ACCOUNT_EMAIL")
        self.oidc_audience = options.get("OIDC_AUDIENCE")

        # Retry configuration (optional)
        self.retry_config = options.get("RETRY_CONFIG")

        # Cloud Tasks client (lazy initialization)
        self._client = None

    @property
    def client(self):
        """Lazy initialization of Cloud Tasks client."""
        if self._client is None:
            self._client = tasks_v2.CloudTasksClient()
        return self._client

    def get_auth_handler(self):
        """
        Get the OIDC authentication handler for task execution endpoints.

        Returns an authentication handler if OIDC is configured
        (either via OIDC_SERVICE_ACCOUNT_EMAIL or auto-detected audience).

        The handler verifies OIDC tokens from Cloud Tasks before allowing
        task execution.

        Returns:
            Callable or None
        """
        # Only enable auth handler if OIDC service account is configured
        # (which means Cloud Tasks will send OIDC tokens)
        if self.oidc_service_account:
            from .auth import create_oidc_auth_handler

            # Get audience (explicit or auto-detected from handler URL)
            audience = self.oidc_audience
            if not audience:
                # Auto-detect from task handler URL
                try:
                    url = self._get_task_handler_url("dummy")
                    audience = self._get_oidc_audience(url)
                except Exception:
                    return None

            return create_oidc_auth_handler(audience)

        return None

    def enqueue(self, task, args, kwargs):
        """
        Enqueue a task to the database and create a Cloud Task.

        The task parameters are stored only in the database.
        The Cloud Task only contains the task ID to trigger execution.
        """
        # First, save to database using parent implementation
        task_result = super().enqueue(task, args, kwargs)

        # Then create Cloud Task to trigger execution
        try:
            self._create_cloud_task(task_result)
        except Exception as e:
            # Log the error but don't fail the enqueue
            # The task is saved in DB and can be executed manually
            logger.error(f"Failed to create Cloud Task for task {task_result.id}: {e}")

        return task_result

    def _get_task_handler_url(self, task_id):
        """
        Get the full URL for the task handler endpoint.

        If TASK_HANDLER_URL is set, use it directly.
        Otherwise, auto-detect host from environment and append path.
        """
        if self.task_handler_url:
            return self.task_handler_url.format(task_id=task_id)

        # Auto-detect host from environment
        host = detect_task_handler_host()
        if not host:
            raise ImproperlyConfigured(
                "Could not detect task handler host from environment. "
                "Set TASK_HANDLER_URL explicitly in TASKS OPTIONS."
            )

        path = self.task_handler_path.format(task_id=task_id)
        return f"{host}{path}"

    def _get_oidc_audience(self, url):
        """
        Get the OIDC audience from configuration or URL.

        If OIDC_AUDIENCE is set, use it.
        Otherwise, derive from the task handler URL host.
        """
        if self.oidc_audience:
            return self.oidc_audience

        # Derive audience from URL (scheme + host)
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"

    def _create_cloud_task(self, task_result):
        """
        Create a Cloud Task to trigger task execution.

        The Cloud Task only contains the task ID in the URL.
        Task parameters are retrieved from the database when executed.

        The Cloud Tasks queue name is taken from the task's queue_name attribute.
        """
        # Build queue path using task's queue_name
        queue_name = task_result.task.queue_name
        parent = self.client.queue_path(
            self.project,
            self.location,
            queue_name,
        )

        # Build task handler URL (contains only task ID)
        url = self._get_task_handler_url(task_result.id)

        # Add query parameters for error handling and retry
        url_with_params = f"{url}?fail_on_error=true&allow_retry=true"

        # Build HTTP request
        http_request = {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": url_with_params,
        }

        # Add OIDC token if service account is configured
        if self.oidc_service_account:
            http_request["oidc_token"] = {
                "service_account_email": self.oidc_service_account,
                "audience": self._get_oidc_audience(url),
            }

        # Build task
        cloud_task = {
            "http_request": http_request,
        }

        # Add schedule time if run_after is set
        if task_result.task.run_after:
            schedule_time = timestamp_pb2.Timestamp()
            schedule_time.FromDatetime(task_result.task.run_after)
            cloud_task["schedule_time"] = schedule_time

        # Create the task
        response = self.client.create_task(
            request={"parent": parent, "task": cloud_task}
        )

        logger.debug(f"Created Cloud Task: {response.name}")
        return response
