"""Tests for the reusable authentication handlers."""

import time

import pytest
from django.contrib.auth.models import AnonymousUser, User
from django.core.exceptions import ImproperlyConfigured
from django.http import JsonResponse
from django.test import RequestFactory

from django_database_task.auth import (
    AUTH_ENDPOINTS,
    HMACAuth,
    SharedSecretAuth,
    StaffOnlyAuth,
    build_signature,
    load_auth_handlers,
)


@pytest.fixture
def request_factory():
    return RequestFactory()


class TestSharedSecretAuth:
    """Tests for SharedSecretAuth."""

    def test_accepts_a_valid_bearer_token(self, request_factory):
        handler = SharedSecretAuth({"TOKEN": "s3cret"})
        request = request_factory.get("/tasks/run/", HTTP_AUTHORIZATION="Bearer s3cret")

        assert handler(request) is None

    def test_rejects_a_missing_header(self, request_factory):
        handler = SharedSecretAuth({"TOKEN": "s3cret"})

        response = handler(request_factory.get("/tasks/run/"))

        assert response.status_code == 401
        assert "Missing Authorization header" in response.content.decode()

    def test_rejects_a_wrong_scheme(self, request_factory):
        handler = SharedSecretAuth({"TOKEN": "s3cret"})
        request = request_factory.get("/tasks/run/", HTTP_AUTHORIZATION="Token s3cret")

        response = handler(request)

        assert response.status_code == 401
        assert "Invalid Authorization header" in response.content.decode()

    def test_rejects_a_wrong_token(self, request_factory):
        handler = SharedSecretAuth({"TOKEN": "s3cret"})
        request = request_factory.get("/tasks/run/", HTTP_AUTHORIZATION="Bearer nope")

        response = handler(request)

        assert response.status_code == 401
        assert "Invalid token" in response.content.decode()

    def test_reads_a_custom_header_without_a_scheme(self, request_factory):
        handler = SharedSecretAuth(
            {"TOKEN": "s3cret", "HEADER": "X-Task-Token", "SCHEME": ""}
        )
        request = request_factory.get("/tasks/run/", HTTP_X_TASK_TOKEN="s3cret")

        assert handler(request) is None

    def test_reads_the_token_from_a_setting(self, request_factory, settings):
        settings.TASK_API_TOKEN = "from-settings"
        handler = SharedSecretAuth({"TOKEN_SETTING": "TASK_API_TOKEN"})
        request = request_factory.get(
            "/tasks/run/", HTTP_AUTHORIZATION="Bearer from-settings"
        )

        assert handler(request) is None

    def test_reads_the_token_from_the_environment(self, request_factory, monkeypatch):
        monkeypatch.setenv("TASK_API_TOKEN", "from-env")
        handler = SharedSecretAuth({"TOKEN_ENV": "TASK_API_TOKEN"})
        request = request_factory.get(
            "/tasks/run/", HTTP_AUTHORIZATION="Bearer from-env"
        )

        assert handler(request) is None

    def test_requires_a_configured_token(self, request_factory):
        handler = SharedSecretAuth({})

        with pytest.raises(ImproperlyConfigured, match="requires one of TOKEN"):
            handler(request_factory.get("/tasks/run/"))

    def test_reports_a_missing_setting(self, request_factory):
        handler = SharedSecretAuth({"TOKEN_SETTING": "DOES_NOT_EXIST"})

        with pytest.raises(ImproperlyConfigured, match="DOES_NOT_EXIST"):
            handler(request_factory.get("/tasks/run/"))

    def test_reports_a_missing_environment_variable(self, request_factory, monkeypatch):
        monkeypatch.delenv("DOES_NOT_EXIST", raising=False)
        handler = SharedSecretAuth({"TOKEN_ENV": "DOES_NOT_EXIST"})

        with pytest.raises(ImproperlyConfigured, match="DOES_NOT_EXIST"):
            handler(request_factory.get("/tasks/run/"))


def _signed_request(
    request_factory, secret, body=b"", timestamp=None, path="/tasks/run/"
):
    timestamp = str(int(time.time())) if timestamp is None else str(timestamp)
    signature = build_signature(secret, timestamp, "POST", path, body)
    return request_factory.post(
        path,
        data=body,
        content_type="application/json",
        HTTP_X_TASK_SIGNATURE=signature,
        HTTP_X_TASK_TIMESTAMP=timestamp,
    )


class TestHMACAuth:
    """Tests for HMACAuth."""

    def test_accepts_a_valid_signature(self, request_factory):
        handler = HMACAuth({"SECRET": "s3cret"})
        request = _signed_request(request_factory, "s3cret", b'{"max_tasks": 5}')

        assert handler(request) is None

    def test_rejects_a_missing_signature(self, request_factory):
        handler = HMACAuth({"SECRET": "s3cret"})

        response = handler(request_factory.post("/tasks/run/"))

        assert response.status_code == 401
        assert "Missing X-Task-Signature header" in response.content.decode()

    def test_rejects_a_missing_timestamp(self, request_factory):
        handler = HMACAuth({"SECRET": "s3cret"})
        request = request_factory.post("/tasks/run/", HTTP_X_TASK_SIGNATURE="abc")

        response = handler(request)

        assert response.status_code == 401
        assert "Missing X-Task-Timestamp header" in response.content.decode()

    def test_rejects_an_expired_signature(self, request_factory):
        handler = HMACAuth({"SECRET": "s3cret", "MAX_AGE": 60})
        request = _signed_request(
            request_factory, "s3cret", timestamp=int(time.time()) - 120
        )

        response = handler(request)

        assert response.status_code == 401
        assert "expired" in response.content.decode()

    def test_accepts_an_old_signature_when_max_age_is_disabled(self, request_factory):
        handler = HMACAuth({"SECRET": "s3cret", "MAX_AGE": 0})
        request = _signed_request(
            request_factory, "s3cret", timestamp=int(time.time()) - 100_000
        )

        assert handler(request) is None

    def test_rejects_a_non_integer_timestamp(self, request_factory):
        handler = HMACAuth({"SECRET": "s3cret"})
        request = request_factory.post(
            "/tasks/run/",
            HTTP_X_TASK_SIGNATURE="abc",
            HTTP_X_TASK_TIMESTAMP="not-a-number",
        )

        response = handler(request)

        assert response.status_code == 401
        assert "Invalid timestamp" in response.content.decode()

    def test_rejects_a_tampered_body(self, request_factory):
        handler = HMACAuth({"SECRET": "s3cret"})
        timestamp = str(int(time.time()))
        signature = build_signature(
            "s3cret", timestamp, "POST", "/tasks/run/", b'{"max_tasks": 1}'
        )
        request = request_factory.post(
            "/tasks/run/",
            data=b'{"max_tasks": 100}',
            content_type="application/json",
            HTTP_X_TASK_SIGNATURE=signature,
            HTTP_X_TASK_TIMESTAMP=timestamp,
        )

        response = handler(request)

        assert response.status_code == 401
        assert "Invalid signature" in response.content.decode()

    def test_rejects_a_signature_made_with_another_secret(self, request_factory):
        handler = HMACAuth({"SECRET": "s3cret"})
        request = _signed_request(request_factory, "other-secret")

        response = handler(request)

        assert response.status_code == 401

    def test_signature_covers_the_query_string(self, request_factory):
        handler = HMACAuth({"SECRET": "s3cret"})
        timestamp = str(int(time.time()))
        signature = build_signature(
            "s3cret", timestamp, "POST", "/tasks/purge/?days=7", b""
        )
        request = request_factory.post(
            "/tasks/purge/?days=30",
            HTTP_X_TASK_SIGNATURE=signature,
            HTTP_X_TASK_TIMESTAMP=timestamp,
        )

        assert handler(request).status_code == 401

    def test_build_signature_accepts_str_and_bytes(self):
        timestamp = "1700000000"
        assert build_signature(
            "s3cret", timestamp, "post", "/tasks/run/", "body"
        ) == build_signature(b"s3cret", timestamp, "POST", "/tasks/run/", b"body")


@pytest.mark.django_db
class TestStaffOnlyAuth:
    """Tests for StaffOnlyAuth."""

    def test_accepts_a_staff_user(self, request_factory):
        request = request_factory.get("/tasks/run/")
        request.user = User.objects.create_user("staff", password="x", is_staff=True)

        assert StaffOnlyAuth()(request) is None

    def test_rejects_an_anonymous_user(self, request_factory):
        request = request_factory.get("/tasks/run/")
        request.user = AnonymousUser()

        response = StaffOnlyAuth()(request)

        assert response.status_code == 401

    def test_rejects_a_non_staff_user(self, request_factory):
        request = request_factory.get("/tasks/run/")
        request.user = User.objects.create_user("regular", password="x")

        response = StaffOnlyAuth()(request)

        assert response.status_code == 403

    def test_requires_the_authentication_middleware(self, request_factory):
        with pytest.raises(ImproperlyConfigured, match="AuthenticationMiddleware"):
            StaffOnlyAuth()(request_factory.get("/tasks/run/"))


def accept_everything(request):
    """A plain callable used as an authentication handler."""
    return None


class TestLoadAuthHandlers:
    """Tests for building handlers from configuration."""

    def test_returns_nothing_without_configuration(self):
        assert load_auth_handlers(None) == []
        assert load_auth_handlers([]) == []

    def test_builds_a_handler_from_a_dotted_path(self):
        specs = load_auth_handlers(
            ["django_database_task.auth.SharedSecretAuth"],
            {"TOKEN": "s3cret"},
        )

        assert len(specs) == 1
        handler, endpoints = specs[0]
        assert isinstance(handler, SharedSecretAuth)
        assert handler.options == {"TOKEN": "s3cret"}
        assert endpoints is None

    def test_accepts_a_single_entry_instead_of_a_list(self):
        specs = load_auth_handlers(
            "django_database_task.auth.SharedSecretAuth", {"TOKEN": "s3cret"}
        )

        assert len(specs) == 1

    def test_entry_options_override_the_shared_defaults(self):
        specs = load_auth_handlers(
            [
                {
                    "HANDLER": "django_database_task.auth.SharedSecretAuth",
                    "OPTIONS": {"TOKEN": "specific"},
                }
            ],
            {"TOKEN": "shared", "HEADER": "X-Task-Token"},
        )

        handler, _ = specs[0]
        assert handler.options == {"TOKEN": "specific", "HEADER": "X-Task-Token"}

    def test_reads_the_endpoints_an_entry_applies_to(self):
        specs = load_auth_handlers(
            [
                {
                    "HANDLER": "django_database_task.auth.SharedSecretAuth",
                    "OPTIONS": {"TOKEN": "s3cret"},
                    "ENDPOINTS": ["run", "purge"],
                }
            ]
        )

        _, endpoints = specs[0]
        assert endpoints == frozenset({"run", "purge"})

    def test_rejects_an_unknown_endpoint(self):
        with pytest.raises(ImproperlyConfigured, match="Unknown endpoints"):
            load_auth_handlers(
                [
                    {
                        "HANDLER": "django_database_task.auth.SharedSecretAuth",
                        "ENDPOINTS": ["nope"],
                    }
                ]
            )

    def test_rejects_a_dict_without_a_handler_key(self):
        with pytest.raises(ImproperlyConfigured, match="requires a HANDLER key"):
            load_auth_handlers([{"OPTIONS": {}}])

    def test_accepts_a_plain_callable(self):
        specs = load_auth_handlers([accept_everything])

        assert specs[0][0] is accept_everything

    def test_accepts_an_instance(self):
        handler = SharedSecretAuth({"TOKEN": "s3cret"})

        specs = load_auth_handlers([handler])

        assert specs[0][0] is handler

    def test_rejects_a_non_callable(self):
        with pytest.raises(ImproperlyConfigured, match="is not callable"):
            load_auth_handlers(
                [{"HANDLER": "django_database_task.auth.AUTH_ENDPOINTS"}]
            )

    def test_reports_an_unimportable_path(self):
        with pytest.raises(ImproperlyConfigured, match="Could not import"):
            load_auth_handlers(["does.not.exist.Handler"])


def test_endpoint_names_match_the_views():
    """Every view's auth_endpoint is a known endpoint name."""
    from django_database_task import views

    view_classes = [
        views.RunTasksView,
        views.RunOneTaskView,
        views.TaskStatusView,
        views.ExecuteTaskView,
        views.PurgeCompletedTasksView,
    ]
    names = {view.auth_endpoint for view in view_classes}

    assert names == set(AUTH_ENDPOINTS)


def test_json_response_handlers_are_usable_as_is(request_factory):
    """A handler may simply be a function returning a JsonResponse."""

    def reject(request):
        return JsonResponse({"error": "nope"}, status=401)

    specs = load_auth_handlers([reject])
    handler, _ = specs[0]

    assert handler(request_factory.get("/tasks/run/")).status_code == 401
