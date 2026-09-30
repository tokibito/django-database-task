"""Tests for purge_completed_tasks()."""

from datetime import timedelta

import pytest
from django.tasks.base import TaskResultStatus
from django.utils import timezone

from django_database_task import purge_completed_tasks
from django_database_task.models import DatabaseTask


def make_task(status=TaskResultStatus.SUCCESSFUL, days_ago=10, **fields):
    now = timezone.now()
    finished = status in (TaskResultStatus.SUCCESSFUL, TaskResultStatus.FAILED)
    return DatabaseTask.objects.create(
        task_path=fields.pop("task_path", "tests.test_executor.sample_task"),
        queue_name="default",
        args_json=[1, 2],
        kwargs_json={},
        status=status,
        enqueued_at=now - timedelta(days=days_ago),
        finished_at=now - timedelta(days=days_ago) if finished else None,
        backend_name=fields.pop("backend_name", "default"),
        **fields,
    )


@pytest.mark.django_db
class TestPurgeCompletedTasks:
    def test_deletes_successful_and_failed_tasks(self):
        make_task(TaskResultStatus.SUCCESSFUL)
        make_task(TaskResultStatus.FAILED)

        assert purge_completed_tasks() == 2
        assert DatabaseTask.objects.count() == 0

    def test_keeps_the_last_week_by_default(self):
        old = make_task(days_ago=8)
        recent = make_task(days_ago=6)

        assert purge_completed_tasks() == 1
        assert not DatabaseTask.objects.filter(id=old.id).exists()
        assert DatabaseTask.objects.filter(id=recent.id).exists()

    def test_days_zero_deletes_every_completed_task(self):
        make_task(days_ago=0)
        make_task(days_ago=30)

        assert purge_completed_tasks(days=0) == 2
        assert DatabaseTask.objects.count() == 0

    def test_never_deletes_ready_or_running_tasks(self):
        make_task(TaskResultStatus.READY)
        make_task(TaskResultStatus.RUNNING)

        assert purge_completed_tasks(days=0) == 0
        assert DatabaseTask.objects.count() == 2

    def test_statuses_limits_the_statuses_deleted(self):
        make_task(TaskResultStatus.SUCCESSFUL)
        failed = make_task(TaskResultStatus.FAILED)

        assert purge_completed_tasks(statuses=[TaskResultStatus.SUCCESSFUL]) == 1
        assert list(DatabaseTask.objects.values_list("id", flat=True)) == [failed.id]

    def test_statuses_accepts_strings(self):
        make_task(TaskResultStatus.FAILED)

        assert purge_completed_tasks(statuses=["FAILED"]) == 1

    def test_only_purges_the_default_backend_by_default(self):
        make_task(backend_name="default")
        other = make_task(backend_name="other")

        assert purge_completed_tasks() == 1
        assert list(DatabaseTask.objects.values_list("id", flat=True)) == [other.id]

    def test_backend_name_selects_the_backend(self):
        default = make_task(backend_name="default")
        make_task(backend_name="other")

        assert purge_completed_tasks(backend_name="other") == 1
        assert list(DatabaseTask.objects.values_list("id", flat=True)) == [default.id]

    def test_backend_name_none_purges_every_backend(self):
        make_task(backend_name="default")
        make_task(backend_name="other")

        assert purge_completed_tasks(backend_name=None) == 2
        assert DatabaseTask.objects.count() == 0

    def test_task_path_limits_the_tasks_deleted(self):
        make_task(task_path="myapp.tasks.heartbeat")
        kept = make_task(task_path="myapp.tasks.report")

        assert purge_completed_tasks(task_path="myapp.tasks.heartbeat") == 1
        assert list(DatabaseTask.objects.values_list("id", flat=True)) == [kept.id]

    def test_dry_run_counts_without_deleting(self):
        make_task()
        make_task()

        assert purge_completed_tasks(dry_run=True) == 2
        assert DatabaseTask.objects.count() == 2

    def test_deletes_in_batches(self):
        for _ in range(5):
            make_task()

        assert purge_completed_tasks(batch_size=2) == 5
        assert DatabaseTask.objects.count() == 0

    def test_negative_days_is_refused(self):
        make_task()

        with pytest.raises(ValueError, match="must not be negative"):
            purge_completed_tasks(days=-1)
        assert DatabaseTask.objects.count() == 1

    @pytest.mark.parametrize("batch_size", [0, -1])
    def test_batch_size_below_one_is_refused(self, batch_size):
        make_task()

        with pytest.raises(ValueError, match="at least 1"):
            purge_completed_tasks(batch_size=batch_size)
        assert DatabaseTask.objects.count() == 1

    @pytest.mark.parametrize(
        "status", [TaskResultStatus.READY, TaskResultStatus.RUNNING, "BOGUS"]
    )
    def test_a_status_that_is_not_completed_is_refused(self, status):
        make_task(TaskResultStatus.READY)
        make_task(TaskResultStatus.SUCCESSFUL)

        with pytest.raises(ValueError, match="Only completed tasks"):
            purge_completed_tasks(statuses=[TaskResultStatus.SUCCESSFUL, status])
        assert DatabaseTask.objects.count() == 2
