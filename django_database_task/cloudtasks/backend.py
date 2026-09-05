"""
Cloud Tasks Database Backend.

Thin wrapper that attaches CloudTasksBroker to DatabaseTaskBackend, so a
project only has to name this backend in its TASKS setting.
"""

from django_database_task.backends import DatabaseTaskBackend

from .broker import CloudTasksBroker


class CloudTasksDatabaseBackend(DatabaseTaskBackend):
    """
    A task backend that persists tasks in the database
    and creates Cloud Tasks for execution.

    This backend inherits all functionality from DatabaseTaskBackend
    and adds automatic Cloud Tasks creation on enqueue.

    Requires: pip install django-database-task[cloudtasks]

    Minimal configuration (GAE/Cloud Run with auto-detection):
        TASKS = {
            "default": {
                "BACKEND": "django_database_task.cloudtasks.CloudTasksDatabaseBackend",
            },
        }

    The Cloud Tasks queue name is determined by the task's queue_name attribute.
    For example:
        @task(queue_name="high-priority")
        def urgent_task():
            ...
    will use the "high-priority" Cloud Tasks queue.

    Tasks without explicit queue use Django's DEFAULT_TASK_QUEUE_NAME ("default").

    See CloudTasksBroker for the options this backend accepts.
    """

    broker_class = CloudTasksBroker

    @property
    def project(self):
        """GCP project the Cloud Tasks are created in."""
        return self.broker.project

    @property
    def location(self):
        """Region the Cloud Tasks queues live in."""
        return self.broker.location

    @property
    def client(self):
        """The Cloud Tasks client."""
        return self.broker.client
