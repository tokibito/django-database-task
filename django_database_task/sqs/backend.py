"""
Amazon SQS Database Backend.

Thin wrapper that attaches SQSBroker to DatabaseTaskBackend, so a project
only has to name this backend in its TASKS setting.
"""

from django_database_task.backends import DatabaseTaskBackend

from .broker import SQSBroker


class SQSDatabaseBackend(DatabaseTaskBackend):
    """
    A task backend that persists tasks in the database and sends a message
    to Amazon SQS so a worker picks them up.

    Requires: pip install django-database-task[sqs]

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

    which receives from SQS and sweeps the database, since a task deferred
    beyond the 15 minute SQS delay limit is not sent to the queue.

    See SQSBroker for the options this backend accepts.
    """

    broker_class = SQSBroker
