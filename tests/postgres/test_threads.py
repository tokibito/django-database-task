"""
``--threads M`` at work, against PostgreSQL.

The executor threads claim and write on connections of their own, so they
need a database with row locks, which is why the command refuses several
threads on SQLite; tests/test_commands.py covers that refusal, and
tests/test_threads.py the pool on its own. Here the command runs with
threads for real: tasks that can only finish together, one worker id per
thread, the cap, the failure count, broker messages spread over the
threads, and the shutdown that lets the running tasks finish.

Run the suite with DJANGO_DATABASE_ENGINE=postgresql to include them.
"""

import sys
import threading
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.db import connections
from django.tasks.base import TaskResultStatus

from django_database_task.brokers import BrokerMessage
from django_database_task.models import DatabaseTask
from tests import tasks as test_tasks
from tests.tasks import (
    barrier_task,
    failing_task,
    shutdown_signal_task,
    simple_task,
    slow_task,
)
from tests.test_commands import FakePullBroker, make_backend, run_worker

if connections["default"].vendor != "postgresql":
    pytest.skip(
        "needs a PostgreSQL database; run the suite with "
        "DJANGO_DATABASE_ENGINE=postgresql",
        allow_module_level=True,
    )

posix_signals = pytest.mark.skipif(
    sys.platform == "win32",
    reason="os.kill() cannot deliver a signal to the test process on Windows",
)

# Real commits: the executor threads work on connections of their own, and
# must see the rows the test enqueued and be free to write theirs while the
# test's connection is not holding a transaction.
pytestmark = pytest.mark.django_db(transaction=True)


class TestThreads:
    def test_tasks_run_at_the_same_time(self):
        """Three tasks that each wait for the other two can only pass together."""
        test_tasks.barrier = threading.Barrier(3, timeout=10)
        results = [barrier_task.enqueue() for _ in range(3)]
        out = StringIO()

        call_command("run_database_tasks", threads=3, stdout=out)

        statuses = {DatabaseTask.objects.get(id=r.id).status for r in results}
        assert statuses == {TaskResultStatus.SUCCESSFUL}
        output = out.getvalue()
        assert "Threads: 3" in output
        assert "Total tasks processed: 3" in output
        assert "Tasks failed" not in output

    def test_each_thread_records_its_own_worker_id(self):
        test_tasks.barrier = threading.Barrier(2, timeout=10)
        results = [barrier_task.enqueue() for _ in range(2)]

        call_command("run_database_tasks", threads=2, stdout=StringIO())

        worker_ids = sorted(
            DatabaseTask.objects.get(id=r.id).worker_ids_json[0] for r in results
        )
        assert [w.rsplit("-", 1)[1] for w in worker_ids] == ["t1", "t2"]
        assert worker_ids[0].rsplit("-", 1)[0] == worker_ids[1].rsplit("-", 1)[0]

    def test_max_tasks_counts_the_tasks_handed_out(self):
        for _ in range(5):
            slow_task.enqueue(seconds=0.1)
        out = StringIO()

        call_command("run_database_tasks", threads=2, max_tasks=3, stdout=out)

        assert "Reached max tasks limit: 3" in out.getvalue()
        assert "Total tasks processed: 3" in out.getvalue()
        assert DatabaseTask.objects.filter(status=TaskResultStatus.READY).count() == 2

    def test_failures_are_counted_across_threads(self):
        for _ in range(4):
            failing_task.enqueue()
        out = StringIO()

        with pytest.raises(SystemExit) as exc_info:
            call_command(
                "run_database_tasks", threads=2, failed_exit_code=5, stdout=out
            )

        assert exc_info.value.code == 5
        assert "Tasks failed: 4" in out.getvalue()
        assert "Total tasks processed: 4" in out.getvalue()

    def test_connections_are_closed_after_every_task(self):
        for _ in range(3):
            simple_task.enqueue(1, 2)

        with patch("django_database_task.threads.close_old_connections") as close:
            call_command("run_database_tasks", threads=2, stdout=StringIO())

        assert close.call_count == 3

    def test_output_ends_after_the_last_task(self):
        slow_task.enqueue(seconds=0.3)
        out = StringIO()

        call_command("run_database_tasks", threads=2, stdout=out)

        output = out.getvalue()
        assert output.index("Task completed successfully") < output.index(
            "No more tasks to process."
        )

    def test_broker_messages_are_spread_over_the_threads(self):
        test_tasks.barrier = threading.Barrier(3, timeout=10)
        results = [barrier_task.enqueue() for _ in range(3)]
        broker = FakePullBroker(
            batches=[[BrokerMessage(task_id=r.id) for r in results]]
        )

        output = run_worker(
            make_backend(broker),
            source="broker",
            threads=3,
            max_messages=10,
            wait_time=0,
        )

        statuses = {DatabaseTask.objects.get(id=r.id).status for r in results}
        assert statuses == {TaskResultStatus.SUCCESSFUL}
        assert sorted(broker.acked) == sorted(r.id for r in results)
        assert "Total tasks processed: 3" in output

    def test_receive_asks_for_no_more_than_the_free_threads(self):
        broker = FakePullBroker(batches=[])

        run_worker(
            make_backend(broker),
            source="broker",
            threads=2,
            max_messages=10,
            wait_time=0,
        )

        assert broker.received[0]["max_messages"] == 2

    @posix_signals
    def test_running_tasks_finish_before_shutdown(self):
        """A signal lets the tasks in the threads finish and starts no more."""
        first = slow_task.enqueue(seconds=0.5)
        second = shutdown_signal_task.enqueue()
        third = simple_task.enqueue(1, 2)
        out = StringIO()

        call_command("run_database_tasks", threads=2, stdout=out)

        assert DatabaseTask.objects.get(id=first.id).status == (
            TaskResultStatus.SUCCESSFUL
        )
        assert DatabaseTask.objects.get(id=second.id).status == (
            TaskResultStatus.SUCCESSFUL
        )
        assert DatabaseTask.objects.get(id=third.id).status == TaskResultStatus.READY
        assert "Shutdown complete" in out.getvalue()
        assert "Total tasks processed: 2" in out.getvalue()
