"""Tests for recovering tasks left in RUNNING status."""

from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.tasks import task_backends
from django.tasks.base import TaskResultStatus
from django.test import override_settings
from django.utils import timezone

from django_database_task.executor import WORKER_LOST_PATH, requeue_stale_tasks
from django_database_task.management.commands.requeue_stale_database_tasks import (
    parse_older_than,
)
from django_database_task.models import DatabaseTask


def make_running_task(
    minutes_ago=60,
    worker_ids=None,
    queue_name="default",
    backend_name="default",
    status=TaskResultStatus.RUNNING,
    **extra,
):
    """Create a task that a worker took `minutes_ago` and never finished."""
    attempted_at = timezone.now() - timedelta(minutes=minutes_ago)
    fields = {
        "task_path": "tests.tasks.simple_task",
        "queue_name": queue_name,
        "args_json": [1, 2],
        "kwargs_json": {},
        "status": status,
        "enqueued_at": attempted_at,
        "started_at": attempted_at,
        "last_attempted_at": attempted_at,
        "worker_ids_json": ["worker-1"] if worker_ids is None else worker_ids,
        "backend_name": backend_name,
    }
    fields.update(extra)
    return DatabaseTask.objects.create(**fields)


@pytest.mark.django_db
class TestRequeueStaleTasks:
    """Tests for the requeue_stale_tasks() API."""

    def test_a_stale_task_goes_back_to_ready(self):
        db_task = make_running_task(minutes_ago=60)

        summary = requeue_stale_tasks(timedelta(minutes=15))

        db_task.refresh_from_db()
        assert db_task.status == TaskResultStatus.READY
        assert summary == {"found": 1, "requeued": 1, "failed": 0}

    def test_a_requeued_task_starts_from_scratch(self):
        db_task = make_running_task(
            minutes_ago=60,
            finished_at=timezone.now(),
            return_value_json="half a result",
        )

        requeue_stale_tasks(timedelta(minutes=15))

        db_task.refresh_from_db()
        assert db_task.started_at is None
        assert db_task.finished_at is None
        assert db_task.return_value_json is None

    def test_a_requeued_task_keeps_the_workers_it_was_handed_to(self):
        """They are the attempt count max_attempts works from."""
        db_task = make_running_task(minutes_ago=60, worker_ids=["worker-1"])

        requeue_stale_tasks(timedelta(minutes=15))

        db_task.refresh_from_db()
        assert db_task.worker_ids_json == ["worker-1"]
        assert db_task.last_attempted_at is not None

    def test_a_task_running_for_less_than_the_threshold_is_left_alone(self):
        db_task = make_running_task(minutes_ago=5)

        summary = requeue_stale_tasks(timedelta(minutes=15))

        db_task.refresh_from_db()
        assert db_task.status == TaskResultStatus.RUNNING
        assert summary["found"] == 0

    @pytest.mark.parametrize(
        "status",
        [
            TaskResultStatus.READY,
            TaskResultStatus.SUCCESSFUL,
            TaskResultStatus.FAILED,
        ],
    )
    def test_only_running_tasks_are_recovered(self, status):
        db_task = make_running_task(minutes_ago=60, status=status)

        summary = requeue_stale_tasks(timedelta(minutes=15))

        db_task.refresh_from_db()
        assert db_task.status == status
        assert summary["found"] == 0

    def test_a_running_task_that_was_never_attempted_is_left_alone(self):
        """Nothing says when it started, so nothing says it is stale."""
        db_task = make_running_task(minutes_ago=60, last_attempted_at=None)

        requeue_stale_tasks(timedelta(minutes=15))

        db_task.refresh_from_db()
        assert db_task.status == TaskResultStatus.RUNNING

    def test_the_queue_can_be_narrowed(self):
        emails = make_running_task(minutes_ago=60, queue_name="emails")
        reports = make_running_task(minutes_ago=60, queue_name="reports")

        requeue_stale_tasks(timedelta(minutes=15), queue_name="emails")

        emails.refresh_from_db()
        reports.refresh_from_db()
        assert emails.status == TaskResultStatus.READY
        assert reports.status == TaskResultStatus.RUNNING

    def test_the_backend_can_be_narrowed(self):
        default = make_running_task(minutes_ago=60, backend_name="default")
        other = make_running_task(minutes_ago=60, backend_name="other")

        requeue_stale_tasks(timedelta(minutes=15), backend_name="default")

        default.refresh_from_db()
        other.refresh_from_db()
        assert default.status == TaskResultStatus.READY
        assert other.status == TaskResultStatus.RUNNING

    def test_every_backend_is_recovered_by_default(self):
        default = make_running_task(minutes_ago=60, backend_name="default")
        other = make_running_task(minutes_ago=60, backend_name="other")

        requeue_stale_tasks(timedelta(minutes=15))

        default.refresh_from_db()
        other.refresh_from_db()
        assert default.status == TaskResultStatus.READY
        assert other.status == TaskResultStatus.READY

    def test_more_than_one_task_is_recovered_at_a_time(self):
        for _ in range(5):
            make_running_task(minutes_ago=60)

        summary = requeue_stale_tasks(timedelta(minutes=15), batch_size=2)

        assert summary == {"found": 5, "requeued": 5, "failed": 0}
        assert DatabaseTask.objects.filter(status=TaskResultStatus.READY).count() == 5


@pytest.mark.django_db
class TestGivingUp:
    """Tests for marking a stale task FAILED instead of requeueing it."""

    def test_a_task_that_used_up_its_attempts_is_failed(self):
        db_task = make_running_task(
            minutes_ago=60, worker_ids=["worker-1", "worker-2", "worker-3"]
        )

        summary = requeue_stale_tasks(timedelta(minutes=15), max_attempts=3)

        db_task.refresh_from_db()
        assert db_task.status == TaskResultStatus.FAILED
        assert summary == {"found": 1, "requeued": 0, "failed": 1}

    def test_a_failed_task_records_that_its_worker_was_lost(self):
        db_task = make_running_task(minutes_ago=60, worker_ids=["worker-1"])

        requeue_stale_tasks(timedelta(minutes=15), mark_failed=True)

        db_task.refresh_from_db()
        assert len(db_task.errors_json) == 1
        error = db_task.errors_json[0]
        assert error["exception_class_path"] == WORKER_LOST_PATH
        assert "worker-1" in error["traceback"]

    def test_a_failed_task_can_be_purged(self):
        """purge --days works off finished_at, so it has to be set."""
        db_task = make_running_task(minutes_ago=60)

        requeue_stale_tasks(timedelta(minutes=15), mark_failed=True)

        db_task.refresh_from_db()
        assert db_task.finished_at is not None

    def test_an_earlier_error_is_kept(self):
        db_task = make_running_task(
            minutes_ago=60,
            errors_json=[{"exception_class_path": "builtins.ValueError", "tb": ""}],
        )

        requeue_stale_tasks(timedelta(minutes=15), mark_failed=True)

        db_task.refresh_from_db()
        assert len(db_task.errors_json) == 2

    def test_a_task_with_attempts_left_is_still_requeued(self):
        db_task = make_running_task(minutes_ago=60, worker_ids=["worker-1"])

        requeue_stale_tasks(timedelta(minutes=15), max_attempts=3)

        db_task.refresh_from_db()
        assert db_task.status == TaskResultStatus.READY

    def test_no_limit_never_gives_up(self):
        db_task = make_running_task(
            minutes_ago=60, worker_ids=[f"worker-{n}" for n in range(20)]
        )

        requeue_stale_tasks(timedelta(minutes=15), max_attempts=0)

        db_task.refresh_from_db()
        assert db_task.status == TaskResultStatus.READY

    def test_mark_failed_never_requeues(self):
        db_task = make_running_task(minutes_ago=60, worker_ids=[])

        summary = requeue_stale_tasks(timedelta(minutes=15), mark_failed=True)

        db_task.refresh_from_db()
        assert db_task.status == TaskResultStatus.FAILED
        assert summary == {"found": 1, "requeued": 0, "failed": 1}


@pytest.mark.django_db
class TestDryRun:
    def test_nothing_is_changed(self):
        db_task = make_running_task(minutes_ago=60)

        requeue_stale_tasks(timedelta(minutes=15), dry_run=True)

        db_task.refresh_from_db()
        assert db_task.status == TaskResultStatus.RUNNING

    def test_the_counts_are_reported(self):
        make_running_task(minutes_ago=60, worker_ids=["worker-1"])
        make_running_task(minutes_ago=60, worker_ids=["w-1", "w-2", "w-3"])

        summary = requeue_stale_tasks(
            timedelta(minutes=15), max_attempts=3, dry_run=True
        )

        assert summary == {"found": 2, "requeued": 1, "failed": 1}


class RecordingBroker:
    """A broker that records what it was asked to notify about."""

    def __init__(self, backend, options=None):
        self.backend = backend
        self.notified = []

    def notify(self, task_result):
        self.notified.append(task_result)

    def get_auth_handlers(self, endpoint=None):
        return []


class BrokenBroker(RecordingBroker):
    """A broker that is down."""

    def notify(self, task_result):
        raise RuntimeError("broker is down")


def tasks_setting(broker):
    return {
        "default": {
            "BACKEND": "django_database_task.backends.DatabaseTaskBackend",
            "QUEUES": [],
            "OPTIONS": {"BROKER": broker},
        },
    }


@pytest.mark.django_db
class TestBrokerNotification:
    """Tests for telling a broker about a task that was requeued."""

    def test_the_broker_is_told_about_a_requeued_task(self):
        db_task = make_running_task(minutes_ago=60)

        with override_settings(TASKS=tasks_setting(RecordingBroker)):
            requeue_stale_tasks(timedelta(minutes=15), notify_broker=True)
            notified = task_backends["default"].broker.notified

        assert [result.id for result in notified] == [str(db_task.id)]

    def test_the_broker_is_not_told_by_default(self):
        make_running_task(minutes_ago=60)

        with override_settings(TASKS=tasks_setting(RecordingBroker)):
            requeue_stale_tasks(timedelta(minutes=15))
            notified = task_backends["default"].broker.notified

        assert notified == []

    def test_a_task_marked_failed_is_not_sent_to_the_broker(self):
        make_running_task(minutes_ago=60)

        with override_settings(TASKS=tasks_setting(RecordingBroker)):
            requeue_stale_tasks(
                timedelta(minutes=15), mark_failed=True, notify_broker=True
            )
            notified = task_backends["default"].broker.notified

        assert notified == []

    def test_a_broker_that_is_down_does_not_undo_the_requeue(self):
        db_task = make_running_task(minutes_ago=60)

        with override_settings(TASKS=tasks_setting(BrokenBroker)):
            summary = requeue_stale_tasks(timedelta(minutes=15), notify_broker=True)

        db_task.refresh_from_db()
        assert db_task.status == TaskResultStatus.READY
        assert summary["requeued"] == 1


class TestParseDuration:
    """Tests for the --older-than value."""

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("90s", timedelta(seconds=90)),
            ("15m", timedelta(minutes=15)),
            ("2h", timedelta(hours=2)),
            ("1d", timedelta(days=1)),
            (" 15m ", timedelta(minutes=15)),
        ],
    )
    def test_a_number_and_a_unit_are_accepted(self, value, expected):
        assert parse_older_than(value) == expected

    @pytest.mark.parametrize(
        "value",
        [
            "15",  # a bare number is ambiguous
            "",
            "m",
            "15 m",
            "1.5h",
            "-5m",
            "15w",
            "0m",
            "fifteen minutes",
        ],
    )
    def test_anything_else_is_rejected(self, value):
        with pytest.raises(CommandError):
            parse_older_than(value)


@pytest.mark.django_db
class TestRequeueStaleDatabaseTasksCommand:
    """Tests for the requeue_stale_database_tasks management command."""

    def test_a_stale_task_is_requeued(self):
        db_task = make_running_task(minutes_ago=60)

        call_command(
            "requeue_stale_database_tasks", older_than="15m", stdout=StringIO()
        )

        db_task.refresh_from_db()
        assert db_task.status == TaskResultStatus.READY

    def test_older_than_is_required(self):
        with pytest.raises(CommandError):
            call_command("requeue_stale_database_tasks", stdout=StringIO())

    def test_an_unreadable_older_than_is_rejected(self):
        with pytest.raises(CommandError):
            call_command(
                "requeue_stale_database_tasks", older_than="15", stdout=StringIO()
            )

    def test_a_negative_max_attempts_is_rejected(self):
        with pytest.raises(CommandError):
            call_command(
                "requeue_stale_database_tasks",
                older_than="15m",
                max_attempts=-1,
                stdout=StringIO(),
            )

    def test_a_batch_size_below_one_is_rejected(self):
        with pytest.raises(CommandError):
            call_command(
                "requeue_stale_database_tasks",
                older_than="15m",
                batch_size=0,
                stdout=StringIO(),
            )

    def test_the_queue_can_be_narrowed(self):
        emails = make_running_task(minutes_ago=60, queue_name="emails")
        reports = make_running_task(minutes_ago=60, queue_name="reports")

        call_command(
            "requeue_stale_database_tasks",
            older_than="15m",
            queue="emails",
            stdout=StringIO(),
        )

        emails.refresh_from_db()
        reports.refresh_from_db()
        assert emails.status == TaskResultStatus.READY
        assert reports.status == TaskResultStatus.RUNNING

    def test_mark_failed(self):
        db_task = make_running_task(minutes_ago=60)

        call_command(
            "requeue_stale_database_tasks",
            older_than="15m",
            mark_failed=True,
            stdout=StringIO(),
        )

        db_task.refresh_from_db()
        assert db_task.status == TaskResultStatus.FAILED

    def test_dry_run_changes_nothing(self):
        db_task = make_running_task(minutes_ago=60)
        out = StringIO()

        call_command(
            "requeue_stale_database_tasks",
            older_than="15m",
            dry_run=True,
            stdout=out,
        )

        db_task.refresh_from_db()
        assert db_task.status == TaskResultStatus.RUNNING
        assert "Dry run" in out.getvalue()

    def test_what_happened_is_reported(self):
        make_running_task(minutes_ago=60)
        out = StringIO()

        call_command("requeue_stale_database_tasks", older_than="15m", stdout=out)

        output = out.getvalue()
        assert "Found 1 stale tasks" in output
        assert "Requeued 1 tasks" in output

    def test_nothing_to_do_is_reported(self):
        out = StringIO()

        call_command("requeue_stale_database_tasks", older_than="15m", stdout=out)

        assert "No stale tasks to recover" in out.getvalue()

    def test_verbosity_zero_says_nothing(self):
        make_running_task(minutes_ago=60)
        out = StringIO()

        call_command(
            "requeue_stale_database_tasks",
            older_than="15m",
            verbosity=0,
            stdout=out,
        )

        assert out.getvalue() == ""
