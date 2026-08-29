"""Fakes standing in for the two PostgreSQL connections the broker uses."""

import socket
from unittest.mock import MagicMock

import pytest


class FakeCursor:
    """A cursor that records what it was asked to execute."""

    def __init__(self, executed):
        self.executed = executed

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))


class FakeDjangoConnection:
    """Stand-in for the Django connection the broker notifies through."""

    def __init__(self, vendor="postgresql", params=None):
        self.vendor = vendor
        self.executed = []
        self.params = params or {"dbname": "tasks", "host": "localhost"}

    def cursor(self):
        return FakeCursor(self.executed)

    def get_connection_params(self):
        return dict(self.params)


class FakeNotify:
    """Stand-in for psycopg's PGnotify."""

    def __init__(self, payload, channel="django_database_task"):
        self.extra = payload.encode("utf-8")
        self.relname = channel.encode("utf-8")


class FakePGconn:
    """The low-level half of a psycopg 3 connection the broker reads from."""

    def __init__(self, connection):
        self.connection = connection

    def consume_input(self):
        try:
            self.connection.readable.recv(4096)
        except BlockingIOError:
            pass

    def notifies(self):
        if not self.connection.pending:
            return None
        return self.connection.pending.pop(0)


class FakeListenConnection:
    """
    Stand-in for the connection the broker listens on.

    It is backed by a real socket pair, so select() in wait() blocks and
    wakes for the same reasons it does against PostgreSQL.
    """

    def __init__(self):
        self.readable, self.writable = socket.socketpair()
        self.readable.setblocking(False)
        self.pending = []
        self.closed = False
        self.autocommit = False
        self.executed = []
        self.pgconn = FakePGconn(self)

    def deliver(self, payload):
        """Deliver a notification, waking anything waiting on the socket."""
        self.pending.append(FakeNotify(payload))
        self.writable.send(b"\x01")

    def fileno(self):
        return self.readable.fileno()

    def cursor(self):
        return FakeCursor(self.executed)

    def close(self):
        self.closed = True
        self.readable.close()
        self.writable.close()


def make_task_result(
    task_id="3f2a9c11-0000-4000-8000-000000000000", queue_name="default", run_after=None
):
    """A stand-in for the TaskResult the backend hands to the broker."""
    result = MagicMock()
    result.id = task_id
    result.task.queue_name = queue_name
    result.task.run_after = run_after
    return result


@pytest.fixture
def django_connection():
    """The Django connection the broker is pointed at."""
    return FakeDjangoConnection()


@pytest.fixture
def connections(monkeypatch, django_connection):
    """Replace the connection registry the broker looks the database up in."""
    from django_database_task.postgres import broker

    registry = {"default": django_connection}
    monkeypatch.setattr(broker, "connections", registry)
    return registry
