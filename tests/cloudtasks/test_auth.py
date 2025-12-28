"""Tests for OIDC authentication."""

from unittest.mock import patch

import pytest
from django.http import JsonResponse
from django.test import RequestFactory

# Skip all tests if google-cloud-tasks is not installed
pytest.importorskip("google.cloud.tasks_v2")


class TestCreateOidcAuthHandler:
    """Tests for create_oidc_auth_handler function."""

    @pytest.fixture
    def request_factory(self):
        return RequestFactory()

    def test_rejects_missing_authorization_header(self, request_factory):
        """Should reject requests without Authorization header."""
        from django_database_task.cloudtasks import create_oidc_auth_handler

        handler = create_oidc_auth_handler("https://example.com")
        request = request_factory.post("/tasks/execute/123/")
        response = handler(request)

        assert response is not None
        assert response.status_code == 401
        assert b"Missing or invalid Authorization header" in response.content

    def test_rejects_invalid_authorization_format(self, request_factory):
        """Should reject non-Bearer authorization."""
        from django_database_task.cloudtasks import create_oidc_auth_handler

        handler = create_oidc_auth_handler("https://example.com")
        request = request_factory.post(
            "/tasks/execute/123/", HTTP_AUTHORIZATION="Basic dXNlcjpwYXNz"
        )
        response = handler(request)

        assert response is not None
        assert response.status_code == 401

    def test_rejects_invalid_token(self, request_factory):
        """Should reject invalid OIDC token."""
        from django_database_task.cloudtasks import create_oidc_auth_handler

        handler = create_oidc_auth_handler("https://example.com")
        request = request_factory.post(
            "/tasks/execute/123/", HTTP_AUTHORIZATION="Bearer invalid-token"
        )

        with patch(
            "django_database_task.cloudtasks.auth.id_token.verify_oauth2_token"
        ) as mock_verify:
            mock_verify.side_effect = ValueError("Invalid token")
            response = handler(request)

        assert response is not None
        assert response.status_code == 401
        assert b"Invalid token" in response.content

    def test_accepts_valid_token(self, request_factory):
        """Should accept valid OIDC token and return None."""
        from django_database_task.cloudtasks import create_oidc_auth_handler

        handler = create_oidc_auth_handler("https://example.com")
        request = request_factory.post(
            "/tasks/execute/123/", HTTP_AUTHORIZATION="Bearer valid-token"
        )

        mock_claims = {
            "iss": "https://accounts.google.com",
            "email": "cloudtasks@example.iam.gserviceaccount.com",
        }

        with patch(
            "django_database_task.cloudtasks.auth.id_token.verify_oauth2_token"
        ) as mock_verify:
            mock_verify.return_value = mock_claims
            response = handler(request)

        assert response is None
        assert request.cloud_tasks_claims == mock_claims

    def test_rejects_invalid_issuer(self, request_factory):
        """Should reject tokens with invalid issuer."""
        from django_database_task.cloudtasks import create_oidc_auth_handler

        handler = create_oidc_auth_handler("https://example.com")
        request = request_factory.post(
            "/tasks/execute/123/", HTTP_AUTHORIZATION="Bearer valid-token"
        )

        mock_claims = {
            "iss": "https://evil.example.com",
            "email": "attacker@evil.com",
        }

        with patch(
            "django_database_task.cloudtasks.auth.id_token.verify_oauth2_token"
        ) as mock_verify:
            mock_verify.return_value = mock_claims
            response = handler(request)

        assert response is not None
        assert response.status_code == 401
        assert b"Invalid issuer" in response.content


class TestVerifyCloudTasksOIDC:
    """Tests for verify_cloud_tasks_oidc decorator."""

    @pytest.fixture
    def request_factory(self):
        return RequestFactory()

    def test_rejects_missing_authorization_header(self, request_factory, settings):
        """Should reject requests without Authorization header."""
        from django_database_task.cloudtasks import verify_cloud_tasks_oidc

        @verify_cloud_tasks_oidc(audience="https://example.com")
        def my_view(request):
            return JsonResponse({"status": "ok"})

        request = request_factory.post("/tasks/execute/123/")
        response = my_view(request)

        assert response.status_code == 401
        assert b"Missing or invalid Authorization header" in response.content

    def test_rejects_invalid_authorization_format(self, request_factory):
        """Should reject non-Bearer authorization."""
        from django_database_task.cloudtasks import verify_cloud_tasks_oidc

        @verify_cloud_tasks_oidc(audience="https://example.com")
        def my_view(request):
            return JsonResponse({"status": "ok"})

        request = request_factory.post(
            "/tasks/execute/123/", HTTP_AUTHORIZATION="Basic dXNlcjpwYXNz"
        )
        response = my_view(request)

        assert response.status_code == 401

    def test_rejects_invalid_token(self, request_factory):
        """Should reject invalid OIDC token."""
        from django_database_task.cloudtasks import verify_cloud_tasks_oidc

        @verify_cloud_tasks_oidc(audience="https://example.com")
        def my_view(request):
            return JsonResponse({"status": "ok"})

        request = request_factory.post(
            "/tasks/execute/123/", HTTP_AUTHORIZATION="Bearer invalid-token"
        )

        with patch(
            "django_database_task.cloudtasks.auth.id_token.verify_oauth2_token"
        ) as mock_verify:
            mock_verify.side_effect = ValueError("Invalid token")
            response = my_view(request)

        assert response.status_code == 401
        assert b"Invalid token" in response.content

    def test_accepts_valid_token(self, request_factory):
        """Should accept valid OIDC token."""
        from django_database_task.cloudtasks import verify_cloud_tasks_oidc

        @verify_cloud_tasks_oidc(audience="https://example.com")
        def my_view(request):
            return JsonResponse(
                {"status": "ok", "email": request.cloud_tasks_claims.get("email")}
            )

        request = request_factory.post(
            "/tasks/execute/123/", HTTP_AUTHORIZATION="Bearer valid-token"
        )

        mock_claims = {
            "iss": "https://accounts.google.com",
            "email": "cloudtasks@example.iam.gserviceaccount.com",
        }

        with patch(
            "django_database_task.cloudtasks.auth.id_token.verify_oauth2_token"
        ) as mock_verify:
            mock_verify.return_value = mock_claims
            response = my_view(request)

        assert response.status_code == 200
        assert b"ok" in response.content

    def test_rejects_invalid_issuer(self, request_factory):
        """Should reject tokens with invalid issuer."""
        from django_database_task.cloudtasks import verify_cloud_tasks_oidc

        @verify_cloud_tasks_oidc(audience="https://example.com")
        def my_view(request):
            return JsonResponse({"status": "ok"})

        request = request_factory.post(
            "/tasks/execute/123/", HTTP_AUTHORIZATION="Bearer valid-token"
        )

        mock_claims = {
            "iss": "https://evil.example.com",
            "email": "attacker@evil.com",
        }

        with patch(
            "django_database_task.cloudtasks.auth.id_token.verify_oauth2_token"
        ) as mock_verify:
            mock_verify.return_value = mock_claims
            response = my_view(request)

        assert response.status_code == 401
        assert b"Invalid issuer" in response.content

    def test_uses_settings_audience(self, request_factory, settings):
        """Should use CLOUD_TASKS_OIDC_AUDIENCE from settings."""
        settings.CLOUD_TASKS_OIDC_AUDIENCE = "https://from-settings.com"

        from django_database_task.cloudtasks import verify_cloud_tasks_oidc

        @verify_cloud_tasks_oidc()  # No audience parameter
        def my_view(request):
            return JsonResponse({"status": "ok"})

        request = request_factory.post(
            "/tasks/execute/123/", HTTP_AUTHORIZATION="Bearer valid-token"
        )

        mock_claims = {
            "iss": "https://accounts.google.com",
        }

        with patch(
            "django_database_task.cloudtasks.auth.id_token.verify_oauth2_token"
        ) as mock_verify:
            mock_verify.return_value = mock_claims
            response = my_view(request)

        assert response.status_code == 200
        mock_verify.assert_called_once()
        call_args = mock_verify.call_args
        assert call_args.kwargs["audience"] == "https://from-settings.com"
