"""Tests for CloudTasksDatabaseBackend."""

from unittest.mock import MagicMock, patch

import pytest
from django.core.exceptions import ImproperlyConfigured

from tests.tasks import simple_task, special_queue_task

# Skip all tests if google-cloud-tasks is not installed
pytest.importorskip("google.cloud.tasks_v2")


class TestCloudTasksDatabaseBackendInit:
    """Tests for CloudTasksDatabaseBackend initialization."""

    def test_does_not_require_a_queue_option(self, monkeypatch):
        """The queue comes from each task, so no queue option is needed."""
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
        monkeypatch.setenv("CLOUD_RUN_REGION", "asia-northeast1")

        from django_database_task.cloudtasks import CloudTasksDatabaseBackend

        backend = CloudTasksDatabaseBackend("default", {"OPTIONS": {}})

        assert backend.project == "my-project"

    def test_requires_project(self, monkeypatch):
        """Should raise error when project cannot be detected."""
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
        monkeypatch.delenv("GAE_APPLICATION", raising=False)
        monkeypatch.setenv("CLOUD_RUN_REGION", "asia-northeast1")

        from django_database_task.cloudtasks import CloudTasksDatabaseBackend

        with pytest.raises(ImproperlyConfigured) as exc_info:
            CloudTasksDatabaseBackend("default", {"OPTIONS": {}})

        assert "Could not detect GCP project" in str(exc_info.value)

    def test_requires_location(self, monkeypatch):
        """Should raise error when location cannot be detected."""
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
        monkeypatch.delenv("CLOUD_RUN_REGION", raising=False)
        monkeypatch.delenv("GAE_REGION", raising=False)
        monkeypatch.delenv("GOOGLE_CLOUD_REGION", raising=False)

        from django_database_task.cloudtasks import CloudTasksDatabaseBackend

        with pytest.raises(ImproperlyConfigured) as exc_info:
            CloudTasksDatabaseBackend("default", {"OPTIONS": {}})

        assert "Could not detect GCP location" in str(exc_info.value)

    def test_successful_init_with_auto_detection(self, monkeypatch):
        """Should initialize successfully with auto-detected values."""
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
        monkeypatch.setenv("CLOUD_RUN_REGION", "asia-northeast1")

        from django_database_task.cloudtasks import CloudTasksDatabaseBackend

        backend = CloudTasksDatabaseBackend("default", {"OPTIONS": {}})

        assert backend.project == "my-project"
        assert backend.location == "asia-northeast1"

    def test_explicit_config_overrides_detection(self, monkeypatch):
        """Should use explicit config over auto-detection."""
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "detected-project")
        monkeypatch.setenv("CLOUD_RUN_REGION", "detected-region")

        from django_database_task.cloudtasks import CloudTasksDatabaseBackend

        backend = CloudTasksDatabaseBackend(
            "default",
            {
                "OPTIONS": {
                    "CLOUD_TASKS_PROJECT": "explicit-project",
                    "CLOUD_TASKS_LOCATION": "explicit-region",
                }
            },
        )

        assert backend.project == "explicit-project"
        assert backend.location == "explicit-region"


class TestCloudTasksDatabaseBackendEnqueue:
    """Tests for enqueue method."""

    @pytest.fixture
    def backend(self, monkeypatch):
        """Create a backend instance for testing."""
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
        monkeypatch.setenv("CLOUD_RUN_REGION", "asia-northeast1")
        monkeypatch.setenv("K_SERVICE", "my-service")

        from django_database_task.cloudtasks import CloudTasksDatabaseBackend

        return CloudTasksDatabaseBackend("default", {"OPTIONS": {}})

    @pytest.mark.django_db
    def test_enqueue_creates_cloud_task(self, backend):
        """Should create Cloud Task after saving to database."""
        # Mock the Cloud Tasks client
        mock_client = MagicMock()
        mock_client.queue_path.return_value = (
            "projects/my-project/locations/asia-northeast1/queues/default"
        )
        mock_client.create_task.return_value = MagicMock(name="task-name")

        with patch.object(backend.broker, "_client", mock_client):
            result = backend.enqueue(simple_task, (2, 3), {})

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

        # The queue comes from the task itself
        mock_client.queue_path.assert_called_once_with(
            "my-project", "asia-northeast1", "default"
        )

    @pytest.mark.django_db
    def test_enqueue_uses_the_queue_of_the_task(self, monkeypatch):
        """A task with its own queue is sent to the matching Cloud Tasks queue."""
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
        monkeypatch.setenv("CLOUD_RUN_REGION", "asia-northeast1")
        monkeypatch.setenv("K_SERVICE", "my-service")

        from django_database_task.cloudtasks import CloudTasksDatabaseBackend

        # An empty QUEUES list lets the backend accept any queue name.
        backend = CloudTasksDatabaseBackend("default", {"QUEUES": [], "OPTIONS": {}})

        mock_client = MagicMock()
        mock_client.create_task.return_value = MagicMock(name="task-name")

        with patch.object(backend.broker, "_client", mock_client):
            backend.enqueue(special_queue_task, (), {})

        mock_client.queue_path.assert_called_once_with(
            "my-project", "asia-northeast1", "special"
        )

    @pytest.mark.django_db
    def test_enqueue_continues_on_cloud_task_error(self, backend, caplog):
        """Should continue even if Cloud Task creation fails."""
        # Mock the Cloud Tasks client to raise an error
        mock_client = MagicMock()
        mock_client.queue_path.return_value = (
            "projects/my-project/locations/asia-northeast1/queues/default"
        )
        mock_client.create_task.side_effect = Exception("API Error")

        with patch.object(backend.broker, "_client", mock_client):
            result = backend.enqueue(simple_task, (2, 3), {})

        # Task should still be saved to database
        assert result.id is not None

        # Error should be logged, with the traceback
        assert "CloudTasksBroker failed to enqueue task" in caplog.text
        assert "API Error" in caplog.text


class TestCloudTasksDatabaseBackendGetAuthHandler:
    """Tests for get_auth_handler method."""

    def test_returns_none_without_oidc_config(self, monkeypatch):
        """Should return None when OIDC is not configured."""
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
        monkeypatch.setenv("CLOUD_RUN_REGION", "asia-northeast1")
        monkeypatch.setenv("K_SERVICE", "my-service")

        from django_database_task.cloudtasks import CloudTasksDatabaseBackend

        backend = CloudTasksDatabaseBackend("default", {"OPTIONS": {}})

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
                    "OIDC_SERVICE_ACCOUNT_EMAIL": "sa@project.iam.gserviceaccount.com",
                    "OIDC_AUDIENCE": "https://custom-audience.com",
                }
            },
        )

        handler = backend.get_auth_handler()
        assert handler is not None


class TestCloudTasksDatabaseBackendGetAuthHandlers:
    """Tests for combining the OIDC handler with configured handlers."""

    @staticmethod
    def _backend(monkeypatch, **extra_options):
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
        monkeypatch.setenv("CLOUD_RUN_REGION", "asia-northeast1")
        monkeypatch.setenv("K_SERVICE", "my-service")

        from django_database_task.cloudtasks import CloudTasksDatabaseBackend

        options = dict(extra_options)
        return CloudTasksDatabaseBackend("default", {"OPTIONS": options})

    def test_returns_nothing_without_any_configuration(self, monkeypatch):
        """Endpoints stay unauthenticated when nothing is configured."""
        backend = self._backend(monkeypatch)

        assert backend.get_auth_handlers() == []

    def test_returns_the_oidc_handler(self, monkeypatch):
        """OIDC alone behaves as it did before 0.4."""
        backend = self._backend(
            monkeypatch,
            OIDC_SERVICE_ACCOUNT_EMAIL="sa@project.iam.gserviceaccount.com",
        )

        assert len(backend.get_auth_handlers()) == 1

    def test_oidc_and_a_shared_secret_coexist(self, monkeypatch):
        """An external cron job can be let in alongside Cloud Tasks.

        Before 0.4 the OIDC handler was the only one applied, so enabling it
        locked every other caller out of the endpoints.
        """
        backend = self._backend(
            monkeypatch,
            OIDC_SERVICE_ACCOUNT_EMAIL="sa@project.iam.gserviceaccount.com",
            AUTH_HANDLERS=["django_database_task.auth.SharedSecretAuth"],
            AUTH_HANDLER_OPTIONS={"TOKEN": "s3cret"},
        )

        handlers = backend.get_auth_handlers()

        assert len(handlers) == 2

    def test_does_not_warn_about_the_deprecated_api(self, monkeypatch, recwarn):
        """The bundled backend keeps get_auth_handler() without deprecation noise."""
        backend = self._backend(
            monkeypatch,
            OIDC_SERVICE_ACCOUNT_EMAIL="sa@project.iam.gserviceaccount.com",
        )
        backend.get_auth_handlers()

        assert [
            w for w in recwarn.list if issubclass(w.category, DeprecationWarning)
        ] == []

    def test_configured_handlers_can_be_scoped_to_an_endpoint(self, monkeypatch):
        """A cron-only credential need not apply to the execute endpoint."""
        backend = self._backend(
            monkeypatch,
            OIDC_SERVICE_ACCOUNT_EMAIL="sa@project.iam.gserviceaccount.com",
            AUTH_HANDLERS=[
                {
                    "HANDLER": "django_database_task.auth.SharedSecretAuth",
                    "OPTIONS": {"TOKEN": "s3cret"},
                    "ENDPOINTS": ["run", "purge"],
                }
            ],
        )

        assert len(backend.get_auth_handlers("execute")) == 1
        assert len(backend.get_auth_handlers("purge")) == 2
