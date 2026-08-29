"""Tests for PostgresNotifyBroker."""

import json
from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone

from django_database_task.postgres import PostgresNotifyBroker

from .conftest import FakeDjangoConnection, FakeListenConnection, make_task_result


def make_broker(connections, listen_connection=None, **options):
    """Build a broker whose two connections are fakes."""
    broker = PostgresNotifyBroker(backend=None, options=options)
    # Set explicitly rather than detected, so the tests cover both driver
    # paths whichever one happens to be installed.
    broker.is_psycopg3 = True
    if listen_connection is not None:
        broker.connect = lambda: listen_connection
    return broker


class TestPostgresNotifyBrokerInit:
    """Tests for the configuration the broker reads."""

    def test_uses_a_default_channel(self, connections):
        assert make_broker(connections).channel == "django_database_task"

    def test_the_channel_option_wins(self, connections):
        assert make_broker(connections, CHANNEL="tasks").channel == "tasks"

    def test_rejects_a_channel_postgresql_cannot_hold(self, connections):
        with pytest.raises(ImproperlyConfigured, match="longer than 63 bytes"):
            make_broker(connections, CHANNEL="x" * 64)

    def test_the_length_limit_counts_bytes(self, connections):
        """A multibyte channel name fits in fewer characters, not fewer bytes."""
        with pytest.raises(ImproperlyConfigured, match="longer than 63 bytes"):
            make_broker(connections, CHANNEL="あ" * 22)

    def test_defaults_to_the_database_the_tasks_are_in(self, connections):
        assert make_broker(connections).database == "default"

    def test_the_database_option_wins(self, connections, django_connection):
        connections["tasks"] = django_connection

        assert make_broker(connections, DATABASE="tasks").database == "tasks"

    def test_rejects_a_database_that_is_not_postgresql(self, connections):
        connections["default"] = FakeDjangoConnection(vendor="sqlite")

        with pytest.raises(ImproperlyConfigured, match="database is sqlite"):
            make_broker(connections)


class TestPostgresNotifyBrokerEnqueue:
    """Tests for notifying the channel when a task is saved."""

    def test_sends_the_task_id(self, connections, django_connection):
        broker = make_broker(connections)

        broker.notify(make_task_result(task_id="abc-123"))

        sql, params = django_connection.executed[0]
        assert sql == "SELECT pg_notify(%s, %s)"
        assert params[0] == "django_database_task"
        assert json.loads(params[1]) == {"task_id": "abc-123", "queue_name": "default"}

    def test_sends_the_queue_name(self, connections, django_connection):
        broker = make_broker(connections)

        broker.notify(make_task_result(queue_name="ranking"))

        _sql, params = django_connection.executed[0]
        assert json.loads(params[1])["queue_name"] == "ranking"

    def test_notifies_the_configured_channel(self, connections, django_connection):
        broker = make_broker(connections, CHANNEL="tasks")

        broker.notify(make_task_result())

        _sql, params = django_connection.executed[0]
        assert params[0] == "tasks"

    def test_a_task_due_now_is_notified(self, connections, django_connection):
        broker = make_broker(connections)

        payload = broker.notify(
            make_task_result(run_after=timezone.now() - timedelta(minutes=1))
        )

        assert payload is not None
        assert django_connection.executed

    def test_a_deferred_task_is_left_for_the_database_sweep(
        self, connections, django_connection
    ):
        """A notification cannot be held back, so it would run the task early."""
        broker = make_broker(connections)

        payload = broker.notify(
            make_task_result(run_after=timezone.now() + timedelta(hours=3))
        )

        assert payload is None
        assert django_connection.executed == []


class TestPostgresNotifyBrokerMessages:
    """Tests for reading a notification payload."""

    def test_reads_the_task_id(self, connections):
        broker = make_broker(connections)

        message = broker.to_message('{"task_id": "abc-123", "queue_name": "default"}')

        assert message.task_id == "abc-123"

    def test_accepts_a_payload_that_is_only_an_id(self, connections):
        """So a NOTIFY sent by hand or from a trigger is understood too."""
        broker = make_broker(connections)

        assert broker.to_message(" abc-123 ").task_id == "abc-123"

    def test_discards_a_payload_naming_no_task(self, connections, caplog):
        broker = make_broker(connections)

        assert broker.to_message('{"queue_name": "default"}') is None
        assert "no task id" in caplog.text

    def test_discards_an_empty_payload(self, connections):
        assert make_broker(connections).to_message("") is None

    def test_a_worker_without_a_queue_takes_every_task(self, connections):
        broker = make_broker(connections)

        message = broker.to_message('{"task_id": "abc", "queue_name": "ranking"}')

        assert message.task_id == "abc"

    def test_a_worker_on_a_queue_takes_only_its_own(self, connections):
        broker = make_broker(connections)
        payload = '{"task_id": "abc", "queue_name": "ranking"}'

        assert broker.to_message(payload, queue_name="ranking") is not None
        assert broker.to_message(payload, queue_name="emails") is None

    def test_a_bare_id_names_no_queue_so_a_filtered_worker_skips_it(self, connections):
        broker = make_broker(connections)

        assert broker.to_message("abc-123", queue_name="ranking") is None


class TestPostgresNotifyBrokerReceive:
    """Tests for waiting on the channel."""

    def test_returns_a_waiting_notification(self, connections):
        listen_connection = FakeListenConnection()
        listen_connection.deliver('{"task_id": "abc", "queue_name": "default"}')
        broker = make_broker(connections, listen_connection)

        messages = broker.receive(wait_seconds=0)

        assert [m.task_id for m in messages] == ["abc"]

    def test_starts_listening_on_the_channel(self, connections):
        listen_connection = FakeListenConnection()
        broker = make_broker(connections, listen_connection, CHANNEL="my tasks")

        broker.receive(wait_seconds=0)

        assert listen_connection.executed == [('LISTEN "my tasks"', None)]
        assert listen_connection.autocommit is True

    def test_the_connection_is_opened_once(self, connections):
        listen_connection = FakeListenConnection()
        broker = make_broker(connections, listen_connection)

        broker.receive(wait_seconds=0)
        broker.receive(wait_seconds=0)

        assert broker.get_connection() is listen_connection

    def test_returns_nothing_when_the_channel_is_quiet(self, connections):
        broker = make_broker(connections, FakeListenConnection())

        assert broker.receive(wait_seconds=0) == []

    def test_waits_until_a_notification_arrives(self, connections):
        """The wait ends on the notification, not on the timeout."""
        import threading

        listen_connection = FakeListenConnection()
        broker = make_broker(connections, listen_connection)
        # Opened first, so the timer does not race the connect.
        broker.get_connection()
        timer = threading.Timer(0.1, listen_connection.deliver, ['{"task_id": "abc"}'])
        timer.start()

        try:
            messages = broker.receive(wait_seconds=30)
        finally:
            timer.cancel()

        assert [m.task_id for m in messages] == ["abc"]

    def test_returns_at_most_max_messages(self, connections):
        listen_connection = FakeListenConnection()
        for i in range(3):
            listen_connection.deliver(json.dumps({"task_id": f"task-{i}"}))
        broker = make_broker(connections, listen_connection)

        messages = broker.receive(max_messages=2, wait_seconds=0)

        assert [m.task_id for m in messages] == ["task-0", "task-1"]

    def test_the_notifications_left_over_come_back_next_time(self, connections):
        """Stopping at max_messages must not drop the rest."""
        listen_connection = FakeListenConnection()
        for i in range(3):
            listen_connection.deliver(json.dumps({"task_id": f"task-{i}"}))
        broker = make_broker(connections, listen_connection)

        broker.receive(max_messages=2, wait_seconds=0)
        messages = broker.receive(max_messages=2, wait_seconds=0)

        assert [m.task_id for m in messages] == ["task-2"]

    def test_a_notification_for_another_queue_is_not_returned(self, connections):
        listen_connection = FakeListenConnection()
        listen_connection.deliver('{"task_id": "abc", "queue_name": "emails"}')
        broker = make_broker(connections, listen_connection)

        assert broker.receive(queue_name="ranking", wait_seconds=0) == []

    def test_a_shutdown_request_ends_the_wait(self, connections):
        from django_database_task.shutdown import GracefulShutdown

        broker = make_broker(connections, FakeListenConnection())

        with GracefulShutdown() as shutdown:
            shutdown.set()
            messages = broker.receive(wait_seconds=30)

        assert messages == []

    def test_a_broken_connection_is_thrown_away(self, connections):
        """So the next call reconnects and starts listening again."""
        listen_connection = FakeListenConnection()
        broker = make_broker(connections, listen_connection)
        broker.get_connection()

        def explode():
            raise OSError("connection lost")

        listen_connection.pgconn.consume_input = explode

        with pytest.raises(OSError, match="connection lost"):
            broker.receive(wait_seconds=0)

        assert broker._connection is None
        assert listen_connection.closed is True

    def test_a_closed_connection_is_reopened(self, connections):
        first, second = FakeListenConnection(), FakeListenConnection()
        opened = iter([first, second])
        broker = make_broker(connections)
        broker.connect = lambda: next(opened)

        broker.receive(wait_seconds=0)
        first.closed = True

        assert broker.get_connection() is second

    def test_connects_with_the_settings_django_uses(
        self, connections, django_connection, monkeypatch
    ):
        pytest.importorskip("psycopg")
        from django.db.backends.postgresql import base

        connect = MagicMock(return_value=FakeListenConnection())
        monkeypatch.setattr(base.Database, "connect", connect)
        broker = make_broker(connections)

        broker.receive(wait_seconds=0)

        connect.assert_called_once_with(**django_connection.params)

    def test_reads_notifications_from_psycopg2(self, connections):
        """The other driver hands them over as a list on the connection."""
        broker = make_broker(connections)
        broker.is_psycopg3 = False

        connection = MagicMock()
        connection.notifies = [MagicMock(payload='{"task_id": "abc"}')]

        assert list(broker.drain(connection)) == ['{"task_id": "abc"}']
        connection.poll.assert_called_once_with()
        assert connection.notifies == []


class TestPostgresNotifyBrokerAcknowledgement:
    """Tests for what happens after a task has been run."""

    def test_ack_does_nothing(self, connections):
        """A notification is not redelivered, so there is nothing to delete."""
        broker = make_broker(connections, FakeListenConnection())
        message = broker.to_message('{"task_id": "abc"}')

        assert broker.ack(message) is None

    def test_nack_does_nothing(self, connections):
        """The task stays READY, so the database sweep runs it again."""
        broker = make_broker(connections, FakeListenConnection())
        message = broker.to_message('{"task_id": "abc"}')

        assert broker.nack(message) is None

    def test_close_drops_the_connection(self, connections):
        listen_connection = FakeListenConnection()
        broker = make_broker(connections, listen_connection)
        broker.get_connection()

        broker.close()

        assert listen_connection.closed is True
        assert broker._connection is None

    def test_close_without_a_connection_is_harmless(self, connections):
        assert make_broker(connections).close() is None


class TestPostgresNotifyBrokerConnecting:
    """Tests for opening the connection the broker listens on."""

    def test_a_connection_that_cannot_listen_is_not_left_open(self, connections):
        listen_connection = FakeListenConnection()

        def explode(*args, **kwargs):
            raise OSError("LISTEN was refused")

        listen_connection.cursor = explode
        broker = make_broker(connections, listen_connection)

        with pytest.raises(OSError, match="LISTEN was refused"):
            broker.receive(wait_seconds=0)

        assert listen_connection.closed is True
