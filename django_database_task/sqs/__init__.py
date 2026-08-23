"""
Amazon SQS integration for django-database-task.

Sends a message holding the task id whenever a task is saved, and lets
run_database_tasks receive those messages. The task itself stays in the
database.

Installation:
    pip install django-database-task[sqs]

Configuration:
    TASKS = {
        "default": {
            "BACKEND": "django_database_task.sqs.SQSDatabaseBackend",
            "OPTIONS": {
                # Detected from AWS_REGION when unset
                # "AWS_REGION": "ap-northeast-1",
            },
        },
    }

Run the worker with:
    python manage.py run_database_tasks --continuous

For more information, see the documentation.
"""

# Detection utilities don't require boto3
from .detection import detect_aws_region, is_ecs, is_lambda


def __getattr__(name):
    """
    Lazy import for classes that require boto3.

    This allows importing detection utilities without installing boto3.
    """
    if name == "SQSDatabaseBackend":
        from .backend import SQSDatabaseBackend

        return SQSDatabaseBackend
    elif name == "SQSBroker":
        from .broker import SQSBroker

        return SQSBroker
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    # Backend
    "SQSDatabaseBackend",
    # Broker
    "SQSBroker",
    # Detection utilities
    "detect_aws_region",
    "is_ecs",
    "is_lambda",
]
