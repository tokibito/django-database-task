"""Tests for management commands."""

import os
import signal
import threading
import time
from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.tasks.base import TaskResultStatus
from django.utils import timezone

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
