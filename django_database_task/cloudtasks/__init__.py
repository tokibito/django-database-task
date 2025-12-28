"""
Cloud Tasks integration for django-database-task.

This module provides a Cloud Tasks backend that automatically creates
Cloud Tasks when tasks are enqueued, enabling serverless task execution
on Google App Engine and Cloud Run.

Installation:
    pip install django-database-task[cloudtasks]

Minimal configuration (GAE/Cloud Run with auto-detection):
    TASKS = {
        "default": {
            "BACKEND": "django_database_task.cloudtasks.CloudTasksDatabaseBackend",
            "OPTIONS": {
                "CLOUD_TASKS_QUEUE": "default",
            },
        },
    }

With OIDC authentication (automatic when OIDC_SERVICE_ACCOUNT_EMAIL is set):
    TASKS = {
        "default": {
            "BACKEND": "django_database_task.cloudtasks.CloudTasksDatabaseBackend",
            "OPTIONS": {
                "CLOUD_TASKS_QUEUE": "default",
                "OIDC_SERVICE_ACCOUNT_EMAIL": "my-sa@project.iam.gserviceaccount.com",
            },
        },
    }

    The backend automatically verifies OIDC tokens on the /tasks/execute/ endpoint.

For more information, see the documentation.
"""

# Detection utilities don't require google-cloud-tasks
from .detection import (
    detect_default_service_account,
    detect_gcp_location,
    detect_gcp_project,
    detect_task_handler_host,
    is_app_engine,
    is_cloud_run,
)


def __getattr__(name):
    """
    Lazy import for classes that require google-cloud-tasks.

    This allows importing detection utilities without installing
    the google-cloud-tasks package.
    """
    if name == "CloudTasksDatabaseBackend":
        from .backend import CloudTasksDatabaseBackend

        return CloudTasksDatabaseBackend
    elif name == "verify_cloud_tasks_oidc":
        from .auth import verify_cloud_tasks_oidc

        return verify_cloud_tasks_oidc
    elif name == "create_oidc_auth_handler":
        from .auth import create_oidc_auth_handler

        return create_oidc_auth_handler
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    # Backend
    "CloudTasksDatabaseBackend",
    # Authentication
    "verify_cloud_tasks_oidc",
    "create_oidc_auth_handler",
    # Detection utilities
    "detect_gcp_project",
    "detect_gcp_location",
    "detect_task_handler_host",
    "detect_default_service_account",
    "is_cloud_run",
    "is_app_engine",
]
