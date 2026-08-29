"""
PostgreSQL LISTEN/NOTIFY integration for django-database-task.

Notifies a PostgreSQL channel with the task id whenever a task is saved,
and lets run_database_tasks wait on that channel instead of polling. The
task itself stays in the database, in the same database the notification
travels through, so there is no queue to create and no extra service to
run.

Installation:
    Nothing beyond the PostgreSQL driver the project already needs. The
    `postgres` extra installs psycopg 3 for a project that has none:

        pip install django-database-task[postgres]

Configuration:
    TASKS = {
        "default": {
            "BACKEND": "django_database_task.postgres.PostgresNotifyDatabaseBackend",
            "OPTIONS": {
                # "CHANNEL": "django_database_task",
            },
        },
    }

Run the worker with:
    python manage.py run_database_tasks --continuous

For more information, see the documentation.
"""

from .backend import PostgresNotifyDatabaseBackend
from .broker import DEFAULT_CHANNEL, PostgresNotifyBroker

__all__ = [
    # Backend
    "PostgresNotifyDatabaseBackend",
    # Broker
    "PostgresNotifyBroker",
    "DEFAULT_CHANNEL",
]
