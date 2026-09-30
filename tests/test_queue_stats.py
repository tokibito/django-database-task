"""Tests for the monitoring API: get_task_counts() and get_queue_stats()."""

from datetime import timedelta

import pytest
from django.tasks.base import TaskResultStatus
from django.utils import timezone

from django_database_task import (
    get_pending_task_count,
    get_queue_stats,
    get_task_counts,
)
from django_database_task.backends import DatabaseTaskBackend
from django_database_task.models import DatabaseTask


def make_task(status=TaskResultStatus.READY, **fields):
    fields.setdefault("enqueued_at", timezone.now())
    return DatabaseTask.objects.create(
        task_path="tests.test_executor.sample_task",
        queue_name=fields.pop("queue_name", "default"),
        args_json=[1, 2],
        kwargs_json={},
        status=status,
        backend_name=fields.pop("backend_name", "default"),
        **fields,
    )


EMPTY_STATS = {
    "pending_count": 0,
    "running_count": 0,
    "successful_count": 0,
    "failed_count": 0,
    "delayed_count": 0,
    "oldest_pending_waiting_since": None,
    "newest_pending_waiting_since": None,
}


@pytest.mark.django_db
class TestGetTaskCounts:
    def test_every_status_is_counted(self):
        make_task(TaskResultStatus.READY)
        make_task(TaskResultStatus.READY)
        make_task(TaskResultStatus.RUNNING)
        make_task(TaskResultStatus.SUCCESSFUL)
        make_task(TaskResultStatus.SUCCESSFUL)
        make_task(TaskResultStatus.SUCCESSFUL)
        make_task(TaskResultStatus.FAILED)

        assert get_task_counts() == {
            TaskResultStatus.READY: 2,
            TaskResultStatus.RUNNING: 1,
            TaskResultStatus.SUCCESSFUL: 3,
            TaskResultStatus.FAILED: 1,
        }

    def test_an_empty_store_counts_zero_for_every_status(self):
        assert get_task_counts() == {status: 0 for status in TaskResultStatus}

    def test_ready_includes_the_tasks_not_due_yet(self):
        make_task(run_after=timezone.now() + timedelta(hours=1))

        assert get_task_counts()[TaskResultStatus.READY] == 1

    def test_queue_name_limits_the_counts_to_one_queue(self):
        make_task(queue_name="emails")
        make_task(queue_name="default")
        make_task(TaskResultStatus.FAILED, queue_name="default")

        counts = get_task_counts(queue_name="emails")

        assert counts[TaskResultStatus.READY] == 1
        assert counts[TaskResultStatus.FAILED] == 0

    def test_the_tasks_of_other_backends_are_not_counted(self):
        make_task(backend_name="other")

        assert get_task_counts()[TaskResultStatus.READY] == 0

    def test_it_returns_what_the_backend_returns(self, monkeypatch):
        monkeypatch.setattr(
            DatabaseTaskBackend,
            "get_status_counts",
            lambda self, queue_name=None: {"queue": queue_name},
        )

        assert get_task_counts(queue_name="emails") == {"queue": "emails"}


@pytest.mark.django_db
class TestGetQueueStats:
    def test_an_empty_queue(self):
        assert get_queue_stats() == EMPTY_STATS

    def test_the_counts_per_status(self):
        make_task(TaskResultStatus.READY)
        make_task(TaskResultStatus.RUNNING)
        make_task(TaskResultStatus.RUNNING)
        make_task(TaskResultStatus.SUCCESSFUL)
        make_task(TaskResultStatus.FAILED)
        make_task(TaskResultStatus.FAILED)
        make_task(TaskResultStatus.FAILED)

        stats = get_queue_stats()

        assert stats["pending_count"] == 1
        assert stats["running_count"] == 2
        assert stats["successful_count"] == 1
        assert stats["failed_count"] == 3
        assert stats["delayed_count"] == 0

    def test_a_task_not_due_yet_is_delayed_not_pending(self):
        make_task()
        make_task(run_after=timezone.now() + timedelta(hours=1))

        stats = get_queue_stats()

        assert stats["pending_count"] == 1
        assert stats["delayed_count"] == 1

    def test_pending_count_is_the_count_of_get_pending_task_count(self):
        make_task()
        make_task(run_after=timezone.now() - timedelta(minutes=1))
        make_task(run_after=timezone.now() + timedelta(hours=1))
        make_task(TaskResultStatus.RUNNING)

        assert get_queue_stats()["pending_count"] == get_pending_task_count() == 2

    def test_pending_and_delayed_add_up_to_the_ready_count(self):
        make_task()
        make_task(run_after=timezone.now() + timedelta(hours=1))
        make_task(run_after=timezone.now() + timedelta(days=1))

        stats = get_queue_stats()

        assert (
            stats["pending_count"] + stats["delayed_count"]
            == get_task_counts()[TaskResultStatus.READY]
        )

    def test_the_oldest_and_newest_pending_task(self):
        now = timezone.now()
        make_task(enqueued_at=now - timedelta(minutes=30))
        make_task(enqueued_at=now - timedelta(minutes=10))
        make_task(enqueued_at=now - timedelta(minutes=20))

        stats = get_queue_stats()

        assert stats["oldest_pending_waiting_since"] == now - timedelta(minutes=30)
        assert stats["newest_pending_waiting_since"] == now - timedelta(minutes=10)

    def test_a_delayed_task_starts_waiting_when_it_becomes_due(self):
        now = timezone.now()
        make_task(
            enqueued_at=now - timedelta(hours=2),
            run_after=now - timedelta(minutes=5),
        )

        stats = get_queue_stats()

        assert stats["oldest_pending_waiting_since"] == now - timedelta(minutes=5)
        assert stats["newest_pending_waiting_since"] == now - timedelta(minutes=5)

    def test_a_run_after_before_the_enqueue_does_not_move_the_wait_back(self):
        now = timezone.now()
        make_task(
            enqueued_at=now - timedelta(minutes=5),
            run_after=now - timedelta(hours=2),
        )

        stats = get_queue_stats()

        assert stats["oldest_pending_waiting_since"] == now - timedelta(minutes=5)

    def test_a_task_not_due_yet_is_not_queue_age(self):
        now = timezone.now()
        make_task(
            enqueued_at=now - timedelta(hours=2),
            run_after=now + timedelta(hours=1),
        )

        stats = get_queue_stats()

        assert stats["delayed_count"] == 1
        assert stats["oldest_pending_waiting_since"] is None
        assert stats["newest_pending_waiting_since"] is None

    def test_tasks_in_other_statuses_are_not_queue_age(self):
        now = timezone.now()
        make_task(TaskResultStatus.RUNNING, enqueued_at=now - timedelta(hours=3))
        make_task(TaskResultStatus.FAILED, enqueued_at=now - timedelta(hours=2))
        make_task(enqueued_at=now - timedelta(minutes=1))

        stats = get_queue_stats()

        assert stats["oldest_pending_waiting_since"] == now - timedelta(minutes=1)

    def test_queue_name_limits_the_stats_to_one_queue(self):
        now = timezone.now()
        make_task(queue_name="emails", enqueued_at=now - timedelta(minutes=1))
        make_task(queue_name="default", enqueued_at=now - timedelta(hours=1))
        make_task(TaskResultStatus.RUNNING, queue_name="default")

        stats = get_queue_stats(queue_name="emails")

        assert stats["pending_count"] == 1
        assert stats["running_count"] == 0
        assert stats["oldest_pending_waiting_since"] == now - timedelta(minutes=1)

    def test_the_tasks_of_other_backends_are_not_counted(self):
        make_task(backend_name="other")
        make_task(TaskResultStatus.FAILED, backend_name="other")

        assert get_queue_stats() == EMPTY_STATS

    def test_the_keys_match_django_tasks_redis(self):
        assert list(get_queue_stats()) == list(EMPTY_STATS)

    def test_it_returns_what_the_backend_returns(self, monkeypatch):
        monkeypatch.setattr(
            DatabaseTaskBackend,
            "get_queue_stats",
            lambda self, queue_name=None: {"queue": queue_name},
        )

        assert get_queue_stats(queue_name="emails") == {"queue": "emails"}
