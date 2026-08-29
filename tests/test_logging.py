"""Tests for the fields the library puts on its log records.

An operator running the worker from a job scheduler reads these through a
structured (JSON) formatter, so what matters is that the values arrive as
attributes on the record rather than only inside the message text.
"""

import logging
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.tasks.base import TaskResultStatus

from django_database_task.backends import DatabaseTaskBackend, task_log_fields
from django_database_task.models import DatabaseTask

from .tasks import failing_task, simple_task

LOGGER_NAME = "django_database_task"


@pytest.fixture
def task_logs(caplog):
    """Capture the library's own records at INFO and above."""
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    return caplog


def records_matching(caplog, fragment):
    return [r for r in caplog.records if fragment in r.getMessage()]


@pytest.mark.django_db
class TestTaskLogFields:
    def test_fields_describe_the_task(self):
        result = simple_task.enqueue(1, 2)
        db_task = DatabaseTask.objects.get(id=result.id)

        fields = task_log_fields(db_task, worker_id="host-abc")

        assert fields == {
            "task_id": str(db_task.id),
            "task_path": db_task.task_path,
            "queue_name": db_task.queue_name,
            "priority": db_task.priority,
            "backend_alias": db_task.backend_name,
            "worker_id": "host-abc",
        }

    def test_task_id_is_a_string(self):
        """A UUID would not survive a JSON formatter unhelped."""
        result = simple_task.enqueue(1, 2)
        db_task = DatabaseTask.objects.get(id=result.id)

        assert isinstance(task_log_fields(db_task)["task_id"], str)

    def test_extra_fields_are_merged(self):
        result = simple_task.enqueue(1, 2)
        db_task = DatabaseTask.objects.get(id=result.id)

        fields = task_log_fields(db_task, duration_ms=12)

        assert fields["duration_ms"] == 12

    def test_no_field_shadows_a_logrecord_attribute(self):
        """
        LogRecord attributes cannot be overwritten through extra: logging
        raises KeyError instead. Guard the whole set rather than finding
        out from a crash in production.
        """
        result = simple_task.enqueue(1, 2)
        db_task = DatabaseTask.objects.get(id=result.id)
        fields = task_log_fields(
            db_task,
            worker_id="w",
            status="SUCCESSFUL",
            duration_ms=1,
            error_class="ValueError",
        )

        reserved = vars(logging.LogRecord("n", logging.INFO, "p", 1, "m", None, None))

        assert not set(fields) & set(reserved)


@pytest.mark.django_db
class TestTaskLifecycleLogging:
    def test_start_is_logged_with_fields(self, task_logs):
        result = simple_task.enqueue(1, 2)

        call_command("run_database_tasks", stdout=StringIO())

        (record,) = records_matching(task_logs, "Task started")
        assert record.task_id == str(result.id)
        assert record.task_path.endswith("simple_task")
        assert record.queue_name == "default"
        assert record.worker_id

    def test_success_is_logged_with_fields(self, task_logs):
        result = simple_task.enqueue(1, 2)

        call_command("run_database_tasks", stdout=StringIO())

        (record,) = records_matching(task_logs, "Task completed successfully")
        assert record.task_id == str(result.id)
        assert record.status == TaskResultStatus.SUCCESSFUL.value
        assert isinstance(record.duration_ms, int)
        assert record.duration_ms >= 0

    def test_failure_is_logged_with_fields(self, task_logs):
        result = failing_task.enqueue()

        call_command("run_database_tasks", stdout=StringIO())

        (record,) = records_matching(task_logs, "Task failed")
        assert record.levelno == logging.ERROR
        assert record.task_id == str(result.id)
        assert record.status == TaskResultStatus.FAILED.value
        assert record.error_class == "builtins.ValueError"
        assert isinstance(record.duration_ms, int)

    def test_worker_id_is_the_one_running_the_task(self, task_logs):
        simple_task.enqueue(1, 2)

        call_command("run_database_tasks", stdout=StringIO())

        (started,) = records_matching(task_logs, "Worker started")
        (task,) = records_matching(task_logs, "Task completed successfully")
        assert task.worker_id == started.worker_id

    def test_a_task_the_worker_could_not_run_is_logged(self, task_logs):
        result = simple_task.enqueue(1, 2)

        with patch.object(
            DatabaseTaskBackend, "_resolve_task", side_effect=ImportError("gone")
        ):
            call_command("run_database_tasks", stdout=StringIO())

        (record,) = records_matching(task_logs, "Worker could not run task")
        assert record.levelno == logging.ERROR
        assert record.task_id == str(result.id)
        assert record.exc_info is not None


@pytest.mark.django_db
class TestWorkerLifecycleLogging:
    def test_start_is_logged_with_fields(self, task_logs):
        call_command("run_database_tasks", queue="emails", stdout=StringIO())

        (record,) = records_matching(task_logs, "Worker started")
        assert record.worker_id
        assert record.backend_alias == "default"
        assert record.source == "db"
        assert record.queue_name == "emails"
        assert record.continuous is False

    def test_finish_reports_the_counts_and_exit_code(self, task_logs):
        simple_task.enqueue(1, 2)
        failing_task.enqueue()

        with pytest.raises(SystemExit):
            call_command("run_database_tasks", failed_exit_code=5, stdout=StringIO())

        (record,) = records_matching(task_logs, "Worker finished")
        assert record.tasks_processed == 2
        assert record.tasks_failed == 1
        assert record.exit_code == 5

    def test_exit_code_is_zero_when_nothing_is_wrong(self, task_logs):
        simple_task.enqueue(1, 2)

        call_command("run_database_tasks", stdout=StringIO())

        (record,) = records_matching(task_logs, "Worker finished")
        assert record.exit_code == 0
