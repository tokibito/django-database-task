"""Tests for environment detection functions."""

from django_database_task.cloudtasks.detection import (
    detect_default_service_account,
    detect_gcp_location,
    detect_gcp_project,
    detect_task_handler_host,
    is_app_engine,
    is_cloud_run,
)


class TestDetectGcpProject:
    """Tests for detect_gcp_project()."""

    def test_from_google_cloud_project(self, monkeypatch):
        """Should detect project from GOOGLE_CLOUD_PROJECT."""
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
        assert detect_gcp_project() == "my-project"

    def test_from_gae_application(self, monkeypatch):
        """Should detect project from GAE_APPLICATION."""
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
        monkeypatch.setenv("GAE_APPLICATION", "s~my-gae-project")
        assert detect_gcp_project() == "my-gae-project"

    def test_from_gae_application_without_prefix(self, monkeypatch):
        """Should handle GAE_APPLICATION without s~ prefix."""
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
        monkeypatch.setenv("GAE_APPLICATION", "my-gae-project")
        assert detect_gcp_project() == "my-gae-project"

    def test_returns_none_when_not_detected(self, monkeypatch):
        """Should return None when no environment variables are set."""
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
        monkeypatch.delenv("GAE_APPLICATION", raising=False)
        assert detect_gcp_project() is None


class TestDetectGcpLocation:
    """Tests for detect_gcp_location()."""

    def test_from_cloud_run_region(self, monkeypatch):
        """Should detect location from CLOUD_RUN_REGION."""
        monkeypatch.setenv("CLOUD_RUN_REGION", "asia-northeast1")
        assert detect_gcp_location() == "asia-northeast1"

    def test_from_google_cloud_region(self, monkeypatch):
        """Should detect location from GOOGLE_CLOUD_REGION."""
        monkeypatch.delenv("CLOUD_RUN_REGION", raising=False)
        monkeypatch.setenv("GOOGLE_CLOUD_REGION", "europe-west1")
        assert detect_gcp_location() == "europe-west1"

    def test_returns_none_when_not_detected(self, monkeypatch):
        """Should return None when no environment variables are set."""
        monkeypatch.delenv("CLOUD_RUN_REGION", raising=False)
        monkeypatch.delenv("GOOGLE_CLOUD_REGION", raising=False)
        assert detect_gcp_location() is None


class TestDetectDefaultServiceAccount:
    """Tests for detect_default_service_account()."""

    def test_gae_service_account(self, monkeypatch):
        """Should return GAE service account format."""
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
        monkeypatch.setenv("GAE_SERVICE", "default")
        assert (
            detect_default_service_account() == "my-project@appspot.gserviceaccount.com"
        )

    def test_returns_none_for_cloud_run(self, monkeypatch):
        """Should return None for Cloud Run (requires project number)."""
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
        monkeypatch.setenv("K_SERVICE", "my-service")
        monkeypatch.delenv("GAE_SERVICE", raising=False)
        assert detect_default_service_account() is None

    def test_returns_none_when_project_not_detected(self, monkeypatch):
        """Should return None when project cannot be detected."""
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
        monkeypatch.delenv("GAE_APPLICATION", raising=False)
        assert detect_default_service_account() is None


class TestDetectTaskHandlerHost:
    """Tests for detect_task_handler_host()."""

    def test_cloud_run_service_url(self, monkeypatch):
        """Should build Cloud Run service URL."""
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
        monkeypatch.setenv("K_SERVICE", "my-service")
        monkeypatch.setenv("CLOUD_RUN_REGION", "asia-northeast1")
        monkeypatch.delenv("GAE_SERVICE", raising=False)

        expected = "https://my-service-my-project.asia-northeast1.run.app"
        assert detect_task_handler_host() == expected

    def test_gae_default_service_url(self, monkeypatch):
        """Should build GAE default service URL with version."""
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
        monkeypatch.setenv("GAE_SERVICE", "default")
        monkeypatch.setenv("GAE_VERSION", "v1")
        monkeypatch.delenv("K_SERVICE", raising=False)
        monkeypatch.delenv("CLOUD_RUN_REGION", raising=False)

        expected = "https://v1-dot-my-project.appspot.com"
        assert detect_task_handler_host() == expected

    def test_gae_non_default_service_url(self, monkeypatch):
        """Should build GAE non-default service URL with version."""
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
        monkeypatch.setenv("GAE_SERVICE", "worker")
        monkeypatch.setenv("GAE_VERSION", "v2")
        monkeypatch.delenv("K_SERVICE", raising=False)
        monkeypatch.delenv("CLOUD_RUN_REGION", raising=False)

        expected = "https://v2-dot-worker-dot-my-project.appspot.com"
        assert detect_task_handler_host() == expected

    def test_returns_none_when_not_detected(self, monkeypatch):
        """Should return None when not in GAE or Cloud Run."""
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
        monkeypatch.delenv("GAE_APPLICATION", raising=False)
        monkeypatch.delenv("K_SERVICE", raising=False)
        monkeypatch.delenv("GAE_SERVICE", raising=False)
        assert detect_task_handler_host() is None


class TestIsCloudRun:
    """Tests for is_cloud_run()."""

    def test_returns_true_on_cloud_run(self, monkeypatch):
        """Should return True when K_SERVICE is set."""
        monkeypatch.setenv("K_SERVICE", "my-service")
        assert is_cloud_run() is True

    def test_returns_false_when_not_cloud_run(self, monkeypatch):
        """Should return False when K_SERVICE is not set."""
        monkeypatch.delenv("K_SERVICE", raising=False)
        assert is_cloud_run() is False


class TestIsAppEngine:
    """Tests for is_app_engine()."""

    def test_returns_true_on_app_engine(self, monkeypatch):
        """Should return True when GAE_SERVICE is set."""
        monkeypatch.setenv("GAE_SERVICE", "default")
        assert is_app_engine() is True

    def test_returns_false_when_not_app_engine(self, monkeypatch):
        """Should return False when GAE_SERVICE is not set."""
        monkeypatch.delenv("GAE_SERVICE", raising=False)
        assert is_app_engine() is False
