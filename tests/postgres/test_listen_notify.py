"""
Tests for PostgresNotifyBroker against a real PostgreSQL server.

test_broker.py stands both connections up as fakes, which covers the
decisions the broker makes but not the protocol underneath: whether the
driver really hands a notification over, whether it arrives only once the
transaction commits, whether LISTEN survives between receive() calls. Those
are what this module checks, so it needs a server.

Run the suite with DJANGO_DATABASE_ENGINE=postgresql to include them.
"""

from datetime import timedelta

import pytest
from django.db import connections, transaction
from django.utils import timezone

from django_database_task.postgres import (
    PostgresNotifyBroker,
    PostgresNotifyDatabaseBackend,
)
from tests.tasks import simple_task

from .conftest import make_task_result

if connections["default"].vendor != "postgresql":
    pytest.skip(
        "needs a PostgreSQL database; run the suite with "
        "DJANGO_DATABASE_ENGINE=postgresql",
        allow_module_level=True,
    )

#: A channel of this suite's own, so a listener left over from a project
#: sharing the server does not see the test notifications, and vice versa.
CHANNEL = "django_database_task_tests"

#: How long a test waits for a notification it expects. Long enough that a
#: slow CI runner does not fail the test, and never reached when the
#: notification arrives, which is the case these tests are written for.
WAIT = 5

#: How long a test waits for a notification it expects never to arrive.
#: Paid in full every time, so it is kept short.
QUIET_WAIT = 1

# Real commits are the point: pytest-django's default rolls the test's
# transaction back, and PostgreSQL delivers no notification for a
# transaction that rolled back.
pytestmark = pytest.mark.django_db(transaction=True)


class Rollback(Exception):
    """Raised to roll an atomic block back without failing the test."""


@pytest.fixture
def broker():
    """A broker listening on the test channel, closed when the test ends."""
    broker = PostgresNotifyBroker(backend=None, options={"CHANNEL": CHANNEL})
    # Listening before the test notifies anything: a notification only
    # reaches the connections that are listening when it is sent.
    broker.get_connection()
    try:
        yield broker
    finally:
        broker.close()


def task_ids(messages):
    return [message.task_id for message in messages]


def backend_pid(connection):
    """The PostgreSQL process a connection is served by."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid()")
        return cursor.fetchone()[0]


class TestDelivery:
    """Tests for a notification travelling from enqueue() to receive()."""

    def test_a_listener_receives_the_task_id(self, broker):
        broker.notify(make_task_result(task_id="abc-123"))

        assert task_ids(broker.receive(wait_seconds=WAIT)) == ["abc-123"]

    def test_the_queue_name_travels_with_it(self, broker):
        broker.notify(make_task_result(queue_name="reports"))

        messages = broker.receive(queue_name="reports", wait_seconds=WAIT)

        assert len(messages) == 1

    def test_a_worker_on_another_queue_ignores_it(self, broker):
        broker.notify(make_task_result(queue_name="reports"))

        assert broker.receive(queue_name="default", wait_seconds=QUIET_WAIT) == []

    def test_nothing_comes_back_from_a_quiet_channel(self, broker):
        assert broker.receive(wait_seconds=QUIET_WAIT) == []

    def test_a_deferred_task_is_not_announced(self, broker):
        run_after = timezone.now() + timedelta(hours=1)

        assert broker.notify(make_task_result(run_after=run_after)) is None
        assert broker.receive(wait_seconds=QUIET_WAIT) == []

    def test_a_channel_name_that_needs_quoting_works(self):
        """LISTEN takes an identifier, so a mixed-case name has to be quoted."""
        broker = PostgresNotifyBroker(
            backend=None, options={"CHANNEL": 'Tasks "needing" quotes'}
        )
        broker.get_connection()
        try:
            broker.notify(make_task_result(task_id="quoted-1"))

            assert task_ids(broker.receive(wait_seconds=WAIT)) == ["quoted-1"]
        finally:
            broker.close()


class TestTransactions:
    """Tests for when PostgreSQL hands the notification over."""

    def test_it_arrives_only_once_the_transaction_commits(self, broker):
        with transaction.atomic():
            broker.notify(make_task_result(task_id="committed-1"))

            assert broker.receive(wait_seconds=QUIET_WAIT) == []

        assert task_ids(broker.receive(wait_seconds=WAIT)) == ["committed-1"]

    def test_a_rolled_back_task_is_never_announced(self, broker):
        with pytest.raises(Rollback):
            with transaction.atomic():
                broker.notify(make_task_result(task_id="rolled-back-1"))
                raise Rollback

        assert broker.receive(wait_seconds=QUIET_WAIT) == []


class TestListening:
    """Tests for the connection the broker waits on."""

    def test_notifications_sent_while_the_worker_was_busy_come_back(self, broker):
        broker.notify(make_task_result(task_id="first"))
        broker.notify(make_task_result(task_id="second"))

        first = broker.receive(max_messages=1, wait_seconds=WAIT)
        second = broker.receive(max_messages=1, wait_seconds=WAIT)

        assert task_ids(first) + task_ids(second) == ["first", "second"]

    def test_it_listens_on_a_connection_of_its_own(self, broker):
        """Not Django's: a connection mid-transaction cannot sit and wait."""
        assert backend_pid(broker.get_connection()) != backend_pid(
            connections["default"]
        )

    def test_a_closed_connection_is_replaced_and_listens_again(self, broker):
        first = broker.get_connection()
        broker.close()

        second = broker.get_connection()
        broker.notify(make_task_result(task_id="after-reconnect"))

        assert second is not first
        assert task_ids(broker.receive(wait_seconds=WAIT)) == ["after-reconnect"]


class TestBackend:
    """Tests for the backend that attaches the broker."""

    def test_saving_a_task_announces_it(self, broker):
        backend = PostgresNotifyDatabaseBackend(
            alias="default", params={"OPTIONS": {"CHANNEL": CHANNEL}}
        )

        result = backend.enqueue(simple_task, (1, 2), {})

        assert task_ids(broker.receive(wait_seconds=WAIT)) == [str(result.id)]
