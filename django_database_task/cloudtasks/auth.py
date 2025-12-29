"""
OIDC authentication for Cloud Tasks.

Provides authentication handler and decorator for verifying OIDC tokens
sent by Cloud Tasks.
"""

import logging
import traceback
from functools import wraps

from django.http import JsonResponse
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

logger = logging.getLogger("django_database_task.cloudtasks.auth")


def create_oidc_auth_handler(audience):
    """
    Create an OIDC authentication handler for Cloud Tasks.

    This function returns a handler that can be used with the backend's
    get_auth_handler() method to verify OIDC tokens from Cloud Tasks.

    Args:
        audience: The expected audience claim in the token.

    Returns:
        A callable that takes a request and returns:
        - None if authentication succeeds
        - A JsonResponse with error details if authentication fails

    Usage:
        # In CloudTasksDatabaseBackend
        def get_auth_handler(self):
            if self.oidc_audience:
                return create_oidc_auth_handler(self.oidc_audience)
            return None
    """

    def handler(request):
        # Get Authorization header
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JsonResponse(
                {"error": "Missing or invalid Authorization header"}, status=401
            )

        token = auth_header[7:]  # Remove "Bearer " prefix

        try:
            # Verify the token
            claims = id_token.verify_oauth2_token(
                token,
                google_requests.Request(),
                audience=audience,
            )

            # Verify issuer
            issuer = claims.get("iss")
            if issuer not in [
                "https://accounts.google.com",
                "accounts.google.com",
            ]:
                raise ValueError(f"Invalid issuer: {issuer}")

            # Attach claims to request for use in view
            request.cloud_tasks_claims = claims

            # Log successful verification
            service_account = claims.get("email", "unknown")
            logger.info(
                "OIDC token verified successfully: service_account=%s audience=%s",
                service_account,
                audience,
            )

        except Exception as e:
            # Log the full exception for debugging
            # Use print as fallback since logging config may not capture this logger
            error_type = type(e).__name__
            error_msg = str(e) if str(e) else "No details available"
            tb = traceback.format_exc()
            full_error = (
                f"OIDC token verification failed: {error_type}: {error_msg}\n{tb}"
            )
            logger.error(full_error)
            # Also print to stderr for environments where logging is not configured
            import sys

            print(full_error, file=sys.stderr)
            return JsonResponse(
                {"error": f"Invalid token: {error_type}: {error_msg}"}, status=401
            )

        return None

    return handler


def verify_cloud_tasks_oidc(view_func=None, audience=None):
    """
    Decorator to verify OIDC tokens from Cloud Tasks.

    This decorator validates the Authorization header containing
    an OIDC token issued by Cloud Tasks.

    Args:
        view_func: The view function to wrap.
        audience: The expected audience claim in the token.
                  If not provided, uses CLOUD_TASKS_OIDC_AUDIENCE setting.

    Usage:
        @verify_cloud_tasks_oidc(audience="https://myapp.example.com")
        def my_view(request):
            ...

        # Or with class-based views
        path(
            "tasks/execute/<uuid:task_id>/",
            verify_cloud_tasks_oidc(
                ExecuteTaskView.as_view(),
                audience="https://myapp.example.com"
            ),
        ),

    Requires: pip install django-database-task[cloudtasks]
    """

    def decorator(func):
        @wraps(func)
        def wrapper(request, *args, **kwargs):
            from django.conf import settings

            # Get audience from parameter or settings
            aud = audience or getattr(settings, "CLOUD_TASKS_OIDC_AUDIENCE", None)
            if not aud:
                return JsonResponse(
                    {"error": "OIDC audience not configured"}, status=500
                )

            # Use the auth handler
            handler = create_oidc_auth_handler(aud)
            error_response = handler(request)
            if error_response:
                return error_response

            return func(request, *args, **kwargs)

        return wrapper

    if view_func:
        return decorator(view_func)
    return decorator
