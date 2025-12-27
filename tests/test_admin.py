"""Tests for Django admin actions."""

import pytest
from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import User
from django.tasks.base import TaskResultStatus
from django.test import RequestFactory

from django_database_task.admin import DatabaseTaskAdmin
from django_database_task.models import DatabaseTask

from .tasks import failing_task, simple_task


@pytest.fixture
def admin_site():
    return AdminSite()


@pytest.fixture
def model_admin(admin_site):
    return DatabaseTaskAdmin(DatabaseTask, admin_site)


@pytest.fixture
def request_factory():
    return RequestFactory()


@pytest.fixture
def admin_user(db):
    return User.objects.create_superuser(
        username="admin", email="admin@example.com", password="password"
    )


@pytest.mark.django_db
class TestRunSelectedTasksAction:
    def test_run_ready_tasks(self, model_admin, request_factory, admin_user):
        """READY tasks are executed."""
        result1 = simple_task.enqueue(1, 2)
        result2 = simple_task.enqueue(3, 4)

        request = request_factory.post("/admin/")
        request.user = admin_user
        request._messages = MockMessages()

        queryset = DatabaseTask.objects.filter(id__in=[result1.id, result2.id])
        model_admin.run_selected_tasks(request, queryset)

        db_task1 = DatabaseTask.objects.get(id=result1.id)
        db_task2 = DatabaseTask.objects.get(id=result2.id)

        assert db_task1.status == TaskResultStatus.SUCCESSFUL
        assert db_task1.return_value_json == 3
        assert db_task2.status == TaskResultStatus.SUCCESSFUL
        assert db_task2.return_value_json == 7

    def test_skip_non_ready_tasks(self, model_admin, request_factory, admin_user):
        """Non-READY tasks are skipped."""
        result1 = simple_task.enqueue(1, 2)
        result2 = simple_task.enqueue(3, 4)

        # Mark one task as already completed
        db_task2 = DatabaseTask.objects.get(id=result2.id)
        db_task2.status = TaskResultStatus.SUCCESSFUL
        db_task2.save()

        request = request_factory.post("/admin/")
        request.user = admin_user
        request._messages = MockMessages()

        queryset = DatabaseTask.objects.filter(id__in=[result1.id, result2.id])
        model_admin.run_selected_tasks(request, queryset)

        db_task1 = DatabaseTask.objects.get(id=result1.id)
        assert db_task1.status == TaskResultStatus.SUCCESSFUL

        # Check message contains "skipped"
        assert any("skipped" in str(m) for m in request._messages.messages)

    def test_no_ready_tasks_warning(self, model_admin, request_factory, admin_user):
        """Warning message when no READY tasks."""
        result = simple_task.enqueue(1, 2)
        db_task = DatabaseTask.objects.get(id=result.id)
        db_task.status = TaskResultStatus.SUCCESSFUL
        db_task.save()

        request = request_factory.post("/admin/")
        request.user = admin_user
        request._messages = MockMessages()

        queryset = DatabaseTask.objects.filter(id=result.id)
        model_admin.run_selected_tasks(request, queryset)

        assert any(
            "No tasks in READY status" in str(m) for m in request._messages.messages
        )

    def test_handles_failing_tasks(self, model_admin, request_factory, admin_user):
        """Failing tasks are handled properly."""
        result = failing_task.enqueue()

        request = request_factory.post("/admin/")
        request.user = admin_user
        request._messages = MockMessages()

        queryset = DatabaseTask.objects.filter(id=result.id)
        model_admin.run_selected_tasks(request, queryset)

        db_task = DatabaseTask.objects.get(id=result.id)
        assert db_task.status == TaskResultStatus.FAILED

        # Check message contains "failed"
        assert any("failed" in str(m) for m in request._messages.messages)


@pytest.mark.django_db
class TestRetryFailedTasksAction:
    def test_retry_failed_tasks(self, model_admin, request_factory, admin_user):
        """FAILED tasks are retried."""
        # Create a task that will fail
        result = failing_task.enqueue()

        # Run it to make it fail
        db_task = DatabaseTask.objects.get(id=result.id)
        from django.tasks import task_backends

        backend = task_backends["default"]
        backend.run_task(db_task, worker_id="test")

        db_task.refresh_from_db()
        assert db_task.status == TaskResultStatus.FAILED

        # Now replace the task with a simple one that will succeed
        db_task.task_path = "tests.tasks.simple_task"
        db_task.args_json = [10, 20]
        db_task.kwargs_json = {}
        db_task.status = TaskResultStatus.FAILED
        db_task.save()

        request = request_factory.post("/admin/")
        request.user = admin_user
        request._messages = MockMessages()

        queryset = DatabaseTask.objects.filter(id=result.id)
        model_admin.retry_failed_tasks(request, queryset)

        db_task.refresh_from_db()
        assert db_task.status == TaskResultStatus.SUCCESSFUL
        assert db_task.return_value_json == 30
        assert "admin-retry" in db_task.worker_ids_json

    def test_skip_non_failed_tasks(self, model_admin, request_factory, admin_user):
        """Non-FAILED tasks are skipped."""
        result1 = failing_task.enqueue()
        result2 = simple_task.enqueue(1, 2)

        # Make one task fail
        db_task1 = DatabaseTask.objects.get(id=result1.id)
        db_task1.status = TaskResultStatus.FAILED
        db_task1.errors_json = [{"exception_class_path": "ValueError"}]
        # Replace with simple task so retry succeeds
        db_task1.task_path = "tests.tasks.simple_task"
        db_task1.args_json = [5, 5]
        db_task1.save()

        request = request_factory.post("/admin/")
        request.user = admin_user
        request._messages = MockMessages()

        queryset = DatabaseTask.objects.filter(id__in=[result1.id, result2.id])
        model_admin.retry_failed_tasks(request, queryset)

        db_task1.refresh_from_db()
        db_task2 = DatabaseTask.objects.get(id=result2.id)

        assert db_task1.status == TaskResultStatus.SUCCESSFUL
        assert db_task2.status == TaskResultStatus.READY  # Unchanged

        # Check message contains "skipped"
        assert any("skipped" in str(m) for m in request._messages.messages)

    def test_no_failed_tasks_warning(self, model_admin, request_factory, admin_user):
        """Warning message when no FAILED tasks."""
        result = simple_task.enqueue(1, 2)

        request = request_factory.post("/admin/")
        request.user = admin_user
        request._messages = MockMessages()

        queryset = DatabaseTask.objects.filter(id=result.id)
        model_admin.retry_failed_tasks(request, queryset)

        assert any(
            "No tasks in FAILED status" in str(m) for m in request._messages.messages
        )

    def test_retry_clears_previous_errors(
        self, model_admin, request_factory, admin_user
    ):
        """Previous errors are cleared on retry."""
        result = simple_task.enqueue(1, 2)

        db_task = DatabaseTask.objects.get(id=result.id)
        db_task.status = TaskResultStatus.FAILED
        db_task.errors_json = [{"exception_class_path": "OldError"}]
        db_task.save()

        request = request_factory.post("/admin/")
        request.user = admin_user
        request._messages = MockMessages()

        queryset = DatabaseTask.objects.filter(id=result.id)
        model_admin.retry_failed_tasks(request, queryset)

        db_task.refresh_from_db()
        assert db_task.status == TaskResultStatus.SUCCESSFUL
        # errors_json should be empty since task succeeded
        assert db_task.errors_json == []


class MockMessages:
    """Mock messages framework for testing."""

    def __init__(self):
        self.messages = []

    def add(self, level, message, extra_tags=""):
        self.messages.append((level, message))

    def __iter__(self):
        return iter(self.messages)
