"""Tests for CloudTasksDatabaseBackend."""

from unittest.mock import MagicMock, patch

import pytest
from django.core.exceptions import ImproperlyConfigured

# Skip all tests if google-cloud-tasks is not installed
pytest.importorskip("google.cloud.tasks_v2")


class TestCloudTasksDatabaseBackendInit:
    """Tests for CloudTasksDatabaseBackend initialization."""

    def test_requires_queue_name(self, monkeypatch):
        """Should raise error when CLOUD_TASKS_QUEUE is not set."""
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
        monkeypatch.setenv("CLOUD_RUN_REGION", "asia-northeast1")

        from django_database_task.cloudtasks import CloudTasksDatabaseBackend

        with pytest.raises(ImproperlyConfigured) as exc_info:
            CloudTasksDatabaseBackend("default", {"OPTIONS": {}})

        assert "CLOUD_TASKS_QUEUE is required" in str(exc_info.value)

    def test_requires_project(self, monkeypatch):
        """Should raise error when project cannot be detected."""
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
        monkeypatch.delenv("GAE_APPLICATION", raising=False)
        monkeypatch.setenv("CLOUD_RUN_REGION", "asia-northeast1")

        from django_database_task.cloudtasks import CloudTasksDatabaseBackend

        with pytest.raises(ImproperlyConfigured) as exc_info:
            CloudTasksDatabaseBackend(
                "default", {"OPTIONS": {"CLOUD_TASKS_QUEUE": "default"}}
            )

        assert "Could not detect GCP project" in str(exc_info.value)

    def test_requires_location(self, monkeypatch):
        """Should raise error when location cannot be detected."""
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
        monkeypatch.delenv("CLOUD_RUN_REGION", raising=False)
        monkeypatch.delenv("GAE_REGION", raising=False)
        monkeypatch.delenv("GOOGLE_CLOUD_REGION", raising=False)

        from django_database_task.cloudtasks import CloudTasksDatabaseBackend

        with pytest.raises(ImproperlyConfigured) as exc_info:
            CloudTasksDatabaseBackend(
                "default", {"OPTIONS": {"CLOUD_TASKS_QUEUE": "default"}}
            )

        assert "Could not detect GCP location" in str(exc_info.value)

    def test_successful_init_with_auto_detection(self, monkeypatch):
        """Should initialize successfully with auto-detected values."""
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
        monkeypatch.setenv("CLOUD_RUN_REGION", "asia-northeast1")

        from django_database_task.cloudtasks import CloudTasksDatabaseBackend

        backend = CloudTasksDatabaseBackend(
            "default", {"OPTIONS": {"CLOUD_TASKS_QUEUE": "my-queue"}}
        )

        assert backend.project == "my-project"
        assert backend.location == "asia-northeast1"
        assert backend.queue_name == "my-queue"

    def test_explicit_config_overrides_detection(self, monkeypatch):
        """Should use explicit config over auto-detection."""
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "detected-project")
        monkeypatch.setenv("CLOUD_RUN_REGION", "detected-region")

        from django_database_task.cloudtasks import CloudTasksDatabaseBackend

        backend = CloudTasksDatabaseBackend(
            "default",
            {
                "OPTIONS": {
                    "CLOUD_TASKS_QUEUE": "my-queue",
                    "CLOUD_TASKS_PROJECT": "explicit-project",
                    "CLOUD_TASKS_LOCATION": "explicit-region",
                }
            },
        )

        assert backend.project == "explicit-project"
        assert backend.location == "explicit-region"


class TestCloudTasksDatabaseBackendGetTaskHandlerUrl:
    """Tests for _get_task_handler_url method."""

    @pytest.fixture
    def backend(self, monkeypatch):
        """Create a backend instance for testing."""
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
        monkeypatch.setenv("CLOUD_RUN_REGION", "asia-northeast1")
        monkeypatch.setenv("K_SERVICE", "my-service")

        from django_database_task.cloudtasks import CloudTasksDatabaseBackend

        return CloudTasksDatabaseBackend(
            "default", {"OPTIONS": {"CLOUD_TASKS_QUEUE": "default"}}
        )

    def test_uses_explicit_url(self, monkeypatch):
        """Should use TASK_HANDLER_URL when set."""
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
        monkeypatch.setenv("CLOUD_RUN_REGION", "asia-northeast1")

        from django_database_task.cloudtasks import CloudTasksDatabaseBackend

        backend = CloudTasksDatabaseBackend(
            "default",
            {
                "OPTIONS": {
                    "CLOUD_TASKS_QUEUE": "default",
                    "TASK_HANDLER_URL": "https://example.com/tasks/{task_id}/",
                }
            },
        )

        url = backend._get_task_handler_url("abc-123")
        assert url == "https://example.com/tasks/abc-123/"

    def test_auto_detects_host(self, backend):
        """Should auto-detect host when TASK_HANDLER_URL is not set."""
        url = backend._get_task_handler_url("abc-123")
        expected = "https://my-service-my-project.asia-northeast1.run.app/tasks/execute/abc-123/"
        assert url == expected

    def test_custom_path(self, monkeypatch):
        """Should use custom TASK_HANDLER_PATH."""
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
        monkeypatch.setenv("CLOUD_RUN_REGION", "asia-northeast1")
        monkeypatch.setenv("K_SERVICE", "my-service")

        from django_database_task.cloudtasks import CloudTasksDatabaseBackend

        backend = CloudTasksDatabaseBackend(
            "default",
            {
                "OPTIONS": {
                    "CLOUD_TASKS_QUEUE": "default",
                    "TASK_HANDLER_PATH": "/api/v1/tasks/{task_id}/run/",
                }
            },
        )

        url = backend._get_task_handler_url("abc-123")
        expected = "https://my-service-my-project.asia-northeast1.run.app/api/v1/tasks/abc-123/run/"
        assert url == expected


class TestCloudTasksDatabaseBackendEnqueue:
    """Tests for enqueue method."""

    @pytest.fixture
    def backend(self, monkeypatch):
        """Create a backend instance for testing."""
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
        monkeypatch.setenv("CLOUD_RUN_REGION", "asia-northeast1")
        monkeypatch.setenv("K_SERVICE", "my-service")

        from django_database_task.cloudtasks import CloudTasksDatabaseBackend

        return CloudTasksDatabaseBackend(
            "default", {"OPTIONS": {"CLOUD_TASKS_QUEUE": "default"}}
        )

    @pytest.mark.django_db
    def test_enqueue_creates_cloud_task(self, backend, monkeypatch):
        """Should create Cloud Task after saving to database."""
        from django.tasks import task

        @task
        def sample_task(x):
            return x * 2

        # Mock the Cloud Tasks client
        mock_client = MagicMock()
        mock_client.queue_path.return_value = (
            "projects/my-project/locations/asia-northeast1/queues/default"
        )
        mock_client.create_task.return_value = MagicMock(name="task-name")

        with patch.object(backend, "_client", mock_client):
            result = backend.enqueue(sample_task, (5,), {})

        # Verify task was saved to database
        assert result.id is not None

        # Verify Cloud Task was created
        mock_client.create_task.assert_called_once()
        call_args = mock_client.create_task.call_args
        request = call_args.kwargs["request"]

        assert (
            request["parent"]
            == "projects/my-project/locations/asia-northeast1/queues/default"
        )
        assert "http_request" in request["task"]
        assert result.id in request["task"]["http_request"]["url"]
        assert "fail_on_error=true" in request["task"]["http_request"]["url"]
        assert "allow_retry=true" in request["task"]["http_request"]["url"]

    @pytest.mark.django_db
    def test_enqueue_continues_on_cloud_task_error(self, backend, monkeypatch, caplog):
        """Should continue even if Cloud Task creation fails."""
        from django.tasks import task

        @task
        def sample_task(x):
            return x * 2

        # Mock the Cloud Tasks client to raise an error
        mock_client = MagicMock()
        mock_client.queue_path.return_value = (
            "projects/my-project/locations/asia-northeast1/queues/default"
        )
        mock_client.create_task.side_effect = Exception("API Error")

        with patch.object(backend, "_client", mock_client):
            result = backend.enqueue(sample_task, (5,), {})

        # Task should still be saved to database
        assert result.id is not None

        # Error should be logged
        assert "Failed to create Cloud Task" in caplog.text


class TestCloudTasksDatabaseBackendGetAuthHandler:
    """Tests for get_auth_handler method."""

    def test_returns_none_without_oidc_config(self, monkeypatch):
        """Should return None when OIDC is not configured."""
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
        monkeypatch.setenv("CLOUD_RUN_REGION", "asia-northeast1")
        monkeypatch.setenv("K_SERVICE", "my-service")

        from django_database_task.cloudtasks import CloudTasksDatabaseBackend

        backend = CloudTasksDatabaseBackend(
            "default", {"OPTIONS": {"CLOUD_TASKS_QUEUE": "default"}}
        )

        assert backend.get_auth_handler() is None

    def test_returns_handler_with_oidc_config(self, monkeypatch):
        """Should return auth handler when OIDC is configured."""
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
        monkeypatch.setenv("CLOUD_RUN_REGION", "asia-northeast1")
        monkeypatch.setenv("K_SERVICE", "my-service")

        from django_database_task.cloudtasks import CloudTasksDatabaseBackend

        backend = CloudTasksDatabaseBackend(
            "default",
            {
                "OPTIONS": {
                    "CLOUD_TASKS_QUEUE": "default",
                    "OIDC_SERVICE_ACCOUNT_EMAIL": "sa@project.iam.gserviceaccount.com",
                }
            },
        )

        handler = backend.get_auth_handler()
        assert handler is not None
        assert callable(handler)

    def test_uses_explicit_audience(self, monkeypatch):
        """Should use explicit OIDC_AUDIENCE when set."""
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
        monkeypatch.setenv("CLOUD_RUN_REGION", "asia-northeast1")
        monkeypatch.setenv("K_SERVICE", "my-service")

        from django_database_task.cloudtasks import CloudTasksDatabaseBackend

        backend = CloudTasksDatabaseBackend(
            "default",
            {
                "OPTIONS": {
                    "CLOUD_TASKS_QUEUE": "default",
                    "OIDC_SERVICE_ACCOUNT_EMAIL": "sa@project.iam.gserviceaccount.com",
                    "OIDC_AUDIENCE": "https://custom-audience.com",
                }
            },
        )

        handler = backend.get_auth_handler()
        assert handler is not None
