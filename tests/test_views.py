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
