"""Tests for the DatabaseTask model."""

import uuid

import pytest
from django.tasks.base import TaskResultStatus
from django.utils import timezone

from django_database_task.models import DatabaseTask


@pytest.mark.django_db
class TestDatabaseTaskModel:
    def test_create_task(self):
        """Task is created correctly."""
        task = DatabaseTask.objects.create(
            task_path="tests.tasks.simple_task",
            queue_name="default",
            priority=0,
            args_json=[1, 2],
            kwargs_json={"key": "value"},
            status=TaskResultStatus.READY,
            enqueued_at=timezone.now(),
            backend_name="default",
        )

        assert task.id is not None
        assert isinstance(task.id, uuid.UUID)
        assert task.task_path == "tests.tasks.simple_task"
        assert task.args_json == [1, 2]
        assert task.kwargs_json == {"key": "value"}

    def test_default_values(self):
        """Default values are set correctly."""
        task = DatabaseTask.objects.create(
            task_path="tests.tasks.simple_task",
            enqueued_at=timezone.now(),
            backend_name="default",
        )

        assert task.queue_name == "default"
        assert task.priority == 0
        assert task.args_json == []
        assert task.kwargs_json == {}
        assert task.status == TaskResultStatus.READY
        assert task.errors_json == []
        assert task.worker_ids_json == []

    def test_str_representation(self):
        """String representation is correct."""
        task = DatabaseTask.objects.create(
            task_path="tests.tasks.simple_task",
            enqueued_at=timezone.now(),
            backend_name="default",
        )

        str_repr = str(task)
        assert "tests.tasks.simple_task" in str_repr
        assert "READY" in str_repr

    def test_ordering(self):
        """Ordering is by priority (desc), enqueued_at (asc)."""
        now = timezone.now()

        task1 = DatabaseTask.objects.create(
            task_path="test1",
            priority=0,
            enqueued_at=now,
            backend_name="default",
        )
        task2 = DatabaseTask.objects.create(
            task_path="test2",
            priority=10,
            enqueued_at=now,
            backend_name="default",
        )
        task3 = DatabaseTask.objects.create(
            task_path="test3",
            priority=0,
            enqueued_at=now - timezone.timedelta(seconds=1),
            backend_name="default",
        )

        tasks = list(DatabaseTask.objects.all())
        # Higher priority comes first
        assert tasks[0] == task2
        # Same priority: earlier enqueued_at comes first
        assert tasks[1] == task3
        assert tasks[2] == task1

    def test_timestamps(self):
        """created_at and updated_at are set automatically."""
        task = DatabaseTask.objects.create(
            task_path="tests.tasks.simple_task",
            enqueued_at=timezone.now(),
            backend_name="default",
        )

        assert task.created_at is not None
        assert task.updated_at is not None

        old_updated = task.updated_at
        task.status = TaskResultStatus.RUNNING
        task.save()

        task.refresh_from_db()
        assert task.updated_at > old_updated
