"""Tests for management commands."""

from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.tasks.base import TaskResultStatus
from django.utils import timezone

from django_database_task.models import DatabaseTask

from .tasks import (
    failing_task,
    high_priority_task,
    low_priority_task,
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
