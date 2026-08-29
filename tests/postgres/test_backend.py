"""Tests for PostgresNotifyDatabaseBackend."""

import json
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.tasks.base import TaskResultStatus

from django_database_task.models import DatabaseTask
from tests.tasks import simple_task

from .conftest import FakeListenConnection


@pytest.fixture
def listen_connection():
    """The connection the worker would wait on."""
    return FakeListenConnection()


@pytest.fixture
def backend(connections, listen_connection):
    """A backend whose two PostgreSQL connections are fakes."""
    from django_database_task.postgres import PostgresNotifyDatabaseBackend

    backend = PostgresNotifyDatabaseBackend("default", {"QUEUES": [], "OPTIONS": {}})
    backend.broker.is_psycopg3 = True
    backend.broker.connect = lambda: listen_connection
    return backend


class TestPostgresNotifyDatabaseBackend:
    """Tests for the backend wiring."""

    def test_it_uses_the_notify_broker(self, backend):
        from django_database_task.postgres import PostgresNotifyBroker

        assert isinstance(backend.broker, PostgresNotifyBroker)

    def test_it_adds_no_authentication(self, backend):
        """Nothing calls back over HTTP, so there is nothing to verify."""
        assert backend.get_auth_handlers() == []

    def test_options_reach_the_broker(self, connections):
        from django_database_task.postgres import PostgresNotifyDatabaseBackend

        backend = PostgresNotifyDatabaseBackend(
            "default", {"OPTIONS": {"CHANNEL": "tasks"}}
        )

        assert backend.broker.channel == "tasks"


@pytest.mark.django_db
class TestPostgresNotifyDatabaseBackendEnqueue:
    """Tests for saving a task and notifying the channel."""

    def test_enqueue_saves_and_notifies(self, backend, django_connection):
        result = backend.enqueue(simple_task, (2, 3), {})

        assert DatabaseTask.objects.get(id=result.id).status == TaskResultStatus.READY
        _sql, params = django_connection.executed[0]
        assert json.loads(params[1]) == {
            "task_id": str(result.id),
            "queue_name": "default",
        }

    def test_a_failed_notification_does_not_lose_the_task(
        self, backend, django_connection, caplog
    ):
        def explode():
            raise RuntimeError("the connection is gone")

        django_connection.cursor = explode

        result = backend.enqueue(simple_task, (2, 3), {})

        assert DatabaseTask.objects.get(id=result.id).status == TaskResultStatus.READY
        assert "PostgresNotifyBroker failed to enqueue task" in caplog.text


@pytest.mark.django_db
class TestPostgresNotifyWorker:
    """Tests for the worker running what the channel announces."""

    @staticmethod
    def _run(backend, **options):
        out = StringIO()
        with patch.dict(
            "django_database_task.management.commands.run_database_tasks.task_backends",
            {"default": backend},
            clear=False,
        ):
            call_command("run_database_tasks", stdout=out, stderr=out, **options)
        return out.getvalue()

    def test_the_default_source_waits_on_the_channel(self, backend):
        """A configured broker turns the usual command into a listening worker."""
        output = self._run(backend)

        assert "Source: both" in output
        assert "Broker: PostgresNotifyBroker" in output

    def test_a_notification_runs_its_task(self, backend, listen_connection):
        result = backend.enqueue(simple_task, (2, 3), {})
        listen_connection.deliver(
            json.dumps({"task_id": str(result.id), "queue_name": "default"})
        )

        self._run(backend)

        db_task = DatabaseTask.objects.get(id=result.id)
        assert db_task.status == TaskResultStatus.SUCCESSFUL
        assert db_task.return_value_json == 5

    def test_the_worker_closes_the_connection_on_the_way_out(
        self, backend, listen_connection
    ):
        self._run(backend)

        assert listen_connection.closed is True

    def test_a_deferred_task_is_run_from_the_database(self, backend):
        """What the channel could not announce is picked up by the sweep."""
        from datetime import timedelta

        from django.utils import timezone

        result = simple_task.using(
            run_after=timezone.now() - timedelta(seconds=1)
        ).enqueue(4, 4)
        DatabaseTask.objects.filter(id=result.id).update(backend_name="default")

        self._run(backend)

        assert DatabaseTask.objects.get(id=result.id).status == (
            TaskResultStatus.SUCCESSFUL
        )
