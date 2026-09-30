"""Tests for HTTP endpoint views."""

import json
from datetime import timedelta

import pytest
from django.http import JsonResponse
from django.tasks.base import TaskResultStatus
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from django_database_task.backends import DatabaseTaskBackend
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

    def test_task_status_returns_the_queue_stats(self, client):
        """The response carries get_queue_stats() alongside pending_count."""
        now = timezone.now()
        for status, enqueued_at, run_after in [
            (TaskResultStatus.READY, now - timedelta(minutes=30), None),
            (TaskResultStatus.READY, now - timedelta(minutes=10), None),
            (TaskResultStatus.READY, now, now + timedelta(hours=1)),
            (TaskResultStatus.RUNNING, now, None),
            (TaskResultStatus.SUCCESSFUL, now, None),
            (TaskResultStatus.FAILED, now, None),
        ]:
            DatabaseTask.objects.create(
                task_path="tests.test_executor.sample_task",
                status=status,
                enqueued_at=enqueued_at,
                run_after=run_after,
                backend_name="default",
            )

        response = client.get(reverse("django_database_task:task_status"))

        assert response.status_code == 200
        data = response.json()
        assert data == {
            "pending_count": 2,
            "running_count": 1,
            "successful_count": 1,
            "failed_count": 1,
            "delayed_count": 1,
            "oldest_pending_waiting_since": data["oldest_pending_waiting_since"],
            "newest_pending_waiting_since": data["newest_pending_waiting_since"],
        }
        # Serialized as ISO 8601 by DjangoJSONEncoder, to the millisecond.
        oldest = parse_datetime(data["oldest_pending_waiting_since"])
        newest = parse_datetime(data["newest_pending_waiting_since"])
        assert abs(oldest - (now - timedelta(minutes=30))) < timedelta(seconds=1)
        assert abs(newest - (now - timedelta(minutes=10))) < timedelta(seconds=1)

    def test_task_status_of_an_empty_queue(self, client):
        response = client.get(reverse("django_database_task:task_status"))

        data = response.json()
        assert data["pending_count"] == 0
        assert data["oldest_pending_waiting_since"] is None
        assert data["newest_pending_waiting_since"] is None

    def test_task_status_filters_the_stats_by_queue(self, client):
        for queue_name, status in [
            ("emails", TaskResultStatus.FAILED),
            ("default", TaskResultStatus.FAILED),
            ("default", TaskResultStatus.FAILED),
        ]:
            DatabaseTask.objects.create(
                task_path="tests.test_executor.sample_task",
                queue_name=queue_name,
                status=status,
                enqueued_at=timezone.now(),
                backend_name="default",
            )

        response = client.get(
            reverse("django_database_task:task_status"),
            {"queue_name": "emails"},
        )

        assert response.json()["failed_count"] == 1

    def test_task_status_of_a_backend_without_queue_stats(self, client):
        """A backend other than a database one still gets pending_count."""
        DatabaseTask.objects.create(
            task_path="tests.test_executor.sample_task",
            status=TaskResultStatus.READY,
            enqueued_at=timezone.now(),
            backend_name="dummy",
        )

        with override_settings(
            TASKS={
                "default": {
                    "BACKEND": "django_database_task.backends.DatabaseTaskBackend"
                },
                "dummy": {"BACKEND": "django.tasks.backends.dummy.DummyBackend"},
            }
        ):
            response = client.get(
                reverse("django_database_task:task_status"),
                {"backend_name": "dummy"},
            )

        assert response.status_code == 200
        assert response.json() == {"pending_count": 1}


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

    def test_purge_via_get(self, client):
        """Test that GET method works for GAE cron compatibility."""
        # Create a completed task
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
        response = client.get(reverse("django_database_task:purge_completed_tasks"))
        assert response.status_code == 200
        data = response.json()
        assert data["deleted"] == 1
        assert data["dry_run"] is False

    def test_purge_via_get_with_params(self, client):
        """Test GET method with query parameters."""
        # Create a completed task
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
        response = client.get(
            reverse("django_database_task:purge_completed_tasks"),
            {"days": "0", "status": "SUCCESSFUL", "dry_run": "true"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        assert data["dry_run"] is True

    def test_purge_via_get_invalid_days(self, client):
        """Test GET method with invalid days parameter."""
        response = client.get(
            reverse("django_database_task:purge_completed_tasks"),
            {"days": "abc"},
        )
        assert response.status_code == 400
        assert "days must be an integer" in response.json()["error"]

    def test_purge_via_get_invalid_batch_size(self, client):
        """Test GET method with invalid batch_size parameter."""
        response = client.get(
            reverse("django_database_task:purge_completed_tasks"),
            {"batch_size": "abc"},
        )
        assert response.status_code == 400
        assert "batch_size must be an integer" in response.json()["error"]

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

    def _create_completed_tasks(self, task_paths):
        for task_path in task_paths:
            DatabaseTask.objects.create(
                task_path=task_path,
                queue_name="default",
                priority=0,
                args_json=[],
                kwargs_json={},
                status=TaskResultStatus.SUCCESSFUL,
                enqueued_at=timezone.now(),
                finished_at=timezone.now(),
                backend_name="default",
            )

    def test_purge_filters_by_task_path(self, client):
        """Only the tasks with the given task path are deleted."""
        self._create_completed_tasks(
            ["tests.tasks.simple_task", "tests.tasks.simple_task", "tests.tasks.other"]
        )

        response = client.post(
            reverse("django_database_task:purge_completed_tasks"),
            data=json.dumps({"task_path": "tests.tasks.simple_task"}),
            content_type="application/json",
        )

        assert response.status_code == 200
        assert response.json()["deleted"] == 2
        assert list(DatabaseTask.objects.values_list("task_path", flat=True)) == [
            "tests.tasks.other"
        ]

    def test_purge_task_path_is_matched_exactly(self, client):
        """A prefix of a task path does not match it."""
        self._create_completed_tasks(["tests.tasks.simple_task"])

        response = client.post(
            reverse("django_database_task:purge_completed_tasks"),
            data=json.dumps({"task_path": "tests.tasks"}),
            content_type="application/json",
        )

        assert response.status_code == 200
        assert response.json()["deleted"] == 0
        assert DatabaseTask.objects.count() == 1

    def test_purge_null_task_path_deletes_every_task(self, client):
        """A null task_path is the same as leaving it out."""
        self._create_completed_tasks(["tests.tasks.simple_task", "tests.tasks.other"])

        response = client.post(
            reverse("django_database_task:purge_completed_tasks"),
            data=json.dumps({"task_path": None}),
            content_type="application/json",
        )

        assert response.status_code == 200
        assert response.json()["deleted"] == 2

    def test_purge_via_get_filters_by_task_path(self, client):
        """GET takes task_path as a query parameter."""
        self._create_completed_tasks(["tests.tasks.simple_task", "tests.tasks.other"])

        response = client.get(
            reverse("django_database_task:purge_completed_tasks"),
            {"task_path": "tests.tasks.simple_task", "dry_run": "true"},
        )

        assert response.status_code == 200
        assert response.json() == {"count": 1, "dry_run": True}
        assert DatabaseTask.objects.count() == 2

    def test_purge_via_get_empty_task_path_deletes_every_task(self, client):
        """An empty task_path query parameter does not filter."""
        self._create_completed_tasks(["tests.tasks.simple_task", "tests.tasks.other"])

        response = client.get(
            reverse("django_database_task:purge_completed_tasks"),
            {"task_path": ""},
        )

        assert response.status_code == 200
        assert response.json()["deleted"] == 2

    def test_purge_rejects_non_string_task_path(self, client):
        """A task_path that is not a string returns 400."""
        self._create_completed_tasks(["tests.tasks.simple_task"])

        response = client.post(
            reverse("django_database_task:purge_completed_tasks"),
            data=json.dumps({"task_path": ["tests.tasks.simple_task"]}),
            content_type="application/json",
        )

        assert response.status_code == 400
        assert "task_path must be a string" in response.json()["error"]
        assert DatabaseTask.objects.count() == 1


# All endpoints that must consult the backend's authentication handler,
# as (url name, HTTP method, reverse() args).
AUTHENTICATED_ENDPOINTS = [
    ("run_tasks", "post", []),
    ("run_one_task", "post", []),
    ("task_status", "get", []),
    ("execute_task", "post", ["3f2a9c11-0000-4000-8000-000000000000"]),
    ("purge_completed_tasks", "get", []),
    ("purge_completed_tasks", "post", []),
]


class AuthHandlerRecorder:
    """An authentication handler that records the requests it saw."""

    def __init__(self, response=None):
        self.requests = []
        self.response = response

    def __call__(self, request):
        self.requests.append(request)
        return self.response


@pytest.fixture
def rejecting_handler(monkeypatch):
    """Make the default backend reject every request with a 401."""
    recorder = AuthHandlerRecorder(JsonResponse({"error": "Unauthorized"}, status=401))
    monkeypatch.setattr(
        DatabaseTaskBackend, "get_auth_handlers", lambda self, endpoint=None: [recorder]
    )
    return recorder


@pytest.fixture
def accepting_handler(monkeypatch):
    """Make the default backend accept every request."""
    recorder = AuthHandlerRecorder(None)
    monkeypatch.setattr(
        DatabaseTaskBackend, "get_auth_handlers", lambda self, endpoint=None: [recorder]
    )
    return recorder


def _call(client, url_name, method, args):
    url = reverse(f"django_database_task:{url_name}", args=args)
    return getattr(client, method)(url, data="", content_type="application/json")


@pytest.mark.django_db
class TestBackendAuthentication:
    """Tests for the authentication handler the backend provides."""

    def test_base_backend_provides_no_handler(self):
        """The base backend opts out of authentication."""
        assert DatabaseTaskBackend(alias="default", params={}).get_auth_handlers() == []

    @pytest.mark.parametrize("url_name,method,args", AUTHENTICATED_ENDPOINTS)
    def test_rejected_request_is_blocked(
        self, client, rejecting_handler, url_name, method, args
    ):
        """Every endpoint returns the handler's response and stops there."""
        response = _call(client, url_name, method, args)

        assert response.status_code == 401
        assert response.json() == {"error": "Unauthorized"}
        assert len(rejecting_handler.requests) == 1

    @pytest.mark.parametrize("url_name,method,args", AUTHENTICATED_ENDPOINTS)
    def test_accepted_request_reaches_the_view(
        self, client, accepting_handler, url_name, method, args
    ):
        """A handler returning None lets the request through."""
        response = _call(client, url_name, method, args)

        assert len(accepting_handler.requests) == 1
        assert response.status_code != 401

    @pytest.mark.parametrize("url_name,method,args", AUTHENTICATED_ENDPOINTS)
    def test_endpoints_work_without_a_handler(self, client, url_name, method, args):
        """Backends without a handler are unaffected."""
        response = _call(client, url_name, method, args)

        assert response.status_code != 401

    def test_rejected_request_does_not_run_tasks(self, client, rejecting_handler):
        """A blocked run request leaves the queued task alone."""
        task = DatabaseTask.objects.create(
            task_path="tests.test_executor.sample_task",
            queue_name="default",
            priority=0,
            args_json=[],
            kwargs_json={},
            status=TaskResultStatus.READY,
            enqueued_at=timezone.now(),
            backend_name="default",
        )

        response = client.post(reverse("django_database_task:run_tasks"))

        assert response.status_code == 401
        task.refresh_from_db()
        assert task.status == TaskResultStatus.READY

    def test_rejected_request_does_not_purge_tasks(self, client, rejecting_handler):
        """A blocked purge request deletes nothing."""
        DatabaseTask.objects.create(
            task_path="tests.test_executor.sample_task",
            queue_name="default",
            priority=0,
            args_json=[],
            kwargs_json={},
            status=TaskResultStatus.SUCCESSFUL,
            enqueued_at=timezone.now(),
            finished_at=timezone.now(),
            backend_name="default",
        )

        response = client.post(reverse("django_database_task:purge_completed_tasks"))

        assert response.status_code == 401
        assert DatabaseTask.objects.count() == 1

    def test_unusable_backend_name_does_not_skip_authentication(
        self, client, rejecting_handler
    ):
        """An unresolvable backend is rejected instead of running unauthenticated."""
        response = client.get(
            reverse("django_database_task:task_status"),
            {"backend_name": "does-not-exist"},
        )

        assert response.status_code == 400
        assert response.json() == {"error": "Invalid backend name"}
        assert rejecting_handler.requests == []

    def test_backend_name_from_the_request_body_is_used(self, client, monkeypatch):
        """Views that read backend_name from a JSON body authenticate with it."""
        seen = []
        monkeypatch.setattr(
            DatabaseTaskBackend,
            "get_auth_handlers",
            lambda self, endpoint=None: [lambda request: seen.append(request) or None],
        )

        response = client.post(
            reverse("django_database_task:run_tasks"),
            data=json.dumps({"backend_name": "default"}),
            content_type="application/json",
        )

        assert response.status_code == 200
        assert len(seen) == 1

    def test_malformed_body_still_reports_a_bad_request(
        self, client, accepting_handler
    ):
        """A broken JSON body is reported by the view, not the auth check."""
        response = client.post(
            reverse("django_database_task:run_tasks"),
            data="{not json",
            content_type="application/json",
        )

        assert response.status_code == 400
        assert "Invalid JSON" in response.json()["error"]


@pytest.fixture
def use_backend(monkeypatch):
    """Make the views authenticate against a backend built for the test."""

    def install(backend):
        monkeypatch.setattr(
            "django_database_task.views.get_backend", lambda name="default": backend
        )
        return backend

    return install


def _backend_with_handlers(entries, options=None):
    """Build a default backend whose AUTH_HANDLERS are the given entries."""
    backend_options = {"AUTH_HANDLERS": entries}
    if options:
        backend_options["AUTH_HANDLER_OPTIONS"] = options
    return DatabaseTaskBackend(alias="default", params={"OPTIONS": backend_options})


@pytest.mark.django_db
class TestAuthHandlerComposition:
    """Tests for combining several authentication handlers."""

    def test_base_backend_provides_no_handlers(self):
        """The base backend opts out of authentication."""
        backend = DatabaseTaskBackend(alias="default", params={})

        assert backend.get_auth_handlers() == []

    def test_one_accepting_handler_lets_the_request_through(self, client, use_backend):
        """A rejection is ignored once another handler accepts."""
        rejecting = AuthHandlerRecorder(JsonResponse({"error": "no"}, status=401))
        accepting = AuthHandlerRecorder(None)
        use_backend(_backend_with_handlers([rejecting, accepting]))

        response = client.get(reverse("django_database_task:task_status"))

        assert response.status_code == 200
        assert len(rejecting.requests) == 1
        assert len(accepting.requests) == 1

    def test_handlers_after_the_accepting_one_are_not_called(self, client, use_backend):
        """Evaluation stops at the first handler that accepts."""
        accepting = AuthHandlerRecorder(None)
        unused = AuthHandlerRecorder(JsonResponse({"error": "no"}, status=401))
        use_backend(_backend_with_handlers([accepting, unused]))

        response = client.get(reverse("django_database_task:task_status"))

        assert response.status_code == 200
        assert unused.requests == []

    def test_the_first_rejection_is_returned(self, client, use_backend):
        """When every handler rejects, the first response is the answer."""
        first = AuthHandlerRecorder(JsonResponse({"error": "first"}, status=401))
        second = AuthHandlerRecorder(JsonResponse({"error": "second"}, status=403))
        use_backend(_backend_with_handlers([first, second]))

        response = client.get(reverse("django_database_task:task_status"))

        assert response.status_code == 401
        assert response.json() == {"error": "first"}
        assert len(second.requests) == 1

    def test_the_broker_handler_runs_before_the_configured_ones(
        self, client, use_backend, monkeypatch
    ):
        """A broker handler is tried first, so its rejection is reported."""
        broker = AuthHandlerRecorder(JsonResponse({"error": "broker"}, status=401))
        configured = AuthHandlerRecorder(
            JsonResponse({"error": "configured"}, status=401)
        )
        backend = _backend_with_handlers([configured])
        monkeypatch.setattr(
            type(backend),
            "get_broker_auth_handlers",
            lambda self, endpoint=None: [broker],
        )
        use_backend(backend)

        response = client.get(reverse("django_database_task:task_status"))

        assert response.json() == {"error": "broker"}

    def test_a_handler_scoped_to_other_endpoints_is_skipped(self, client, use_backend):
        """ENDPOINTS limits a handler to the endpoints it names."""
        recorder = AuthHandlerRecorder(JsonResponse({"error": "no"}, status=401))
        use_backend(
            _backend_with_handlers([{"HANDLER": recorder, "ENDPOINTS": ["purge"]}])
        )

        response = client.get(reverse("django_database_task:task_status"))

        assert response.status_code == 200
        assert recorder.requests == []

    def test_a_handler_scoped_to_this_endpoint_is_applied(self, client, use_backend):
        """The same handler still guards the endpoint it names."""
        recorder = AuthHandlerRecorder(JsonResponse({"error": "no"}, status=401))
        use_backend(
            _backend_with_handlers([{"HANDLER": recorder, "ENDPOINTS": ["purge"]}])
        )

        response = client.get(reverse("django_database_task:purge_completed_tasks"))

        assert response.status_code == 401
        assert len(recorder.requests) == 1

    def test_a_shared_secret_lets_an_external_caller_in(self, client, use_backend):
        """The documented cron setup works end to end."""
        use_backend(
            _backend_with_handlers(
                ["django_database_task.auth.SharedSecretAuth"], {"TOKEN": "s3cret"}
            )
        )
        url = reverse("django_database_task:task_status")

        assert client.get(url).status_code == 401
        assert client.get(url, HTTP_AUTHORIZATION="Bearer s3cret").status_code == 200

    def test_a_backend_without_auth_handlers_is_unauthenticated(
        self, client, use_backend
    ):
        """A third party backend that provides no handlers lets requests in."""

        class HandlerlessBackend:
            pass

        use_backend(HandlerlessBackend())

        response = client.get(reverse("django_database_task:task_status"))

        assert response.status_code == 200
