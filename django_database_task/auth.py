"""
Reusable authentication handlers for the task HTTP endpoints.

An authentication handler is any callable that takes a request and returns
``None`` to accept it, or a response to reject it. Backends expose the
handlers they want applied through ``get_auth_handlers()``; the views accept
a request as soon as one handler accepts it.

Handlers are configured in the backend ``OPTIONS``:

    TASKS = {
        "default": {
            "BACKEND": "django_database_task.backends.DatabaseTaskBackend",
            "OPTIONS": {
                "AUTH_HANDLERS": [
                    "django_database_task.auth.SharedSecretAuth",
                ],
                "AUTH_HANDLER_OPTIONS": {"TOKEN_SETTING": "TASK_API_TOKEN"},
            },
        },
    }

Each entry is either a dotted path (or callable) or a dict that carries its
own options and, optionally, the endpoints it applies to:

    "AUTH_HANDLERS": [
        {
            "HANDLER": "django_database_task.auth.SharedSecretAuth",
            "OPTIONS": {"TOKEN_SETTING": "TASK_CRON_TOKEN"},
            "ENDPOINTS": ["run", "run_one", "purge"],
        },
    ]
"""

import hashlib
import hmac
import inspect
import os
import time

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.http import JsonResponse
from django.utils.module_loading import import_string

# Endpoint names passed to get_auth_handlers(). They match the view classes
# in django_database_task.views.
AUTH_ENDPOINTS = frozenset(
    {
        "run",
        "run_one",
        "status",
        "execute",
        "purge",
    }
)


class BaseAuthHandler:
    """
    Base class for configurable authentication handlers.

    Subclasses implement ``authenticate()``. Returning ``None`` accepts the
    request; returning a response rejects it.
    """

    def __init__(self, options=None):
        self.options = options or {}

    def __call__(self, request):
        return self.authenticate(request)

    def authenticate(self, request):
        raise NotImplementedError

    def get_option(self, key, default=None):
        return self.options.get(key, default)

    def resolve_secret(self, value_key, setting_key, env_key):
        """
        Resolve a secret from the options, a Django setting, or the environment.

        Reading the secret from a setting or an environment variable is
        preferred over writing it into ``OPTIONS`` directly.
        """
        value = self.options.get(value_key)
        if value:
            return value

        setting_name = self.options.get(setting_key)
        if setting_name:
            value = getattr(settings, setting_name, None)
            if not value:
                raise ImproperlyConfigured(
                    f"{type(self).__name__}: setting {setting_name!r} "
                    f"(from {setting_key}) is not set."
                )
            return value

        env_name = self.options.get(env_key)
        if env_name:
            value = os.environ.get(env_name)
            if not value:
                raise ImproperlyConfigured(
                    f"{type(self).__name__}: environment variable {env_name!r} "
                    f"(from {env_key}) is not set."
                )
            return value

        raise ImproperlyConfigured(
            f"{type(self).__name__} requires one of {value_key}, "
            f"{setting_key} or {env_key} in AUTH_HANDLER_OPTIONS."
        )


class SharedSecretAuth(BaseAuthHandler):
    """
    Accept a request that carries a shared secret in a header.

    Options:
        TOKEN          The token itself. Prefer TOKEN_SETTING or TOKEN_ENV.
        TOKEN_SETTING  Name of a Django setting holding the token.
        TOKEN_ENV      Name of an environment variable holding the token.
        HEADER         Header to read (default: "Authorization").
        SCHEME         Scheme prefix to strip (default: "Bearer"). Set to an
                       empty string to compare the raw header value.
    """

    def authenticate(self, request):
        expected = self.resolve_secret("TOKEN", "TOKEN_SETTING", "TOKEN_ENV")
        header = self.get_option("HEADER", "Authorization")
        scheme = self.get_option("SCHEME", "Bearer")

        provided = request.headers.get(header)
        if not provided:
            return JsonResponse({"error": f"Missing {header} header"}, status=401)

        if scheme:
            prefix = f"{scheme} "
            if not provided.startswith(prefix):
                return JsonResponse({"error": f"Invalid {header} header"}, status=401)
            provided = provided[len(prefix) :]

        if not hmac.compare_digest(provided, expected):
            return JsonResponse({"error": "Invalid token"}, status=401)

        return None


def build_signature(secret, timestamp, method, path, body=b"", algorithm="sha256"):
    """
    Build the HMAC signature HMACAuth expects.

    The signed payload is ``timestamp\\nmethod\\npath\\nbody``, where ``path``
    is the full path including the query string. Returns a hex digest.

    Use this from the caller (a cron job, a webhook sender) to produce the
    value of the signature header.
    """
    if isinstance(body, str):
        body = body.encode()
    if isinstance(secret, str):
        secret = secret.encode()

    payload = b"\n".join(
        [
            str(timestamp).encode(),
            method.upper().encode(),
            path.encode(),
            body,
        ]
    )
    return hmac.new(secret, payload, getattr(hashlib, algorithm)).hexdigest()


class HMACAuth(BaseAuthHandler):
    """
    Accept a request signed with a shared secret, with replay protection.

    The caller sends a timestamp and a signature built by build_signature().

    Options:
        SECRET            The secret. Prefer SECRET_SETTING or SECRET_ENV.
        SECRET_SETTING    Name of a Django setting holding the secret.
        SECRET_ENV        Name of an environment variable holding the secret.
        HEADER            Signature header (default: "X-Task-Signature").
        TIMESTAMP_HEADER  Timestamp header (default: "X-Task-Timestamp").
        MAX_AGE           Seconds a signature stays valid (default: 300).
                          0 disables the check.
        ALGORITHM         Hash algorithm name (default: "sha256").
    """

    def authenticate(self, request):
        secret = self.resolve_secret("SECRET", "SECRET_SETTING", "SECRET_ENV")
        header = self.get_option("HEADER", "X-Task-Signature")
        timestamp_header = self.get_option("TIMESTAMP_HEADER", "X-Task-Timestamp")
        max_age = self.get_option("MAX_AGE", 300)
        algorithm = self.get_option("ALGORITHM", "sha256")

        provided = request.headers.get(header)
        if not provided:
            return JsonResponse({"error": f"Missing {header} header"}, status=401)

        timestamp = request.headers.get(timestamp_header)
        if not timestamp:
            return JsonResponse(
                {"error": f"Missing {timestamp_header} header"}, status=401
            )

        if max_age:
            try:
                sent_at = int(timestamp)
            except ValueError:
                return JsonResponse({"error": "Invalid timestamp"}, status=401)
            if abs(time.time() - sent_at) > max_age:
                return JsonResponse({"error": "Signature has expired"}, status=401)

        expected = build_signature(
            secret,
            timestamp,
            request.method,
            request.get_full_path(),
            request.body,
            algorithm=algorithm,
        )
        if not hmac.compare_digest(provided, expected):
            return JsonResponse({"error": "Invalid signature"}, status=401)

        return None


class StaffOnlyAuth(BaseAuthHandler):
    """
    Accept a request from a logged in staff user.

    Requires django.contrib.auth's AuthenticationMiddleware.
    """

    def authenticate(self, request):
        user = getattr(request, "user", None)
        if user is None:
            raise ImproperlyConfigured(
                "StaffOnlyAuth requires "
                "django.contrib.auth.middleware.AuthenticationMiddleware."
            )

        if not user.is_authenticated:
            return JsonResponse({"error": "Authentication required"}, status=401)
        if not user.is_staff:
            return JsonResponse({"error": "Staff privileges required"}, status=403)

        return None


def load_auth_handler(entry, default_options=None):
    """
    Build one authentication handler from a configuration entry.

    The entry is a dotted path, a callable, an instance, or a dict with
    HANDLER / OPTIONS / ENDPOINTS keys.

    Returns a (handler, endpoints) tuple, where endpoints is a frozenset of
    endpoint names the handler applies to, or None for all endpoints.
    """
    endpoints = None
    options = dict(default_options or {})

    if isinstance(entry, dict):
        try:
            target = entry["HANDLER"]
        except KeyError:
            raise ImproperlyConfigured(
                "An AUTH_HANDLERS entry given as a dict requires a HANDLER key."
            ) from None
        options.update(entry.get("OPTIONS") or {})
        if entry.get("ENDPOINTS") is not None:
            endpoints = frozenset(entry["ENDPOINTS"])
            unknown = endpoints - AUTH_ENDPOINTS
            if unknown:
                raise ImproperlyConfigured(
                    f"Unknown endpoints in AUTH_HANDLERS: {sorted(unknown)}. "
                    f"Valid names are {sorted(AUTH_ENDPOINTS)}."
                )
    else:
        target = entry

    if isinstance(target, str):
        try:
            target = import_string(target)
        except ImportError as e:
            raise ImproperlyConfigured(
                f"Could not import authentication handler {target!r}: {e}"
            ) from e

    if inspect.isclass(target):
        handler = target(options)
    else:
        handler = target

    if not callable(handler):
        raise ImproperlyConfigured(
            f"Authentication handler {handler!r} is not callable."
        )

    return handler, endpoints


def load_auth_handlers(entries, default_options=None):
    """Build (handler, endpoints) tuples from an AUTH_HANDLERS setting."""
    if not entries:
        return []
    if isinstance(entries, (str, dict)) or callable(entries):
        entries = [entries]
    return [load_auth_handler(entry, default_options) for entry in entries]
