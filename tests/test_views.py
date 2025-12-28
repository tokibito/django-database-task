"""Tests for HTTP endpoint views."""

import json

import pytest
from django.tasks.base import TaskResultStatus
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from django_database_task.models import DatabaseTask


@pytest.fixture
def client():
    """Return a Django test client."""
    return Client()


@pytest.mark.django_db
class TestRunTasksView:
    """Tests for RunTasksView."""

    def test_run_tasks_processes_pending_tasks(self, client):
        """Test that POST processes pending tasks."""
        for i in range(3):
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

        response = client.post(
            reverse("django_database_task:run_tasks"),
            data=json.dumps({"max_tasks": 10}),
            content_type="application/json",
        )

        assert response.status_code == 200
        data = response.json()
        assert data["processed"] == 3
        assert len(data["results"]) == 3

    def test_run_tasks_respects_max_tasks(self, client):
        """Test that max_tasks limit is respected."""
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

        response = client.post(
            reverse("django_database_task:run_tasks"),
            data=json.dumps({"max_tasks": 2}),
            content_type="application/json",
        )

        assert response.status_code == 200
        data = response.json()
        assert data["processed"] == 2

    def test_run_tasks_filters_by_queue(self, client):
        """Test that queue_name filter works."""
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

        response = client.post(
            reverse("django_database_task:run_tasks"),
            data=json.dumps({"queue_name": "emails"}),
            content_type="application/json",
        )

        assert response.status_code == 200
        data = response.json()
        assert data["processed"] == 1

    def test_run_tasks_returns_empty_when_no_tasks(self, client):
        """Test response when no tasks are available."""
        response = client.post(
            reverse("django_database_task:run_tasks"),
            content_type="application/json",
        )

        assert response.status_code == 200
        data = response.json()
        assert data["processed"] == 0
        assert data["results"] == []

    def test_run_tasks_rejects_get(self, client):
        """Test that GET method is not allowed."""
        response = client.get(reverse("django_database_task:run_tasks"))
        assert response.status_code == 405

    def test_run_tasks_rejects_invalid_json(self, client):
        """Test that invalid JSON returns 400."""
        response = client.post(
            reverse("django_database_task:run_tasks"),
            data="not valid json",
            content_type="application/json",
        )

        assert response.status_code == 400
        assert "Invalid JSON" in response.json()["error"]

    def test_run_tasks_rejects_invalid_max_tasks(self, client):
        """Test that invalid max_tasks returns 400."""
        response = client.post(
            reverse("django_database_task:run_tasks"),
            data=json.dumps({"max_tasks": 0}),
            content_type="application/json",
        )

        assert response.status_code == 400
        assert "positive integer" in response.json()["error"]

    def test_run_tasks_rejects_excessive_max_tasks(self, client):
        """Test that max_tasks > 100 returns 400."""
        response = client.post(
            reverse("django_database_task:run_tasks"),
            data=json.dumps({"max_tasks": 101}),
            content_type="application/json",
        )

        assert response.status_code == 400
        assert "cannot exceed 100" in response.json()["error"]


@pytest.mark.django_db
class TestRunOneTaskView:
    """Tests for RunOneTaskView."""

    def test_run_one_task_processes_single_task(self, client):
        """Test that POST processes a single task."""
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

        response = client.post(
            reverse("django_database_task:run_one_task"),
            content_type="application/json",
        )

        assert response.status_code == 200
        data = response.json()
        assert data["processed"] is True
        assert data["result"]["status"] == "SUCCESSFUL"

    def test_run_one_task_returns_false_when_no_tasks(self, client):
        """Test response when no tasks are available."""
        response = client.post(
            reverse("django_database_task:run_one_task"),
            content_type="application/json",
        )

        assert response.status_code == 200
        data = response.json()
        assert data["processed"] is False
        assert data["result"] is None

    def test_run_one_task_rejects_get(self, client):
        """Test that GET method is not allowed."""
        response = client.get(reverse("django_database_task:run_one_task"))
        assert response.status_code == 405


@pytest.mark.django_db
class TestTaskStatusView:
    """Tests for TaskStatusView."""

    def test_task_status_returns_pending_count(self, client):
        """Test that GET returns pending task count."""
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

        response = client.get(reverse("django_database_task:task_status"))

        assert response.status_code == 200
        data = response.json()
        assert data["pending_count"] == 5

    def test_task_status_filters_by_queue(self, client):
        """Test that queue_name filter works."""
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

        response = client.get(
            reverse("django_database_task:task_status"),
            {"queue_name": "emails"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["pending_count"] == 1

    def test_task_status_rejects_post(self, client):
        """Test that POST method is not allowed."""
        response = client.post(reverse("django_database_task:task_status"))
        assert response.status_code == 405


@pytest.mark.django_db
class TestExecuteTaskView:
    """Tests for ExecuteTaskView."""

    def test_execute_task_runs_specific_task(self, client):
        """Test that POST executes a specific task by ID."""
        task = DatabaseTask.objects.create(
            task_path="tests.test_executor.sample_task",
            queue_name="default",
            priority=0,
            args_json=[5, 3],
            kwargs_json={},
            status=TaskResultStatus.READY,
            enqueued_at=timezone.now(),
            backend_name="default",
        )

        response = client.post(
            reverse("django_database_task:execute_task", args=[task.id]),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["executed"] is True
        assert data["result"]["id"] == str(task.id)
        assert data["result"]["status"] == "SUCCESSFUL"

        # Verify task is now completed
        task.refresh_from_db()
        assert task.status == TaskResultStatus.SUCCESSFUL

    def test_execute_task_returns_404_for_nonexistent_task(self, client):
        """Test that 404 is returned for nonexistent task ID."""
        import uuid

        fake_id = uuid.uuid4()
        response = client.post(
            reverse("django_database_task:execute_task", args=[fake_id]),
        )

        assert response.status_code == 404
        assert response.json()["error"] == "Task not found"

    def test_execute_task_returns_false_for_non_ready_task(self, client):
        """Test that non-READY tasks are not executed."""
        task = DatabaseTask.objects.create(
            task_path="tests.test_executor.sample_task",
            queue_name="default",
            priority=0,
            args_json=[1, 2],
            kwargs_json={},
            status=TaskResultStatus.RUNNING,  # Already running
            enqueued_at=timezone.now(),
            backend_name="default",
        )

        response = client.post(
            reverse("django_database_task:execute_task", args=[task.id]),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["executed"] is False
        assert "not in READY status" in data["reason"]

    def test_execute_task_returns_false_for_completed_task(self, client):
        """Test that completed tasks are not re-executed."""
        task = DatabaseTask.objects.create(
            task_path="tests.test_executor.sample_task",
            queue_name="default",
            priority=0,
            args_json=[1, 2],
            kwargs_json={},
            status=TaskResultStatus.SUCCESSFUL,
            enqueued_at=timezone.now(),
            finished_at=timezone.now(),
            backend_name="default",
        )

        response = client.post(
            reverse("django_database_task:execute_task", args=[task.id]),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["executed"] is False

    def test_execute_task_returns_false_for_failed_task(self, client):
        """Test that failed tasks are not re-executed via execute endpoint."""
        task = DatabaseTask.objects.create(
            task_path="tests.test_executor.sample_task",
            queue_name="default",
            priority=0,
            args_json=[1, 2],
            kwargs_json={},
            status=TaskResultStatus.FAILED,
            enqueued_at=timezone.now(),
            backend_name="default",
        )

        response = client.post(
            reverse("django_database_task:execute_task", args=[task.id]),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["executed"] is False

    def test_execute_task_rejects_get(self, client):
        """Test that GET method is not allowed."""
        import uuid

        fake_id = uuid.uuid4()
        response = client.get(
            reverse("django_database_task:execute_task", args=[fake_id]),
        )
        assert response.status_code == 405

    def test_execute_task_handles_failed_execution(self, client):
        """Test that failed task execution returns proper status."""
        task = DatabaseTask.objects.create(
            task_path="tests.test_executor.failing_task",
            queue_name="default",
            priority=0,
            args_json=[],
            kwargs_json={},
            status=TaskResultStatus.READY,
            enqueued_at=timezone.now(),
            backend_name="default",
        )

        response = client.post(
            reverse("django_database_task:execute_task", args=[task.id]),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["executed"] is True
        assert data["result"]["status"] == "FAILED"

        # Verify task is now failed
        task.refresh_from_db()
        assert task.status == TaskResultStatus.FAILED

    def test_execute_task_fail_on_error_returns_500(self, client):
        """Test that fail_on_error=true returns 500 on failure."""
        task = DatabaseTask.objects.create(
            task_path="tests.test_executor.failing_task",
            queue_name="default",
            priority=0,
            args_json=[],
            kwargs_json={},
            status=TaskResultStatus.READY,
            enqueued_at=timezone.now(),
            backend_name="default",
        )

        response = client.post(
            reverse("django_database_task:execute_task", args=[task.id])
            + "?fail_on_error=true",
        )

        assert response.status_code == 500
        data = response.json()
        assert data["executed"] is True
        assert data["failed"] is True
        assert data["result"]["status"] == "FAILED"

    def test_execute_task_fail_on_error_returns_200_on_success(self, client):
        """Test that fail_on_error=true still returns 200 on success."""
        task = DatabaseTask.objects.create(
            task_path="tests.test_executor.sample_task",
            queue_name="default",
            priority=0,
            args_json=[1, 2],
            kwargs_json={},
            status=TaskResultStatus.READY,
            enqueued_at=timezone.now(),
            backend_name="default",
        )

        response = client.post(
            reverse("django_database_task:execute_task", args=[task.id])
            + "?fail_on_error=true",
        )

        assert response.status_code == 200
        data = response.json()
        assert data["executed"] is True
        assert "failed" not in data

    def test_execute_task_allow_retry_executes_failed_task(self, client):
        """Test that allow_retry=true allows re-execution of failed tasks."""
        task = DatabaseTask.objects.create(
            task_path="tests.test_executor.sample_task",
            queue_name="default",
            priority=0,
            args_json=[5, 3],
            kwargs_json={},
            status=TaskResultStatus.FAILED,  # Already failed
            enqueued_at=timezone.now(),
            finished_at=timezone.now(),
            backend_name="default",
            errors_json=[{"exception_class_path": "ValueError", "traceback": "..."}],
        )

        response = client.post(
            reverse("django_database_task:execute_task", args=[task.id])
            + "?allow_retry=true",
        )

        assert response.status_code == 200
        data = response.json()
        assert data["executed"] is True
        assert data["result"]["status"] == "SUCCESSFUL"

        # Verify task is now successful
        task.refresh_from_db()
        assert task.status == TaskResultStatus.SUCCESSFUL

    def test_execute_task_without_allow_retry_skips_failed_task(self, client):
        """Test that failed tasks are skipped without allow_retry."""
        task = DatabaseTask.objects.create(
            task_path="tests.test_executor.sample_task",
            queue_name="default",
            priority=0,
            args_json=[5, 3],
            kwargs_json={},
            status=TaskResultStatus.FAILED,
            enqueued_at=timezone.now(),
            backend_name="default",
        )

        response = client.post(
            reverse("django_database_task:execute_task", args=[task.id]),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["executed"] is False

    def test_execute_task_cloud_tasks_retry_flow(self, client):
        """Test full Cloud Tasks retry flow with fail_on_error and allow_retry."""
        # First execution - task fails
        task = DatabaseTask.objects.create(
            task_path="tests.test_executor.failing_task",
            queue_name="default",
            priority=0,
            args_json=[],
            kwargs_json={},
            status=TaskResultStatus.READY,
            enqueued_at=timezone.now(),
            backend_name="default",
        )

        # First attempt - fails with 500
        response = client.post(
            reverse("django_database_task:execute_task", args=[task.id])
            + "?fail_on_error=true&allow_retry=true",
        )
        assert response.status_code == 500
        assert response.json()["result"]["status"] == "FAILED"

        # Verify task is failed
        task.refresh_from_db()
        assert task.status == TaskResultStatus.FAILED

        # Update task to a working task path for retry simulation
        task.task_path = "tests.test_executor.sample_task"
        task.args_json = [1, 2]
        task.save()

        # Cloud Tasks retry - now succeeds
        response = client.post(
            reverse("django_database_task:execute_task", args=[task.id])
            + "?fail_on_error=true&allow_retry=true",
        )
        assert response.status_code == 200
        assert response.json()["result"]["status"] == "SUCCESSFUL"

        # Verify task is now successful
        task.refresh_from_db()
        assert task.status == TaskResultStatus.SUCCESSFUL


@pytest.mark.django_db
class TestPurgeCompletedTasksView:
    """Tests for PurgeCompletedTasksView."""

    def test_purge_deletes_completed_tasks(self, client):
        """Test that POST deletes completed tasks."""
        # Create completed tasks
        for i in range(3):
            DatabaseTask.objects.create(
                task_path="tests.test_executor.sample_task",
                queue_name="default",
                priority=0,
                args_json=[i, i],
                kwargs_json={},
                status=TaskResultStatus.SUCCESSFUL,
                enqueued_at=timezone.now(),
                finished_at=timezone.now(),
                backend_name="default",
            )

        # Create a pending task that should NOT be deleted
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

        response = client.post(
            reverse("django_database_task:purge_completed_tasks"),
            content_type="application/json",
        )

        assert response.status_code == 200
        data = response.json()
        assert data["deleted"] == 3
        assert data["dry_run"] is False

        # Verify only pending task remains
        assert DatabaseTask.objects.count() == 1
        assert DatabaseTask.objects.first().status == TaskResultStatus.READY

    def test_purge_deletes_failed_tasks(self, client):
        """Test that failed tasks are deleted by default."""
        DatabaseTask.objects.create(
            task_path="tests.test_executor.sample_task",
            queue_name="default",
            priority=0,
            args_json=[1, 2],
            kwargs_json={},
            status=TaskResultStatus.FAILED,
            enqueued_at=timezone.now(),
            finished_at=timezone.now(),
            backend_name="default",
        )

        response = client.post(
            reverse("django_database_task:purge_completed_tasks"),
            content_type="application/json",
        )

        assert response.status_code == 200
        data = response.json()
        assert data["deleted"] == 1

    def test_purge_filters_by_status(self, client):
        """Test that status filter works."""
        DatabaseTask.objects.create(
            task_path="tests.test_executor.sample_task",
            queue_name="default",
            priority=0,
            args_json=[1, 2],
            kwargs_json={},
            status=TaskResultStatus.SUCCESSFUL,
            enqueued_at=timezone.now(),
            finished_at=timezone.now(),
            backend_name="default",
        )
        DatabaseTask.objects.create(
            task_path="tests.test_executor.sample_task",
            queue_name="default",
            priority=0,
            args_json=[1, 2],
            kwargs_json={},
            status=TaskResultStatus.FAILED,
            enqueued_at=timezone.now(),
            finished_at=timezone.now(),
            backend_name="default",
        )

        response = client.post(
            reverse("django_database_task:purge_completed_tasks"),
            data=json.dumps({"status": "SUCCESSFUL"}),
            content_type="application/json",
        )

        assert response.status_code == 200
        data = response.json()
        assert data["deleted"] == 1

        # Verify only failed task remains
        assert DatabaseTask.objects.count() == 1
        assert DatabaseTask.objects.first().status == TaskResultStatus.FAILED

    def test_purge_filters_by_days(self, client):
        """Test that days filter works."""
        from datetime import timedelta

        # Task completed 10 days ago (will be deleted)
        DatabaseTask.objects.create(
            task_path="tests.test_executor.sample_task",
            queue_name="default",
            priority=0,
            args_json=[1, 2],
            kwargs_json={},
            status=TaskResultStatus.SUCCESSFUL,
            enqueued_at=timezone.now() - timedelta(days=10),
            finished_at=timezone.now() - timedelta(days=10),
            backend_name="default",
        )

        # Task completed today
        new_task = DatabaseTask.objects.create(
            task_path="tests.test_executor.sample_task",
            queue_name="default",
            priority=0,
            args_json=[1, 2],
            kwargs_json={},
            status=TaskResultStatus.SUCCESSFUL,
            enqueued_at=timezone.now(),
            finished_at=timezone.now(),
            backend_name="default",
        )

        response = client.post(
            reverse("django_database_task:purge_completed_tasks"),
            data=json.dumps({"days": 7}),
            content_type="application/json",
        )

        assert response.status_code == 200
        data = response.json()
        assert data["deleted"] == 1

        # Verify only new task remains
        assert DatabaseTask.objects.count() == 1
        assert DatabaseTask.objects.first().id == new_task.id

    def test_purge_dry_run(self, client):
        """Test that dry_run returns count without deleting."""
        for i in range(5):
            DatabaseTask.objects.create(
                task_path="tests.test_executor.sample_task",
                queue_name="default",
                priority=0,
                args_json=[i, i],
                kwargs_json={},
                status=TaskResultStatus.SUCCESSFUL,
                enqueued_at=timezone.now(),
                finished_at=timezone.now(),
                backend_name="default",
            )

        response = client.post(
            reverse("django_database_task:purge_completed_tasks"),
            data=json.dumps({"dry_run": True}),
            content_type="application/json",
        )

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 5
        assert data["dry_run"] is True

        # Verify no tasks were deleted
        assert DatabaseTask.objects.count() == 5

    def test_purge_returns_zero_when_no_tasks(self, client):
        """Test response when no tasks to delete."""
        response = client.post(
            reverse("django_database_task:purge_completed_tasks"),
            content_type="application/json",
        )

        assert response.status_code == 200
        data = response.json()
        assert data["deleted"] == 0

    def test_purge_rejects_get(self, client):
        """Test that GET method is not allowed."""
        response = client.get(reverse("django_database_task:purge_completed_tasks"))
        assert response.status_code == 405

    def test_purge_rejects_invalid_json(self, client):
        """Test that invalid JSON returns 400."""
        response = client.post(
            reverse("django_database_task:purge_completed_tasks"),
            data="not valid json",
            content_type="application/json",
        )

        assert response.status_code == 400
        assert "Invalid JSON" in response.json()["error"]

    def test_purge_rejects_invalid_days(self, client):
        """Test that invalid days returns 400."""
        response = client.post(
            reverse("django_database_task:purge_completed_tasks"),
            data=json.dumps({"days": -1}),
            content_type="application/json",
        )

        assert response.status_code == 400
        assert "non-negative integer" in response.json()["error"]

    def test_purge_rejects_invalid_batch_size(self, client):
        """Test that invalid batch_size returns 400."""
        response = client.post(
            reverse("django_database_task:purge_completed_tasks"),
            data=json.dumps({"batch_size": 0}),
            content_type="application/json",
        )

        assert response.status_code == 400
        assert "positive integer" in response.json()["error"]

    def test_purge_rejects_excessive_batch_size(self, client):
        """Test that batch_size > 10000 returns 400."""
        response = client.post(
            reverse("django_database_task:purge_completed_tasks"),
            data=json.dumps({"batch_size": 10001}),
            content_type="application/json",
        )

        assert response.status_code == 400
        assert "cannot exceed 10000" in response.json()["error"]

    def test_purge_rejects_invalid_status(self, client):
        """Test that invalid status returns 400."""
        response = client.post(
            reverse("django_database_task:purge_completed_tasks"),
            data=json.dumps({"status": "INVALID"}),
            content_type="application/json",
        )

        assert response.status_code == 400
        assert "No valid statuses" in response.json()["error"]
