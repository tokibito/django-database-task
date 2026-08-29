"""
PostgreSQL LISTEN/NOTIFY broker.

Sends a notification holding the task id whenever a task is saved, and lets
a worker wait for those notifications with run_database_tasks. Everything
else about the task stays in the database, in the same database the
notification travels through.

Nothing beyond the PostgreSQL connection the project already has is needed:
no queue to create, no credentials to hand out, no extra service to run.
"""

import json
import logging
import select
from functools import cached_property
from time import monotonic

from django.core.exceptions import ImproperlyConfigured
from django.db import connections, router
from django.utils import timezone

from django_database_task.brokers import BrokerMessage, PullBroker
from django_database_task.shutdown import is_shutdown_requested

logger = logging.getLogger(__name__)

#: Channel used when CHANNEL is not configured.
DEFAULT_CHANNEL = "django_database_task"

#: The longest channel name PostgreSQL accepts (NAMEDATALEN - 1).
MAX_CHANNEL_NAME_LENGTH = 63

#: How long a single wait lasts before the loop looks at the shutdown flag
#: again, in seconds. A wait longer than this is split into chunks, so a
#: worker asked to stop does not sit in select() until the whole wait is up.
WAIT_CHUNK_SECONDS = 1.0


def quote_channel(channel):
    """
    Quote a channel name for use as an identifier in LISTEN.

    LISTEN takes an identifier rather than a parameter, so the name is
    quoted here. NOTIFY is not used at all: pg_notify() takes the channel
    as a plain string parameter.
    """
    escaped = channel.replace('"', '""')
    return f'"{escaped}"'


class PostgresNotifyBroker(PullBroker):
    """
    Broker backed by PostgreSQL LISTEN/NOTIFY.

    Saving a task sends ``pg_notify(channel, '{"task_id": ...}')`` on the
    same connection and inside the same transaction as the INSERT, so the
    notification is delivered when — and only when — the transaction that
    created the task commits. A worker waiting on the channel therefore
    never hears about a task it cannot yet see, and never hears about one
    whose transaction was rolled back.

    Options:
        CHANNEL   Name of the channel to notify and listen on
                  (default: "django_database_task"). PostgreSQL limits it
                  to 63 bytes.
        DATABASE  Alias of the database connection to use. Defaults to the
                  one the task rows are written to.

    Queues:
        One channel carries every queue, because LISTEN has no wildcard and
        a worker without --queue has no list of queue names to listen on.
        The queue name travels in the payload instead, and a worker started
        with --queue ignores notifications for the other queues.

    Delivery:
        A notification is broadcast, not handed to one consumer: every
        listening worker receives it and races for the task, and the loser
        finds it already taken by the READY check and the row lock. It is
        also delivered only to workers listening at that moment, so a task
        saved while no worker was connected is never announced. Both are
        covered by the database sweep the worker performs, which is why
        --source resolves to 'both' rather than 'broker'.
    """

    def __init__(self, backend, options=None):
        super().__init__(backend, options)

        self.channel = self.options.get("CHANNEL") or DEFAULT_CHANNEL
        if len(self.channel.encode("utf-8")) > MAX_CHANNEL_NAME_LENGTH:
            raise ImproperlyConfigured(
                f"CHANNEL cannot be longer than {MAX_CHANNEL_NAME_LENGTH} bytes, "
                f"the longest channel name PostgreSQL accepts "
                f"(got {self.channel!r})."
            )

        self.database = self.options.get("DATABASE") or self.get_default_database()
        vendor = connections[self.database].vendor
        if vendor != "postgresql":
            raise ImproperlyConfigured(
                f"The PostgreSQL LISTEN/NOTIFY broker needs a PostgreSQL "
                f"connection, but the {self.database!r} database is {vendor}. "
                f"Set DATABASE in TASKS OPTIONS to the alias the tasks are "
                f"stored in."
            )

        self._connection = None

    def get_default_database(self):
        """Get the alias of the database the task rows are written to."""
        from django_database_task.models import DatabaseTask

        return router.db_for_write(DatabaseTask)

    @cached_property
    def is_psycopg3(self):
        """Whether the installed driver is psycopg 3 rather than psycopg2."""
        from django.db.backends.postgresql.psycopg_any import is_psycopg3

        return is_psycopg3

    # -- enqueue ---------------------------------------------------------

    def enqueue(self, task_result):
        """
        Notify the channel about a task that was just saved.

        A task deferred to a later time is not announced: a notification
        cannot be held back, and a worker acting on it would run the task
        early. It stays in the database for the sweep to run when due.

        Returns:
            The payload that was sent, or None if nothing was sent.
        """
        run_after = task_result.task.run_after
        if run_after and run_after > timezone.now():
            logger.debug(
                "Task %s runs at %s; leaving it in the database for the "
                "worker to pick up when it is due",
                task_result.id,
                run_after,
            )
            return None

        payload = json.dumps(
            {
                "task_id": str(task_result.id),
                "queue_name": task_result.task.queue_name,
            }
        )

        with connections[self.database].cursor() as cursor:
            cursor.execute("SELECT pg_notify(%s, %s)", [self.channel, payload])

        logger.debug("Notified %s about task %s", self.channel, task_result.id)
        return payload

    # -- receive ---------------------------------------------------------

    def receive(self, queue_name=None, max_messages=1, wait_seconds=20):
        """
        Wait on the channel and return the notifications it delivers.

        Notifications that arrived while the worker was busy are waiting on
        the connection and come back straight away, since LISTEN stays in
        effect between calls. Only a connection that was not open at the
        time misses them.
        """
        connection = self.get_connection()
        deadline = monotonic() + max(0.0, float(wait_seconds))
        messages = []

        try:
            while True:
                for payload in self.drain(connection):
                    message = self.to_message(payload, queue_name)
                    if message is not None:
                        messages.append(message)
                        if len(messages) >= max_messages:
                            return messages

                if messages:
                    return messages

                remaining = deadline - monotonic()
                if remaining <= 0 or is_shutdown_requested():
                    return messages

                self.wait(connection, min(remaining, WAIT_CHUNK_SECONDS))
        except Exception:
            # The connection is the only state worth throwing away: the
            # caller reports the error and calls receive() again, which
            # reconnects and re-LISTENs.
            self.close()
            raise

    def ack(self, message):
        """
        Do nothing: a notification is never redelivered, so it needs no
        acknowledgement. The task's own status records that it ran.
        """

    def nack(self, message, delay=None):
        """
        Do nothing: a notification cannot be given back.

        The task is left READY in the database, so the worker's database
        sweep runs it again.
        """

    def close(self):
        """Close the listening connection, dropping the LISTEN with it."""
        connection, self._connection = self._connection, None
        if connection is None:
            return
        try:
            connection.close()
        except Exception:
            logger.debug("Failed to close the listening connection", exc_info=True)

    # -- connection ------------------------------------------------------

    def get_connection(self):
        """
        Get the connection this broker listens on, opening it if needed.

        It is a connection of its own, separate from the one Django runs
        queries on: it has to sit idle in autocommit waiting for
        notifications, which a connection in the middle of a transaction
        cannot do.
        """
        if self._connection is None or self._connection.closed:
            self._connection = self.open_connection()
        return self._connection

    def open_connection(self):
        """Open a connection and start listening on the channel."""
        connection = self.connect()
        try:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(f"LISTEN {quote_channel(self.channel)}")
        except Exception:
            # Nothing holds it yet, so it would be leaked rather than
            # reused or closed by the caller.
            connection.close()
            raise

        logger.debug("Listening on %s", self.channel)
        return connection

    def connect(self):
        """
        Open a connection with the driver and the settings Django uses.

        The parameters come from the Django connection, so the broker
        reaches the same database with the same credentials without
        anything being configured twice.
        """
        from django.db.backends.postgresql.base import Database

        return Database.connect(**connections[self.database].get_connection_params())

    def drain(self, connection):
        """
        Yield the payloads of the notifications already delivered.

        The notifications are taken one at a time, so a caller that stops
        early leaves the rest for the next call rather than dropping them.
        """
        if self.is_psycopg3:
            pgconn = connection.pgconn
            pgconn.consume_input()
            while (notify := pgconn.notifies()) is not None:
                yield notify.extra.decode("utf-8", errors="replace")
        else:
            connection.poll()
            while connection.notifies:
                yield connection.notifies.pop(0).payload

    def wait(self, connection, timeout):
        """Wait for the connection to have something to read, or time out."""
        try:
            select.select([connection], [], [], timeout)
        except InterruptedError:  # pragma: no cover - retried by the caller
            pass

    # -- messages --------------------------------------------------------

    def to_message(self, payload, queue_name=None):
        """
        Turn a notification payload into a BrokerMessage.

        Returns None when the payload names no task, or names one for
        another queue than the worker was started for.
        """
        try:
            body = json.loads(payload)
        except ValueError:
            body = None

        if isinstance(body, dict):
            task_id = body.get("task_id")
            notified_queue = body.get("queue_name")
        else:
            # A payload that is only an id, so a NOTIFY sent by hand or
            # from a trigger works too. It names no queue, which a worker
            # restricted to one treats as "not mine".
            task_id = payload.strip()
            notified_queue = None

        if not task_id:
            logger.error(
                "Discarding a notification on %s: no task id in %r",
                self.channel,
                payload,
            )
            return None

        if queue_name and notified_queue != queue_name:
            logger.debug(
                "Ignoring task %s: it is queued on %r, not %r",
                task_id,
                notified_queue,
                queue_name,
            )
            return None

        return BrokerMessage(str(task_id), raw=payload)
