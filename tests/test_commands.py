"""Tests for management commands."""

import os
import signal
import threading
import time
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.tasks.base import TaskResultStatus
from django.utils import timezone

from django_database_task.backends import DatabaseTaskBackend
from django_database_task.brokers import BrokerMessage, HTTPPushBroker, PullBroker
from django_database_task.models import DatabaseTask

from . import tasks as test_tasks
from .tasks import (
    failing_task,
    high_priority_task,
    low_priority_task,
    record_sigterm_handler_task,
    shutdown_aware_task,
    shutdown_signal_task,
    simple_task,
    special_queue_task,
)


@pytest.mark.django_db
class TestRunDatabaseTasks:
    def test_run_database_tasks_executes_task(self):
        """Task is executed."""
        simple_task.enqueue(5, 3)

        out = StringIO()
        call_command("run_database_tasks", stdout=out)

        assert (
            DatabaseTask.objects.filter(status=TaskResultStatus.SUCCESSFUL).count() == 1
        )

    def test_run_database_tasks_updates_status(self):
        """Status is updated."""
        result = simple_task.enqueue(1, 2)

        call_command("run_database_tasks", stdout=StringIO())

        db_task = DatabaseTask.objects.get(id=result.id)
        assert db_task.status == TaskResultStatus.SUCCESSFUL
        assert db_task.return_value_json == 3

    def test_run_database_tasks_respects_priority(self):
        """Tasks are executed in priority order."""
        # Enqueue low priority first
        low_result = low_priority_task.enqueue()
        high_result = high_priority_task.enqueue()

        # Execute only 1 task
        call_command("run_database_tasks", max_tasks=1, stdout=StringIO())

        # High priority is executed first
        high_task = DatabaseTask.objects.get(id=high_result.id)
        low_task = DatabaseTask.objects.get(id=low_result.id)

        assert high_task.status == TaskResultStatus.SUCCESSFUL
        assert low_task.status == TaskResultStatus.READY

    def test_run_database_tasks_respects_run_after(self):
        """run_after is respected."""
        # Set future execution time
        future = timezone.now() + timedelta(hours=1)
        future_task = simple_task.using(run_after=future)
        future_result = future_task.enqueue(1, 1)

        # Currently executable task
        now_result = simple_task.enqueue(2, 2)

        call_command("run_database_tasks", stdout=StringIO())

        # Future task is not executed
        future_db = DatabaseTask.objects.get(id=future_result.id)
        now_db = DatabaseTask.objects.get(id=now_result.id)

        assert future_db.status == TaskResultStatus.READY
        assert now_db.status == TaskResultStatus.SUCCESSFUL

    def test_run_database_tasks_handles_error(self):
        """Status is FAILED on error."""
        result = failing_task.enqueue()

        call_command("run_database_tasks", stdout=StringIO())

        db_task = DatabaseTask.objects.get(id=result.id)
        assert db_task.status == TaskResultStatus.FAILED
        assert len(db_task.errors_json) > 0

    def test_run_database_tasks_queue_filter(self):
        """Queue filter works."""
        default_result = simple_task.enqueue(1, 1)
        special_result = special_queue_task.enqueue()

        # Execute only special queue
        call_command("run_database_tasks", queue="special", stdout=StringIO())

        default_db = DatabaseTask.objects.get(id=default_result.id)
        special_db = DatabaseTask.objects.get(id=special_result.id)

        assert default_db.status == TaskResultStatus.READY
        assert special_db.status == TaskResultStatus.SUCCESSFUL

    def test_run_database_tasks_max_tasks(self):
        """max_tasks option works."""
        simple_task.enqueue(1, 1)
        simple_task.enqueue(2, 2)
        simple_task.enqueue(3, 3)

        call_command("run_database_tasks", max_tasks=2, stdout=StringIO())

        assert (
            DatabaseTask.objects.filter(status=TaskResultStatus.SUCCESSFUL).count() == 2
        )
        assert DatabaseTask.objects.filter(status=TaskResultStatus.READY).count() == 1

    def test_run_database_tasks_no_tasks(self):
        """No tasks to process."""
        out = StringIO()
        call_command("run_database_tasks", stdout=out)

        assert "No more tasks to process" in out.getvalue()


@pytest.mark.django_db
class TestRunDatabaseTasksGracefulShutdown:
    def test_running_task_finishes_before_shutdown(self):
        """A task running when SIGTERM arrives is not interrupted."""
        signal_result = shutdown_signal_task.enqueue()
        simple_task.enqueue(1, 2)

        out = StringIO()
        call_command("run_database_tasks", stdout=out)

        signal_db = DatabaseTask.objects.get(id=signal_result.id)
        assert signal_db.status == TaskResultStatus.SUCCESSFUL
        assert signal_db.return_value_json == "sent SIGTERM"

    def test_no_new_task_is_started_after_signal(self):
        """Queued tasks are left untouched after a shutdown signal."""
        shutdown_signal_task.enqueue()
        next_result = simple_task.enqueue(1, 2)

        call_command("run_database_tasks", stdout=StringIO())

        next_db = DatabaseTask.objects.get(id=next_result.id)
        assert next_db.status == TaskResultStatus.READY

    def test_shutdown_is_reported(self):
        """The shutdown is reported on stdout."""
        shutdown_signal_task.enqueue()

        out = StringIO()
        call_command("run_database_tasks", stdout=out)
        output = out.getvalue()

        assert "Received SIGTERM" in output
        assert "Shutdown complete" in output
        assert "Total tasks processed: 1" in output

    def test_sigint_also_shuts_down_gracefully(self):
        """SIGINT is handled like SIGTERM, without a KeyboardInterrupt."""
        signal_result = shutdown_signal_task.enqueue(signal_name="SIGINT")
        next_result = simple_task.enqueue(1, 2)

        out = StringIO()
        call_command("run_database_tasks", stdout=out)

        assert (
            DatabaseTask.objects.get(id=signal_result.id).status
            == TaskResultStatus.SUCCESSFUL
        )
        assert (
            DatabaseTask.objects.get(id=next_result.id).status == TaskResultStatus.READY
        )
        assert "Received SIGINT" in out.getvalue()

    def test_continuous_mode_stops_waiting_on_signal(self):
        """The polling sleep is interrupted by a shutdown signal."""
        pid = os.getpid()
        timer = threading.Timer(0.3, lambda: os.kill(pid, signal.SIGTERM))
        timer.daemon = True
        timer.start()

        started = time.monotonic()
        try:
            call_command(
                "run_database_tasks",
                continuous=True,
                interval=60,
                stdout=StringIO(),
            )
        finally:
            timer.cancel()
        elapsed = time.monotonic() - started

        # Without an interruptible sleep this would block for 60 seconds.
        assert elapsed < 30

    def test_signal_handlers_are_restored(self):
        """The original signal handlers are restored after the command."""
        original_term = signal.getsignal(signal.SIGTERM)
        original_int = signal.getsignal(signal.SIGINT)

        call_command("run_database_tasks", stdout=StringIO())

        assert signal.getsignal(signal.SIGTERM) is original_term
        assert signal.getsignal(signal.SIGINT) is original_int

    def test_handler_is_installed_while_task_runs(self):
        """The graceful shutdown handler is active during task execution."""
        original_term = signal.getsignal(signal.SIGTERM)
        test_tasks.recorded_sigterm_handler = None

        record_sigterm_handler_task.enqueue()
        call_command("run_database_tasks", stdout=StringIO())

        assert test_tasks.recorded_sigterm_handler is not None
        assert test_tasks.recorded_sigterm_handler is not original_term

    def test_no_graceful_shutdown_option_skips_handlers(self):
        """--no-graceful-shutdown leaves the signal handlers untouched."""
        original_term = signal.getsignal(signal.SIGTERM)
        test_tasks.recorded_sigterm_handler = None

        record_sigterm_handler_task.enqueue()
        out = StringIO()
        call_command("run_database_tasks", no_graceful_shutdown=True, stdout=out)

        assert test_tasks.recorded_sigterm_handler is original_term
        assert "Graceful shutdown: disabled" in out.getvalue()

    def test_shutdown_timeout_is_reported(self):
        """The configured shutdown timeout is reported on startup."""
        out = StringIO()
        call_command("run_database_tasks", shutdown_timeout=30.0, stdout=out)

        assert "Graceful shutdown: enabled (timeout=30.0s)" in out.getvalue()

    def test_task_can_check_shutdown_state(self):
        """Task functions can stop early with is_shutdown_requested()."""
        result = shutdown_aware_task.enqueue(iterations=100)

        call_command("run_database_tasks", stdout=StringIO())

        db_task = DatabaseTask.objects.get(id=result.id)
        assert db_task.status == TaskResultStatus.SUCCESSFUL
        assert db_task.return_value_json < 100


@pytest.mark.django_db
class TestRunDatabaseTasksVerbosity:
    def _run_continuous(self, **options):
        """Run one polling round in continuous mode, then stop it."""
        pid = os.getpid()
        timer = threading.Timer(0.3, lambda: os.kill(pid, signal.SIGTERM))
        timer.daemon = True
        timer.start()

        out = StringIO()
        try:
            call_command(
                "run_database_tasks",
                continuous=True,
                interval=0.05,
                stdout=out,
                **options,
            )
        finally:
            timer.cancel()
        return out.getvalue()

    @staticmethod
    def _heartbeat_lines(output):
        """Lines made up only of heartbeat dots."""
        return [line for line in output.splitlines() if line and set(line) == {"."}]

    def test_idle_dots_are_hidden_by_default(self):
        """No heartbeat dot is printed at the default verbosity."""
        output = self._run_continuous()

        assert self._heartbeat_lines(output) == []

    def test_idle_dots_shown_with_verbosity_2(self):
        """-v 2 prints a heartbeat dot per idle poll."""
        output = self._run_continuous(verbosity=2)

        assert self._heartbeat_lines(output)

    def test_verbosity_0_silences_informational_output(self):
        """-v 0 prints nothing for a successful run."""
        simple_task.enqueue(5, 3)

        out = StringIO()
        call_command("run_database_tasks", verbosity=0, stdout=out)

        assert out.getvalue() == ""
        assert (
            DatabaseTask.objects.filter(status=TaskResultStatus.SUCCESSFUL).count() == 1
        )

    def test_verbosity_0_still_reports_failures(self):
        """Task failures are reported even at verbosity 0."""
        failing_task.enqueue()

        out = StringIO()
        call_command("run_database_tasks", verbosity=0, stdout=out)

        assert "Task failed" in out.getvalue()

    def test_default_verbosity_reports_task(self):
        """The per-task block is still printed at the default verbosity."""
        simple_task.enqueue(5, 3)

        out = StringIO()
        call_command("run_database_tasks", stdout=out)

        output = out.getvalue()
        assert "Processing task:" in output
        assert "Task completed successfully" in output


@pytest.mark.django_db
class TestPurgeCompletedDatabaseTasks:
    def test_purge_deletes_completed_tasks(self):
        """Completed tasks are deleted."""
        # Create and execute tasks
        simple_task.enqueue(1, 1)
        failing_task.enqueue()

        call_command("run_database_tasks", stdout=StringIO())

        assert DatabaseTask.objects.count() == 2

        # Delete
        call_command("purge_completed_database_tasks", stdout=StringIO())

        assert DatabaseTask.objects.count() == 0

    def test_purge_respects_status_option(self):
        """status option works."""
        simple_task.enqueue(1, 1)
        failing_task.enqueue()

        call_command("run_database_tasks", stdout=StringIO())

        # Delete only SUCCESSFUL
        call_command(
            "purge_completed_database_tasks", status="SUCCESSFUL", stdout=StringIO()
        )

        assert DatabaseTask.objects.count() == 1
        assert DatabaseTask.objects.first().status == TaskResultStatus.FAILED

    def test_purge_respects_days_option(self):
        """days option works."""
        # Create and execute task
        result = simple_task.enqueue(1, 1)
        call_command("run_database_tasks", stdout=StringIO())

        # Set finished_at to the past
        db_task = DatabaseTask.objects.get(id=result.id)
        db_task.finished_at = timezone.now() - timedelta(days=10)
        db_task.save()

        # Delete tasks older than 5 days
        call_command("purge_completed_database_tasks", days=5, stdout=StringIO())

        assert DatabaseTask.objects.count() == 0

    def test_purge_keeps_recent_tasks(self):
        """Recent tasks are not deleted."""
        simple_task.enqueue(1, 1)
        call_command("run_database_tasks", stdout=StringIO())

        # Delete tasks older than 5 days (recent tasks remain)
        call_command("purge_completed_database_tasks", days=5, stdout=StringIO())

        assert DatabaseTask.objects.count() == 1

    def test_purge_dry_run(self):
        """dry-run mode does not delete."""
        simple_task.enqueue(1, 1)
        call_command("run_database_tasks", stdout=StringIO())

        out = StringIO()
        call_command("purge_completed_database_tasks", dry_run=True, stdout=out)

        assert DatabaseTask.objects.count() == 1
        assert "Dry run" in out.getvalue()

    def test_purge_no_tasks(self):
        """No tasks to delete."""
        out = StringIO()
        call_command("purge_completed_database_tasks", stdout=out)

        assert "No tasks to delete" in out.getvalue()


@pytest.mark.django_db
class TestPurgeWithPendingTasks:
    def test_purge_does_not_delete_ready_tasks(self):
        """READY status tasks are not deleted."""
        simple_task.enqueue(1, 1)  # Not executed

        call_command("purge_completed_database_tasks", stdout=StringIO())

        assert DatabaseTask.objects.count() == 1
        assert DatabaseTask.objects.first().status == TaskResultStatus.READY


class FakePullBroker(PullBroker):
    """A pull broker whose messages are handed to it by the test."""

    def __init__(self, backend=None, options=None, batches=None):
        super().__init__(backend, options)
        self.batches = list(batches or [])
        self.received = []
        self.acked = []
        self.nacked = []

    def enqueue(self, task_result):
        pass

    def receive(self, queue_name=None, max_messages=1, wait_seconds=20):
        self.received.append(
            {
                "queue_name": queue_name,
                "max_messages": max_messages,
                "wait_seconds": wait_seconds,
            }
        )
        if not self.batches:
            return []
        return self.batches.pop(0)[:max_messages]

    def ack(self, message):
        self.acked.append(message.task_id)

    def nack(self, message, delay=None):
        self.nacked.append(message.task_id)


def make_backend(broker=None):
    """Build a backend, optionally with a broker already attached."""
    backend = DatabaseTaskBackend(alias="default", params={"QUEUES": []})
    backend.broker = broker
    if broker is not None:
        broker.backend = backend
    return backend


def run_worker(backend, **options):
    """Run the command against a backend built for the test."""
    out = StringIO()
    with patch.dict(
        "django_database_task.management.commands.run_database_tasks.task_backends",
        {"default": backend},
        clear=False,
    ):
        call_command("run_database_tasks", stdout=out, stderr=out, **options)
    return out.getvalue()


@pytest.mark.django_db
class TestSourceResolution:
    """Tests for choosing where the worker reads tasks from."""

    def test_auto_uses_the_database_without_a_broker(self):
        output = run_worker(make_backend())

        assert "Source: db" in output

    def test_auto_uses_both_with_a_pull_broker(self):
        output = run_worker(make_backend(FakePullBroker()))

        assert "Source: both" in output

    def test_auto_uses_the_database_with_a_push_only_broker(self):
        """A broker that cannot be received from is not a task source."""
        output = run_worker(make_backend(HTTPPushBroker(backend=None)))

        assert "Source: db" in output

    def test_db_is_honoured_even_with_a_broker(self):
        broker = FakePullBroker()

        output = run_worker(make_backend(broker), source="db")

        assert "Source: db" in output
        assert broker.received == []

    def test_broker_source_requires_a_pull_broker(self):
        with pytest.raises(CommandError, match="needs a backend whose broker"):
            run_worker(make_backend(), source="broker")

    def test_both_source_requires_a_pull_broker(self):
        with pytest.raises(CommandError, match="needs a backend whose broker"):
            run_worker(make_backend(), source="both")

    def test_max_messages_must_be_positive(self):
        with pytest.raises(CommandError, match="--max-messages must be at least 1"):
            run_worker(make_backend(), max_messages=0)


@pytest.mark.django_db
class TestBrokerSource:
    """Tests for running tasks the broker points at."""

    def test_runs_the_task_a_message_names(self):
        result = simple_task.enqueue(2, 3)
        broker = FakePullBroker(batches=[[BrokerMessage(str(result.id))]])

        run_worker(make_backend(broker), source="broker")

        db_task = DatabaseTask.objects.get(id=result.id)
        assert db_task.status == TaskResultStatus.SUCCESSFUL
        assert db_task.return_value_json == 5
        assert broker.acked == [str(result.id)]

    def test_does_not_touch_the_database_queue(self):
        """Only what the broker names is run in broker mode."""
        untouched = simple_task.enqueue(1, 1)
        broker = FakePullBroker(batches=[[]])

        run_worker(make_backend(broker), source="broker")

        assert DatabaseTask.objects.get(id=untouched.id).status == (
            TaskResultStatus.READY
        )

    def test_a_failing_task_is_still_acknowledged(self):
        """The failure is recorded, so redelivering would only repeat it."""
        result = failing_task.enqueue()
        broker = FakePullBroker(batches=[[BrokerMessage(str(result.id))]])

        run_worker(make_backend(broker), source="broker")

        assert DatabaseTask.objects.get(id=result.id).status == TaskResultStatus.FAILED
        assert broker.acked == [str(result.id)]
        assert broker.nacked == []

    def test_a_message_for_a_deleted_task_is_dropped(self):
        broker = FakePullBroker(
            batches=[[BrokerMessage("3f2a9c11-0000-4000-8000-000000000000")]]
        )

        output = run_worker(make_backend(broker), source="broker")

        assert "no longer exists" in output
        assert broker.acked == ["3f2a9c11-0000-4000-8000-000000000000"]

    def test_a_message_for_a_finished_task_is_acknowledged(self):
        result = simple_task.enqueue(1, 1)
        DatabaseTask.objects.filter(id=result.id).update(
            status=TaskResultStatus.SUCCESSFUL
        )
        broker = FakePullBroker(batches=[[BrokerMessage(str(result.id))]])

        output = run_worker(make_backend(broker), source="broker")

        assert "not ready to run" in output
        assert broker.acked == [str(result.id)]
        assert "Total tasks processed: 0" in output

    def test_a_worker_side_failure_returns_the_message(self, monkeypatch):
        """A broken worker must not swallow the task."""
        result = simple_task.enqueue(1, 1)
        broker = FakePullBroker(batches=[[BrokerMessage(str(result.id))]])
        monkeypatch.setattr(
            "django_database_task.management.commands.run_database_tasks."
            "run_task_by_id",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("database is down")),
        )

        output = run_worker(make_backend(broker), source="broker")

        assert "database is down" in output
        assert broker.acked == []
        assert broker.nacked == [str(result.id)]

    def test_a_broker_failure_does_not_stop_the_worker(self):
        class BrokenBroker(FakePullBroker):
            def receive(self, queue_name=None, max_messages=1, wait_seconds=20):
                raise RuntimeError("broker is down")

        output = run_worker(make_backend(BrokenBroker()), source="broker")

        assert "Error receiving from broker" in output
        assert "Total tasks processed: 0" in output

    def test_the_queue_and_limits_are_passed_to_the_broker(self):
        broker = FakePullBroker()

        run_worker(
            make_backend(broker),
            source="broker",
            queue="emails",
            max_messages=5,
            wait_time=3.0,
        )

        assert broker.received == [
            {"queue_name": "emails", "max_messages": 5, "wait_seconds": 3.0}
        ]

    def test_max_tasks_caps_what_is_requested(self):
        """The worker never receives more messages than it may run."""
        results = [simple_task.enqueue(1, 1) for _ in range(3)]
        broker = FakePullBroker(batches=[[BrokerMessage(str(r.id)) for r in results]])

        run_worker(make_backend(broker), source="broker", max_messages=10, max_tasks=2)

        assert broker.received[0]["max_messages"] == 2
        assert len(broker.acked) == 2
        assert (
            DatabaseTask.objects.filter(status=TaskResultStatus.SUCCESSFUL).count() == 2
        )


@pytest.mark.django_db
class TestBothSources:
    """Tests for reading from the broker and the database together."""

    def test_the_broker_is_polled_without_waiting(self):
        """The database must get its turn, so the first poll cannot block."""
        broker = FakePullBroker()

        run_worker(make_backend(broker), source="both")

        assert broker.received[0]["wait_seconds"] == 0

    def test_the_database_is_used_when_the_broker_is_empty(self):
        """This is what picks up deferred tasks a broker cannot hold."""
        result = simple_task.enqueue(4, 4)
        broker = FakePullBroker()

        run_worker(make_backend(broker), source="both")

        db_task = DatabaseTask.objects.get(id=result.id)
        assert db_task.status == TaskResultStatus.SUCCESSFUL
        assert db_task.return_value_json == 8

    def test_the_broker_is_preferred_over_the_database(self):
        from_broker = simple_task.enqueue(1, 1)
        simple_task.enqueue(2, 2)
        broker = FakePullBroker(batches=[[BrokerMessage(str(from_broker.id))]])

        run_worker(make_backend(broker), source="both", max_tasks=1)

        assert DatabaseTask.objects.get(id=from_broker.id).status == (
            TaskResultStatus.SUCCESSFUL
        )

    def test_both_sources_are_drained(self):
        from_broker = simple_task.enqueue(1, 1)
        from_database = simple_task.enqueue(2, 2)
        broker = FakePullBroker(batches=[[BrokerMessage(str(from_broker.id))]])

        run_worker(make_backend(broker), source="both")

        assert (
            DatabaseTask.objects.filter(status=TaskResultStatus.SUCCESSFUL).count() == 2
        )
        assert broker.acked == [str(from_broker.id)]
        assert DatabaseTask.objects.get(id=from_database.id).status == (
            TaskResultStatus.SUCCESSFUL
        )


@pytest.mark.django_db
class TestBrokerContinuousMode:
    """Tests for the worker staying up while receiving from a broker."""

    def test_the_broker_wait_replaces_the_idle_interval(self):
        """A long poll is the wait, so no interval sleep is added to it."""
        waits = []

        class WaitRecordingBroker(FakePullBroker):
            def receive(self, queue_name=None, max_messages=1, wait_seconds=20):
                waits.append(wait_seconds)
                if len(waits) >= 3:
                    os.kill(os.getpid(), signal.SIGTERM)
                return []

        started = time.monotonic()
        run_worker(
            make_backend(WaitRecordingBroker()),
            source="broker",
            continuous=True,
            wait_time=0.01,
            interval=30,
        )

        # An added interval sleep would make this take at least 30 seconds.
        assert time.monotonic() - started < 10
        assert waits[:3] == [0.01, 0.01, 0.01]

    def test_both_waits_on_the_broker_when_everything_is_idle(self):
        waits = []

        class WaitRecordingBroker(FakePullBroker):
            def receive(self, queue_name=None, max_messages=1, wait_seconds=20):
                waits.append(wait_seconds)
                if len(waits) >= 4:
                    os.kill(os.getpid(), signal.SIGTERM)
                return []

        run_worker(
            make_backend(WaitRecordingBroker()),
            source="both",
            continuous=True,
            wait_time=0.01,
            interval=30,
        )

        # A non-blocking poll, then the blocking one that stands in for
        # the idle interval, and around again.
        assert waits[:4] == [0, 0.01, 0, 0.01]

    def test_a_shutdown_signal_stops_the_worker(self):
        class SignallingBroker(FakePullBroker):
            def receive(self, queue_name=None, max_messages=1, wait_seconds=20):
                os.kill(os.getpid(), signal.SIGTERM)
                return []

        output = run_worker(
            make_backend(SignallingBroker()),
            source="broker",
            continuous=True,
            wait_time=0.01,
        )

        assert "Shutdown complete" in output


@pytest.mark.django_db
class TestBrokerLifecycle:
    """Tests for releasing whatever the broker holds open."""

    def test_the_broker_is_closed_when_the_worker_stops(self):
        class ClosingBroker(FakePullBroker):
            closed = 0

            def close(self):
                type(self).closed += 1

        broker = ClosingBroker()

        run_worker(make_backend(broker), source="broker")

        assert type(broker).closed == 1

    def test_the_broker_is_closed_after_a_failure(self):
        class BrokenBroker(FakePullBroker):
            closed = 0

            def receive(self, queue_name=None, max_messages=1, wait_seconds=20):
                raise RuntimeError("broker is down")

            def close(self):
                type(self).closed += 1

        run_worker(make_backend(BrokenBroker()), source="broker")

        assert BrokenBroker.closed == 1

    def test_a_broker_that_is_not_used_is_left_alone(self):
        class ClosingBroker(FakePullBroker):
            closed = 0

            def close(self):
                type(self).closed += 1

        run_worker(make_backend(ClosingBroker()), source="db")

        assert ClosingBroker.closed == 0

    def test_a_failure_to_close_is_reported_not_raised(self):
        class UnclosableBroker(FakePullBroker):
            def close(self):
                raise RuntimeError("connection already gone")

        output = run_worker(make_backend(UnclosableBroker()), source="broker")

        assert "Error closing the broker" in output


@pytest.mark.django_db
class TestExitCodes:
    """Tests for the exit codes a job scheduler reads."""

    def test_no_exit_code_by_default(self):
        """Without the options an empty run still exits normally."""
        call_command("run_database_tasks", stdout=StringIO())

    def test_failed_task_exits_normally_by_default(self):
        """A failed task does not change the exit code on its own."""
        failing_task.enqueue()

        call_command("run_database_tasks", stdout=StringIO())

    def test_empty_exit_code_used_when_nothing_ran(self):
        """An idle run reports the code the scheduler was given."""
        with pytest.raises(SystemExit) as exc_info:
            call_command("run_database_tasks", empty_exit_code=4, stdout=StringIO())

        assert exc_info.value.code == 4

    def test_empty_exit_code_not_used_when_a_task_ran(self):
        """Processing anything at all makes the run a normal one."""
        simple_task.enqueue(1, 2)

        call_command("run_database_tasks", empty_exit_code=4, stdout=StringIO())

    def test_failed_exit_code_used_when_a_task_failed(self):
        failing_task.enqueue()

        with pytest.raises(SystemExit) as exc_info:
            call_command("run_database_tasks", failed_exit_code=5, stdout=StringIO())

        assert exc_info.value.code == 5

    def test_failed_exit_code_not_used_when_every_task_succeeded(self):
        simple_task.enqueue(1, 2)

        call_command("run_database_tasks", failed_exit_code=5, stdout=StringIO())

    def test_a_task_the_worker_could_not_run_counts_as_failed(self):
        """
        The task never gets as far as running -- its code cannot be
        imported -- so nothing records it as FAILED except the worker.
        """
        simple_task.enqueue(1, 2)

        with patch.object(
            DatabaseTaskBackend, "_resolve_task", side_effect=ImportError("gone")
        ):
            with pytest.raises(SystemExit) as exc_info:
                call_command(
                    "run_database_tasks", failed_exit_code=5, stdout=StringIO()
                )

        assert exc_info.value.code == 5

    def test_success_after_a_failure_still_reports_the_failure(self):
        failing_task.enqueue()
        simple_task.enqueue(1, 2)

        with pytest.raises(SystemExit) as exc_info:
            call_command("run_database_tasks", failed_exit_code=5, stdout=StringIO())

        assert exc_info.value.code == 5

    def test_failure_count_is_reported(self):
        failing_task.enqueue()

        out = StringIO()
        call_command("run_database_tasks", stdout=out)

        assert "Tasks failed: 1" in out.getvalue()

    def test_exit_code_must_be_a_number(self):
        with pytest.raises(CommandError, match="whole numbers"):
            call_command("run_database_tasks", "--empty-exit-code=nope")

    def test_exit_code_must_fit_in_a_wait_status(self):
        with pytest.raises(CommandError, match="between 0 and 255"):
            call_command("run_database_tasks", "--failed-exit-code=300")


@pytest.mark.django_db
class TestBrokerExitCodes:
    """Exit codes for the tasks a broker hands the worker."""

    def test_failed_broker_task_counts_as_failed(self):
        result = failing_task.enqueue()
        broker = FakePullBroker(batches=[[BrokerMessage(task_id=result.id)]])
        backend = make_backend(broker)

        with pytest.raises(SystemExit) as exc_info:
            run_worker(backend, source="broker", failed_exit_code=5, wait_time=0)

        assert exc_info.value.code == 5

    def test_receive_error_is_not_a_task_failure(self):
        """
        A broker that cannot be reached is an infrastructure problem, not a
        task that failed, so it leaves the run looking idle instead.
        """
        broker = FakePullBroker()
        broker.receive = lambda **kwargs: (_ for _ in ()).throw(RuntimeError("down"))
        backend = make_backend(broker)

        with pytest.raises(SystemExit) as exc_info:
            run_worker(
                backend,
                source="broker",
                empty_exit_code=4,
                failed_exit_code=5,
                wait_time=0,
            )

        assert exc_info.value.code == 4

    def test_failure_wins_over_an_empty_run(self):
        """
        A message the worker could not run leaves the processed count at
        zero, so both conditions hold at once and the failure has to win.
        """
        result = simple_task.enqueue(1, 2)
        broker = FakePullBroker(batches=[[BrokerMessage(task_id=result.id)]])
        backend = make_backend(broker)

        with patch.object(
            DatabaseTaskBackend, "_resolve_task", side_effect=ImportError("gone")
        ):
            with pytest.raises(SystemExit) as exc_info:
                run_worker(
                    backend,
                    source="broker",
                    empty_exit_code=4,
                    failed_exit_code=5,
                    wait_time=0,
                )

        assert exc_info.value.code == 5

    def test_missing_broker_task_is_not_a_failure(self):
        """A message naming a task that is gone leaves nothing to fail."""
        import uuid as uuid_module

        broker = FakePullBroker(batches=[[BrokerMessage(task_id=uuid_module.uuid4())]])
        backend = make_backend(broker)

        with pytest.raises(SystemExit) as exc_info:
            run_worker(
                backend,
                source="broker",
                empty_exit_code=4,
                failed_exit_code=5,
                wait_time=0,
            )

        assert exc_info.value.code == 4
