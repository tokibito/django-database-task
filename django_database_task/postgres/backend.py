"""
PostgreSQL LISTEN/NOTIFY Database Backend.

Thin wrapper that attaches PostgresNotifyBroker to DatabaseTaskBackend, so
a project only has to name this backend in its TASKS setting.
"""

from django_database_task.backends import DatabaseTaskBackend

from .broker import PostgresNotifyBroker


class PostgresNotifyDatabaseBackend(DatabaseTaskBackend):
    """
    A task backend that persists tasks in the database and notifies a
    PostgreSQL channel so a waiting worker picks them up at once.

    Requires nothing beyond the PostgreSQL connection the project already
    uses; the notification travels through it.

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

    which waits on the channel and sweeps the database, since a
    notification only reaches the workers listening at that moment and a
    deferred task is not announced at all.

    See PostgresNotifyBroker for the options this backend accepts.
    """

    broker_class = PostgresNotifyBroker
