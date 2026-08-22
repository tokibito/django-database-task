"""Tests for CloudTasksBroker."""

from unittest.mock import MagicMock

import pytest
from django.core.exceptions import ImproperlyConfigured

# Skip all tests if google-cloud-tasks is not installed
pytest.importorskip("google.cloud.tasks_v2")


def make_broker(monkeypatch, **options):
    """Build a broker with the Cloud Run environment detected."""
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
    monkeypatch.setenv("CLOUD_RUN_REGION", "asia-northeast1")
    monkeypatch.setenv("K_SERVICE", "my-service")

    from django_database_task.cloudtasks import CloudTasksBroker

    return CloudTasksBroker(backend=None, options=options)


class TestCloudTasksBrokerInit:
    """Tests for the configuration the broker reads."""

    def test_detects_project_and_location(self, monkeypatch):
        broker = make_broker(monkeypatch)

        assert broker.project == "my-project"
        assert broker.location == "asia-northeast1"

    def test_explicit_config_wins_over_detection(self, monkeypatch):
        broker = make_broker(
            monkeypatch,
            CLOUD_TASKS_PROJECT="explicit-project",
            CLOUD_TASKS_LOCATION="explicit-region",
        )

        assert broker.project == "explicit-project"
        assert broker.location == "explicit-region"

    def test_requires_a_project(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
        monkeypatch.delenv("GAE_APPLICATION", raising=False)
        monkeypatch.setenv("CLOUD_RUN_REGION", "asia-northeast1")

        from django_database_task.cloudtasks import CloudTasksBroker

        with pytest.raises(ImproperlyConfigured, match="Could not detect GCP project"):
            CloudTasksBroker(backend=None, options={})

    def test_requires_a_location(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
        monkeypatch.delenv("CLOUD_RUN_REGION", raising=False)
        monkeypatch.delenv("GAE_REGION", raising=False)
        monkeypatch.delenv("GOOGLE_CLOUD_REGION", raising=False)

        from django_database_task.cloudtasks import CloudTasksBroker

        with pytest.raises(ImproperlyConfigured, match="Could not detect GCP location"):
            CloudTasksBroker(backend=None, options={})


class TestCloudTasksBrokerHandlerUrl:
    """Tests for the URL Cloud Tasks is told to call."""

    def test_uses_explicit_url(self, monkeypatch):
        broker = make_broker(
            monkeypatch, TASK_HANDLER_URL="https://example.com/tasks/{task_id}/"
        )

        assert broker.get_handler_url("abc-123") == "https://example.com/tasks/abc-123/"

    def test_auto_detects_the_host(self, monkeypatch):
        broker = make_broker(monkeypatch)

        assert broker.get_handler_url("abc-123") == (
            "https://my-service-my-project.asia-northeast1.run.app"
            "/tasks/execute/abc-123/"
        )

    def test_uses_a_custom_path(self, monkeypatch):
        broker = make_broker(
            monkeypatch, TASK_HANDLER_PATH="/api/v1/tasks/{task_id}/run/"
        )

        assert broker.get_handler_url("abc-123") == (
            "https://my-service-my-project.asia-northeast1.run.app"
            "/api/v1/tasks/abc-123/run/"
        )

    def test_reports_an_undetectable_host(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
        monkeypatch.setenv("CLOUD_RUN_REGION", "asia-northeast1")
        monkeypatch.delenv("K_SERVICE", raising=False)
        monkeypatch.delenv("GAE_SERVICE", raising=False)
        monkeypatch.delenv("GAE_VERSION", raising=False)

        from django_database_task.cloudtasks import CloudTasksBroker

        broker = CloudTasksBroker(backend=None, options={})

        with pytest.raises(ImproperlyConfigured, match="Set TASK_HANDLER_URL"):
            broker.get_handler_url("abc-123")


class TestCloudTasksBrokerOidcAudience:
    """Tests for the audience of the OIDC tokens."""

    def test_derives_the_audience_from_the_url(self, monkeypatch):
        broker = make_broker(monkeypatch)

        audience = broker.get_oidc_audience("https://example.com/tasks/execute/1/")

        assert audience == "https://example.com"

    def test_explicit_audience_wins(self, monkeypatch):
        broker = make_broker(monkeypatch, OIDC_AUDIENCE="https://custom-audience.com")

        audience = broker.get_oidc_audience("https://example.com/tasks/execute/1/")

        assert audience == "https://custom-audience.com"


class TestCloudTasksBrokerAuthHandlers:
    """Tests for the handlers that verify incoming OIDC tokens."""

    def test_no_handler_without_oidc(self, monkeypatch):
        assert make_broker(monkeypatch).get_auth_handlers() == []

    def test_a_handler_with_oidc(self, monkeypatch):
        broker = make_broker(
            monkeypatch,
            OIDC_SERVICE_ACCOUNT_EMAIL="sa@project.iam.gserviceaccount.com",
        )

        handlers = broker.get_auth_handlers()

        assert len(handlers) == 1
        assert callable(handlers[0])

    def test_no_handler_when_the_audience_cannot_be_worked_out(self, monkeypatch):
        """An unusable audience must not authenticate everyone by accident."""
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
        monkeypatch.setenv("CLOUD_RUN_REGION", "asia-northeast1")
        monkeypatch.delenv("K_SERVICE", raising=False)
        monkeypatch.delenv("GAE_SERVICE", raising=False)

        from django_database_task.cloudtasks import CloudTasksBroker

        broker = CloudTasksBroker(
            backend=None,
            options={
                "OIDC_SERVICE_ACCOUNT_EMAIL": "sa@project.iam.gserviceaccount.com"
            },
        )

        assert broker.get_auth_handlers() == []


class TestCloudTasksBrokerQueue:
    """Tests for the queue a task is sent to."""

    def test_the_queue_name_is_used_as_it_is(self, monkeypatch):
        """No mapping is applied: the Cloud Tasks queue is the Django queue."""
        broker = make_broker(monkeypatch)

        assert broker.resolve_queue("ranking") == "ranking"


class TestCloudTasksBrokerClose:
    """Tests for releasing the client."""

    def test_close_drops_the_client(self, monkeypatch):
        broker = make_broker(monkeypatch)
        broker._client = MagicMock()

        broker.close()

        assert broker._client is None
