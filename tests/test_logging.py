"""Tests for the fields the library puts on its log records.

An operator running the worker from a job scheduler reads these through a
structured (JSON) formatter, so what matters is that the values arrive as
attributes on the record rather than only inside the message text.
"""

import logging
import os
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.tasks.base import TaskResultStatus

from django_database_task.backends import DatabaseTaskBackend, task_log_fields
from django_database_task.brokers import BrokerMessage
from django_database_task.models import DatabaseTask
from django_database_task.supervisor import WORKER_INDEX_ENV

from .tasks import failing_task, simple_task
from .test_commands import FakePullBroker, make_backend, run_worker

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
    def test_fields_describe_the_task(self, monkeypatch):
        monkeypatch.delenv(WORKER_INDEX_ENV, raising=False)
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
            "pid": os.getpid(),
        }

    def test_fields_carry_the_supervisors_worker_index(self, monkeypatch):
        monkeypatch.setenv(WORKER_INDEX_ENV, "3")
        result = simple_task.enqueue(1, 2)
        db_task = DatabaseTask.objects.get(id=result.id)

        fields = task_log_fields(db_task, worker_id="host-abc")

        assert fields["worker_index"] == 3
        assert fields["pid"] == os.getpid()

    def test_an_unreadable_worker_index_is_left_out(self, monkeypatch):
        monkeypatch.setenv(WORKER_INDEX_ENV, "not-a-number")
        result = simple_task.enqueue(1, 2)
        db_task = DatabaseTask.objects.get(id=result.id)

        assert "worker_index" not in task_log_fields(db_task)

    def test_the_join_fields_shadow_no_logrecord_attribute(self, monkeypatch):
        monkeypatch.setenv(WORKER_INDEX_ENV, "1")
        result = simple_task.enqueue(1, 2)
        db_task = DatabaseTask.objects.get(id=result.id)

        reserved = vars(logging.LogRecord("n", logging.INFO, "p", 1, "m", None, None))

        assert not set(task_log_fields(db_task)) & set(reserved)

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
        """
        The database went away under the worker. The task is still READY
        afterwards, so the run is capped rather than left to fetch it again.
        """
        result = simple_task.enqueue(1, 2)

        with patch.object(
            DatabaseTaskBackend,
            "_claim_task",
            side_effect=RuntimeError("database is down"),
        ):
            call_command("run_database_tasks", max_tasks=1, stdout=StringIO())

        (record,) = records_matching(task_logs, "Worker could not run task")
        assert record.levelno == logging.ERROR
        assert record.task_id == str(result.id)
        assert record.exc_info is not None

    def test_a_task_the_worker_could_not_run_from_the_broker_is_logged(self, task_logs):
        """The record names the task in full, as the database path does."""
        result = simple_task.enqueue(1, 2)
        broker = FakePullBroker(batches=[[BrokerMessage(str(result.id))]])

        with patch.object(
            DatabaseTaskBackend,
            "_claim_task",
            side_effect=RuntimeError("database is down"),
        ):
            run_worker(make_backend(broker), source="broker")

        (started,) = records_matching(task_logs, "Worker started")
        (record,) = records_matching(task_logs, "Worker could not run task from")
        db_task = DatabaseTask.objects.get(id=result.id)
        assert record.levelno == logging.ERROR
        assert record.exc_info is not None
        assert record.task_id == str(result.id)
        assert record.task_path == db_task.task_path
        assert record.queue_name == db_task.queue_name
        assert record.priority == db_task.priority
        assert record.backend_alias == started.backend_alias
        assert record.worker_id == started.worker_id

    def test_a_broker_message_whose_task_cannot_be_read_is_still_logged(
        self, task_logs
    ):
        """
        A message naming something that is not a task id fails the lookup
        as well; the record keeps the id it was given.
        """
        broker = FakePullBroker(batches=[[BrokerMessage("not-a-uuid")]])

        run_worker(make_backend(broker), source="broker")

        (started,) = records_matching(task_logs, "Worker started")
        (record,) = records_matching(task_logs, "Worker could not run task from")
        assert record.levelno == logging.ERROR
        assert record.task_id == "not-a-uuid"
        assert record.worker_id == started.worker_id
        assert broker.nacked == ["not-a-uuid"]

    def test_a_broker_message_whose_task_cannot_be_read_keeps_the_join_fields(
        self, task_logs, monkeypatch
    ):
        monkeypatch.setenv(WORKER_INDEX_ENV, "5")
        broker = FakePullBroker(batches=[[BrokerMessage("not-a-uuid")]])

        run_worker(make_backend(broker), source="broker")

        (record,) = records_matching(task_logs, "Worker could not run task from")
        assert record.worker_index == 5
        assert record.pid == os.getpid()

    def test_a_task_that_could_not_be_started_is_logged(self, task_logs):
        """The task function no longer imports; the task is FAILED unrun."""
        result = simple_task.enqueue(1, 2)
        DatabaseTask.objects.filter(id=result.id).update(
            task_path="tests.tasks.removed_task"
        )

        call_command("run_database_tasks", stdout=StringIO())

        (record,) = records_matching(task_logs, "Task could not be started")
        assert record.levelno == logging.ERROR
        assert record.task_id == str(result.id)
        assert record.task_path == "tests.tasks.removed_task"
        assert record.status == TaskResultStatus.FAILED.value
        assert record.error_class == "builtins.AttributeError"
        assert record.exc_info is not None
        assert records_matching(task_logs, "Worker could not run task") == []
        assert records_matching(task_logs, "Task started") == []


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

    def test_a_failed_receive_is_logged_with_the_worker_fields(self, task_logs):
        class BrokenBroker(FakePullBroker):
            def receive(self, queue_name=None, max_messages=1, wait_seconds=20):
                raise RuntimeError("broker is down")

        run_worker(make_backend(BrokenBroker()), source="broker", queue="emails")

        (started,) = records_matching(task_logs, "Worker started")
        (record,) = records_matching(task_logs, "Error receiving from broker")
        assert record.levelno == logging.ERROR
        assert record.exc_info is not None
        assert record.worker_id == started.worker_id
        assert record.backend_alias == "default"
        assert record.queue_name == "emails"
        assert record.broker == "BrokenBroker"

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

    def test_worker_records_carry_the_pid_and_worker_index(
        self, task_logs, monkeypatch
    ):
        """What a supervisor's ``Worker process started`` record joins on."""
        monkeypatch.setenv(WORKER_INDEX_ENV, "2")
        simple_task.enqueue(1, 2)

        call_command("run_database_tasks", stdout=StringIO())

        for message in ("Worker started", "Task completed", "Worker finished"):
            (record,) = records_matching(task_logs, message)
            assert record.pid == os.getpid(), message
            assert record.worker_index == 2, message

    def test_a_worker_without_a_supervisor_records_no_index(
        self, task_logs, monkeypatch
    ):
        monkeypatch.delenv(WORKER_INDEX_ENV, raising=False)

        call_command("run_database_tasks", stdout=StringIO())

        (record,) = records_matching(task_logs, "Worker started")
        assert record.pid == os.getpid()
        assert not hasattr(record, "worker_index")

    def test_a_failed_receive_carries_the_worker_index(self, task_logs, monkeypatch):
        class BrokenBroker(FakePullBroker):
            def receive(self, queue_name=None, max_messages=1, wait_seconds=20):
                raise RuntimeError("broker is down")

        monkeypatch.setenv(WORKER_INDEX_ENV, "4")
        run_worker(make_backend(BrokenBroker()), source="broker")

        (record,) = records_matching(task_logs, "Error receiving from broker")
        assert record.worker_index == 4
        assert record.pid == os.getpid()
