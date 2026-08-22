"""
Cloud Tasks broker.

Creates a Cloud Task whenever a task is saved, so Cloud Tasks calls the
application back to execute it. The Cloud Task carries only the task id;
everything else stays in the database.
"""

import logging
from urllib.parse import urlparse

from django.core.exceptions import ImproperlyConfigured
from google.cloud import tasks_v2
from google.protobuf import timestamp_pb2

from django_database_task.brokers import HTTPPushBroker

from .detection import (
    detect_gcp_location,
    detect_gcp_project,
    detect_task_handler_host,
)

logger = logging.getLogger(__name__)


class CloudTasksBroker(HTTPPushBroker):
    """
    Broker that creates Cloud Tasks.

    Requires: pip install django-database-task[cloudtasks]

    The Cloud Tasks queue name is the task's queue_name attribute, so
    @task(queue_name="high-priority") lands in the "high-priority" queue.
    Tasks without an explicit queue use Django's DEFAULT_TASK_QUEUE_NAME
    ("default").

    Options:
        CLOUD_TASKS_PROJECT         GCP project. Detected when unset.
        CLOUD_TASKS_LOCATION        Region. Detected when unset.
        TASK_HANDLER_URL            URL template with a {task_id} placeholder.
        TASK_HANDLER_PATH           Path used with the detected host.
        OIDC_SERVICE_ACCOUNT_EMAIL  Enables OIDC on the created tasks and
                                    the verification of incoming tokens.
        OIDC_AUDIENCE               Audience to expect. Derived from the
                                    handler URL when unset.
        RETRY_CONFIG                Cloud Tasks retry configuration.
    """

    def __init__(self, backend, options=None):
        super().__init__(backend, options)

        # Auto-detected or explicit: Project ID
        self.project = self.options.get("CLOUD_TASKS_PROJECT") or detect_gcp_project()
        if not self.project:
            raise ImproperlyConfigured(
                "Could not detect GCP project. "
                "Set CLOUD_TASKS_PROJECT in TASKS OPTIONS or "
                "ensure GOOGLE_CLOUD_PROJECT environment variable is set."
            )

        # Auto-detected or explicit: Location (region)
        self.location = (
            self.options.get("CLOUD_TASKS_LOCATION") or detect_gcp_location()
        )
        if not self.location:
            raise ImproperlyConfigured(
                "Could not detect GCP location. "
                "Set CLOUD_TASKS_LOCATION in TASKS OPTIONS or "
                "ensure CLOUD_RUN_REGION environment variable is set."
            )

        # OIDC configuration (optional)
        self.oidc_service_account = self.options.get("OIDC_SERVICE_ACCOUNT_EMAIL")
        self.oidc_audience = self.options.get("OIDC_AUDIENCE")

        # Retry configuration (optional)
        self.retry_config = self.options.get("RETRY_CONFIG")

        # Cloud Tasks client (lazy initialization)
        self._client = None

    @property
    def client(self):
        """Lazy initialization of Cloud Tasks client."""
        if self._client is None:
            self._client = tasks_v2.CloudTasksClient()
        return self._client

    def close(self):
        self._client = None

    def detect_handler_host(self):
        """Detect the App Engine or Cloud Run host from the environment."""
        return detect_task_handler_host()

    def get_oidc_audience(self, url):
        """
        Get the OIDC audience from configuration or from the handler URL.

        If OIDC_AUDIENCE is set, use it. Otherwise derive it from the URL.
        """
        if self.oidc_audience:
            return self.oidc_audience

        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"

    def get_auth_handlers(self, endpoint=None):
        """
        Get the handler that verifies OIDC tokens sent by Cloud Tasks.

        Only enabled when OIDC_SERVICE_ACCOUNT_EMAIL is configured, which
        is what makes Cloud Tasks send the tokens in the first place.

        Returns:
            list of callables
        """
        if not self.oidc_service_account:
            return []

        from .auth import create_oidc_auth_handler

        audience = self.oidc_audience
        if not audience:
            # Auto-detect from the task handler URL
            try:
                audience = self.get_oidc_audience(self.get_handler_url("dummy"))
            except Exception:
                return []

        return [create_oidc_auth_handler(audience)]

    def enqueue(self, task_result):
        """
        Create a Cloud Task that triggers execution of a saved task.

        The Cloud Task holds only the task id in its URL; the parameters
        are read back from the database when it runs.
        """
        parent = self.client.queue_path(
            self.project,
            self.location,
            self.resolve_queue(task_result.task.queue_name),
        )

        # Build task handler URL (contains only task ID)
        url = self.get_handler_url(task_result.id)

        # Add query parameters for error handling and retry
        url_with_params = f"{url}?fail_on_error=true&allow_retry=true"

        http_request = {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": url_with_params,
        }

        # Add OIDC token if service account is configured
        if self.oidc_service_account:
            http_request["oidc_token"] = {
                "service_account_email": self.oidc_service_account,
                "audience": self.get_oidc_audience(url),
            }

        cloud_task = {
            "http_request": http_request,
        }

        # Add schedule time if run_after is set
        if task_result.task.run_after:
            schedule_time = timestamp_pb2.Timestamp()
            schedule_time.FromDatetime(task_result.task.run_after)
            cloud_task["schedule_time"] = schedule_time

        response = self.client.create_task(
            request={"parent": parent, "task": cloud_task}
        )

        logger.debug("Created Cloud Task: %s", response.name)
        return response
