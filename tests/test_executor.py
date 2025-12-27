"""Tests for the executor module (public API)."""

from datetime import timedelta

import pytest
from django.tasks import task
from django.tasks.base import TaskResultStatus
from django.utils import timezone

from django_database_task import (
    fetch_task,
    get_pending_task_count,
    process_one_task,
    process_tasks,
)
from django_database_task.models import DatabaseTask


@task
def sample_task(x, y):
    """Sample task for testing."""
    return x + y


@task
def failing_task():
    """Task that always fails."""
    raise ValueError("Intentional failure")


@pytest.mark.django_db
class TestFetchTask:
    """Tests for fetch_task function."""

    def test_fetch_task_returns_pending_task(self):
        """Test that fetch_task returns a pending task."""
        db_task = DatabaseTask.objects.create(
            task_path="tests.test_executor.sample_task",
            queue_name="default",
            priority=0,
            args_json=[1, 2],
            kwargs_json={},
            status=TaskResultStatus.READY,
            enqueued_at=timezone.now(),
            backend_name="default",
        )

        fetched = fetch_task()
        assert fetched is not None
        assert fetched.id == db_task.id

    def test_fetch_task_returns_none_when_empty(self):
        """Test that fetch_task returns None when no tasks available."""
        fetched = fetch_task()
        assert fetched is None

    def test_fetch_task_respects_queue_name(self):
        """Test that fetch_task filters by queue name."""
        DatabaseTask.objects.create(
            task_path="tests.test_executor.sample_task",
            queue_name="emails",
            priority=0,
            args_json=[1, 2],
            kwargs_json={},
            status=TaskResultStatus.READY,
            enqueued_at=timezone.now(),
            backend_name="default",
        )

        # Should not find task in different queue
        fetched = fetch_task(queue_name="other")
        assert fetched is None

        # Should find task in correct queue
        fetched = fetch_task(queue_name="emails")
        assert fetched is not None

    def test_fetch_task_respects_priority(self):
        """Test that fetch_task returns highest priority task first."""
        DatabaseTask.objects.create(
            task_path="tests.test_executor.sample_task",
            queue_name="default",
            priority=-10,
            args_json=[1, 2],
            kwargs_json={},
            status=TaskResultStatus.READY,
            enqueued_at=timezone.now(),
            backend_name="default",
        )

        high_priority = DatabaseTask.objects.create(
            task_path="tests.test_executor.sample_task",
            queue_name="default",
            priority=10,
            args_json=[3, 4],
            kwargs_json={},
            status=TaskResultStatus.READY,
            enqueued_at=timezone.now(),
            backend_name="default",
        )

        fetched = fetch_task()
        assert fetched.id == high_priority.id

    def test_fetch_task_respects_run_after(self):
        """Test that fetch_task respects run_after timestamp."""
        DatabaseTask.objects.create(
            task_path="tests.test_executor.sample_task",
            queue_name="default",
            priority=0,
            args_json=[1, 2],
            kwargs_json={},
            status=TaskResultStatus.READY,
            run_after=timezone.now() + timedelta(hours=1),
            enqueued_at=timezone.now(),
            backend_name="default",
        )

        # Should not find future task
        fetched = fetch_task()
        assert fetched is None


@pytest.mark.django_db
class TestProcessOneTask:
    """Tests for process_one_task function."""

    def test_process_one_task_executes_task(self):
        """Test that process_one_task executes a task."""
        DatabaseTask.objects.create(
            task_path="tests.test_executor.sample_task",
            queue_name="default",
            priority=0,
            args_json=[5, 3],
            kwargs_json={},
            status=TaskResultStatus.READY,
            enqueued_at=timezone.now(),
            backend_name="default",
        )

        result = process_one_task()

        assert result is not None
        assert result.status == TaskResultStatus.SUCCESSFUL
        assert result.return_value == 8

    def test_process_one_task_returns_none_when_empty(self):
        """Test that process_one_task returns None when no tasks."""
        result = process_one_task()
        assert result is None

    def test_process_one_task_handles_failure(self):
        """Test that process_one_task handles task failure."""
        DatabaseTask.objects.create(
            task_path="tests.test_executor.failing_task",
            queue_name="default",
            priority=0,
            args_json=[],
            kwargs_json={},
            status=TaskResultStatus.READY,
            enqueued_at=timezone.now(),
            backend_name="default",
        )

        result = process_one_task()

        assert result is not None
        assert result.status == TaskResultStatus.FAILED
        assert len(result.errors) > 0

    def test_process_one_task_updates_worker_id(self):
        """Test that process_one_task sets worker ID."""
        db_task = DatabaseTask.objects.create(
            task_path="tests.test_executor.sample_task",
            queue_name="default",
            priority=0,
            args_json=[1, 2],
            kwargs_json={},
            status=TaskResultStatus.READY,
            enqueued_at=timezone.now(),
            backend_name="default",
        )

        process_one_task(worker_id="test-worker-123")

        db_task.refresh_from_db()
        assert "test-worker-123" in db_task.worker_ids_json


@pytest.mark.django_db
class TestProcessTasks:
    """Tests for process_tasks function."""

    def test_process_tasks_executes_multiple_tasks(self):
        """Test that process_tasks executes multiple tasks."""
        for i in range(5):
            DatabaseTask.objects.create(
                task_path="tests.test_executor.sample_task",
                queue_name="default",
                priority=0,
                args_json=[i, i],
                kwargs_json={},
                status=TaskResultStatus.READY,
                enqueued_at=timezone.now(),
                backend_name="default",
            )

        results = process_tasks()

        assert len(results) == 5
        assert all(r.status == TaskResultStatus.SUCCESSFUL for r in results)

    def test_process_tasks_respects_max_tasks(self):
        """Test that process_tasks respects max_tasks limit."""
        for i in range(10):
            DatabaseTask.objects.create(
                task_path="tests.test_executor.sample_task",
                queue_name="default",
                priority=0,
                args_json=[i, i],
                kwargs_json={},
                status=TaskResultStatus.READY,
                enqueued_at=timezone.now(),
                backend_name="default",
            )

        results = process_tasks(max_tasks=3)

        assert len(results) == 3

        # 7 tasks should remain
        remaining = DatabaseTask.objects.filter(status=TaskResultStatus.READY).count()
        assert remaining == 7

    def test_process_tasks_returns_empty_list_when_no_tasks(self):
        """Test that process_tasks returns empty list when no tasks."""
        results = process_tasks()
        assert results == []

    def test_process_tasks_filters_by_queue(self):
        """Test that process_tasks filters by queue name."""
        DatabaseTask.objects.create(
            task_path="tests.test_executor.sample_task",
            queue_name="emails",
            priority=0,
            args_json=[1, 2],
            kwargs_json={},
            status=TaskResultStatus.READY,
            enqueued_at=timezone.now(),
            backend_name="default",
        )
        DatabaseTask.objects.create(
            task_path="tests.test_executor.sample_task",
            queue_name="default",
            priority=0,
            args_json=[3, 4],
            kwargs_json={},
            status=TaskResultStatus.READY,
            enqueued_at=timezone.now(),
            backend_name="default",
        )

        results = process_tasks(queue_name="emails")

        assert len(results) == 1
        assert results[0].return_value == 3


@pytest.mark.django_db
class TestGetPendingTaskCount:
    """Tests for get_pending_task_count function."""

    def test_get_pending_task_count_returns_correct_count(self):
        """Test that get_pending_task_count returns correct count."""
        for i in range(5):
            DatabaseTask.objects.create(
                task_path="tests.test_executor.sample_task",
                queue_name="default",
                priority=0,
                args_json=[i, i],
                kwargs_json={},
                status=TaskResultStatus.READY,
                enqueued_at=timezone.now(),
                backend_name="default",
            )

        count = get_pending_task_count()
        assert count == 5

    def test_get_pending_task_count_excludes_completed_tasks(self):
        """Test that get_pending_task_count excludes completed tasks."""
        DatabaseTask.objects.create(
            task_path="tests.test_executor.sample_task",
            queue_name="default",
            priority=0,
            args_json=[1, 2],
            kwargs_json={},
            status=TaskResultStatus.READY,
            enqueued_at=timezone.now(),
            backend_name="default",
        )
        DatabaseTask.objects.create(
            task_path="tests.test_executor.sample_task",
            queue_name="default",
            priority=0,
            args_json=[3, 4],
            kwargs_json={},
            status=TaskResultStatus.SUCCESSFUL,
            enqueued_at=timezone.now(),
            backend_name="default",
        )

        count = get_pending_task_count()
        assert count == 1

    def test_get_pending_task_count_filters_by_queue(self):
        """Test that get_pending_task_count filters by queue name."""
        DatabaseTask.objects.create(
            task_path="tests.test_executor.sample_task",
            queue_name="emails",
            priority=0,
            args_json=[1, 2],
            kwargs_json={},
            status=TaskResultStatus.READY,
            enqueued_at=timezone.now(),
            backend_name="default",
        )
        DatabaseTask.objects.create(
            task_path="tests.test_executor.sample_task",
            queue_name="default",
            priority=0,
            args_json=[3, 4],
            kwargs_json={},
            status=TaskResultStatus.READY,
            enqueued_at=timezone.now(),
            backend_name="default",
        )

        count = get_pending_task_count(queue_name="emails")
        assert count == 1

    def test_get_pending_task_count_excludes_future_tasks(self):
        """Test that get_pending_task_count excludes future tasks."""
        DatabaseTask.objects.create(
            task_path="tests.test_executor.sample_task",
            queue_name="default",
            priority=0,
            args_json=[1, 2],
            kwargs_json={},
            status=TaskResultStatus.READY,
            enqueued_at=timezone.now(),
            backend_name="default",
        )
        DatabaseTask.objects.create(
            task_path="tests.test_executor.sample_task",
            queue_name="default",
            priority=0,
            args_json=[3, 4],
            kwargs_json={},
            status=TaskResultStatus.READY,
            run_after=timezone.now() + timedelta(hours=1),
            enqueued_at=timezone.now(),
            backend_name="default",
        )

        count = get_pending_task_count()
        assert count == 1
