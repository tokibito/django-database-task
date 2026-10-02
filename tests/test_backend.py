"""Tests for the database task backend."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.tasks import task_backends
from django.tasks.base import TaskResultStatus
from django.tasks.exceptions import TaskResultDoesNotExist
from django.utils import timezone

from django_database_task.backends import DatabaseTaskBackend
from django_database_task.models import DatabaseTask

from . import tasks as test_tasks
from .tasks import (
    async_failing_task,
    async_task,
    context_task,
    counting_task,
    failing_task,
    high_priority_task,
    simple_task,
    special_queue_task,
)


@pytest.mark.django_db
class TestDatabaseTaskBackend:
    def test_enqueue_creates_database_task(self):
        """enqueue creates a database task."""
        result = simple_task.enqueue(1, 2)

        assert DatabaseTask.objects.filter(id=result.id).exists()
        db_task = DatabaseTask.objects.get(id=result.id)
        assert db_task.task_path == "tests.tasks.simple_task"
        assert db_task.args_json == [1, 2]
        assert db_task.status == TaskResultStatus.READY

    def test_enqueue_returns_task_result(self):
        """enqueue returns a TaskResult."""
        result = simple_task.enqueue(3, 4)

        assert result.id is not None
        assert result.status == TaskResultStatus.READY
        assert result.args == [3, 4]
        assert result.kwargs == {}
        assert result.enqueued_at is not None

    def test_enqueue_with_kwargs(self):
        """enqueue with keyword arguments."""
        result = simple_task.enqueue(x=5, y=6)

        db_task = DatabaseTask.objects.get(id=result.id)
        assert db_task.args_json == []
        assert db_task.kwargs_json == {"x": 5, "y": 6}

    def test_enqueue_with_priority(self):
        """enqueue with priority."""
        result = high_priority_task.enqueue()

        db_task = DatabaseTask.objects.get(id=result.id)
        assert db_task.priority == 10

    def test_enqueue_with_queue_name(self):
        """enqueue with queue name."""
        result = special_queue_task.enqueue()

        db_task = DatabaseTask.objects.get(id=result.id)
        assert db_task.queue_name == "special"

    def test_enqueue_with_run_after(self):
        """enqueue with delayed execution."""
        run_after = timezone.now() + timedelta(hours=1)
        task = simple_task.using(run_after=run_after)
        result = task.enqueue(1, 2)

        db_task = DatabaseTask.objects.get(id=result.id)
        assert db_task.run_after is not None
        assert db_task.run_after >= run_after - timedelta(seconds=1)

    def test_get_result_returns_task(self):
        """get_result returns the task result."""
        result = simple_task.enqueue(7, 8)

        backend = task_backends["default"]
        fetched = backend.get_result(result.id)

        assert fetched.id == result.id
        assert fetched.status == TaskResultStatus.READY
        assert fetched.args == [7, 8]

    def test_get_result_not_found(self):
        """Raises TaskResultDoesNotExist for non-existent ID."""
        backend = task_backends["default"]

        with pytest.raises(TaskResultDoesNotExist):
            backend.get_result("00000000-0000-0000-0000-000000000000")


@pytest.mark.django_db
class TestTaskExecution:
    def test_run_task_success(self):
        """Task executes successfully."""
        result = simple_task.enqueue(10, 20)
        db_task = DatabaseTask.objects.get(id=result.id)

        backend = task_backends["default"]
        final_result = backend.run_task(db_task, worker_id="test-worker")

        assert final_result.status == TaskResultStatus.SUCCESSFUL
        assert final_result.return_value == 30

        db_task.refresh_from_db()
        assert db_task.status == TaskResultStatus.SUCCESSFUL
        assert db_task.return_value_json == 30
        assert db_task.finished_at is not None

    def test_run_task_failure(self):
        """Task fails with error."""
        result = failing_task.enqueue()
        db_task = DatabaseTask.objects.get(id=result.id)

        backend = task_backends["default"]
        final_result = backend.run_task(db_task, worker_id="test-worker")

        assert final_result.status == TaskResultStatus.FAILED
        assert len(final_result.errors) == 1
        assert "ValueError" in final_result.errors[0].exception_class_path

        db_task.refresh_from_db()
        assert db_task.status == TaskResultStatus.FAILED
        assert len(db_task.errors_json) == 1

    def test_run_task_with_context(self):
        """Task with context executes correctly."""
        result = context_task.enqueue()
        db_task = DatabaseTask.objects.get(id=result.id)

        backend = task_backends["default"]
        final_result = backend.run_task(db_task, worker_id="test-worker")

        assert final_result.status == TaskResultStatus.SUCCESSFUL
        assert result.id in final_result.return_value

    def test_run_task_updates_worker_id(self):
        """worker_id is updated."""
        result = simple_task.enqueue(1, 1)
        db_task = DatabaseTask.objects.get(id=result.id)

        backend = task_backends["default"]
        backend.run_task(db_task, worker_id="my-worker-123")

        db_task.refresh_from_db()
        assert "my-worker-123" in db_task.worker_ids_json

    def test_run_task_updates_timestamps(self):
        """Timestamps are updated."""
        result = simple_task.enqueue(1, 1)
        db_task = DatabaseTask.objects.get(id=result.id)

        backend = task_backends["default"]
        backend.run_task(db_task, worker_id="test-worker")

        db_task.refresh_from_db()
        assert db_task.started_at is not None
        assert db_task.finished_at is not None
        assert db_task.last_attempted_at is not None
        assert db_task.started_at <= db_task.finished_at


@pytest.mark.django_db
class TestTaskClaim:
    """run_task() runs a task only if it is the one to move it out of READY."""

    def test_a_task_handed_to_two_workers_runs_once(self):
        """The second worker to claim the same READY row runs nothing."""
        test_tasks.counting_task_runs.clear()
        result = counting_task.enqueue()
        first = DatabaseTask.objects.get(id=result.id)
        second = DatabaseTask.objects.get(id=result.id)

        backend = task_backends["default"]
        first_result = backend.run_task(first, worker_id="worker-1")
        second_result = backend.run_task(second, worker_id="worker-2")

        assert first_result.status == TaskResultStatus.SUCCESSFUL
        assert second_result is None
        assert test_tasks.counting_task_runs == [1]

        db_task = DatabaseTask.objects.get(id=result.id)
        assert db_task.status == TaskResultStatus.SUCCESSFUL
        assert db_task.worker_ids_json == ["worker-1"]

    def test_a_task_that_is_no_longer_ready_is_not_run(self):
        """A row that left READY after it was fetched is left alone."""
        test_tasks.counting_task_runs.clear()
        result = counting_task.enqueue()
        db_task = DatabaseTask.objects.get(id=result.id)
        DatabaseTask.objects.filter(id=result.id).update(
            status=TaskResultStatus.RUNNING, worker_ids_json=["worker-1"]
        )

        backend = task_backends["default"]
        assert backend.run_task(db_task, worker_id="worker-2") is None

        assert test_tasks.counting_task_runs == []
        db_task.refresh_from_db()
        assert db_task.status == TaskResultStatus.RUNNING
        assert db_task.worker_ids_json == ["worker-1"]

    def test_a_deleted_task_is_not_run(self):
        """A row purged after it was fetched is not resurrected."""
        test_tasks.counting_task_runs.clear()
        result = counting_task.enqueue()
        db_task = DatabaseTask.objects.get(id=result.id)
        DatabaseTask.objects.filter(id=result.id).delete()

        backend = task_backends["default"]
        assert backend.run_task(db_task, worker_id="worker-1") is None

        assert test_tasks.counting_task_runs == []
        assert not DatabaseTask.objects.filter(id=result.id).exists()

    def test_the_worker_id_is_appended_to_the_stored_list(self):
        """An attempt recorded after the row was loaded is kept."""
        result = simple_task.enqueue(1, 1)
        db_task = DatabaseTask.objects.get(id=result.id)
        # A worker that died, its attempt recorded by requeue_stale_tasks
        # while this worker was holding an older copy of the row.
        DatabaseTask.objects.filter(id=result.id).update(worker_ids_json=["worker-1"])

        backend = task_backends["default"]
        final_result = backend.run_task(db_task, worker_id="worker-2")

        assert final_result.worker_ids == ["worker-1", "worker-2"]
        db_task.refresh_from_db()
        assert db_task.worker_ids_json == ["worker-1", "worker-2"]

    def test_started_at_of_an_earlier_attempt_is_kept(self):
        """started_at marks the first attempt, not the latest one."""
        result = simple_task.enqueue(1, 1)
        db_task = DatabaseTask.objects.get(id=result.id)
        earlier = timezone.now() - timedelta(hours=1)
        DatabaseTask.objects.filter(id=result.id).update(started_at=earlier)

        backend = task_backends["default"]
        backend.run_task(db_task, worker_id="worker-2")

        db_task.refresh_from_db()
        assert db_task.started_at == earlier
        assert db_task.last_attempted_at > earlier


@pytest.mark.django_db
class TestTaskStartFailure:
    """Tests for a task whose function cannot be resolved after the claim."""

    @pytest.mark.parametrize(
        "task_path, error_class, named_in_traceback",
        [
            ("tests.tasks.removed_task", "builtins.AttributeError", "removed_task"),
            (
                "tests.renamed_module.simple_task",
                "builtins.ModuleNotFoundError",
                "tests.renamed_module",
            ),
        ],
    )
    def test_a_task_that_cannot_be_imported_is_failed(
        self, task_path, error_class, named_in_traceback
    ):
        """Recorded like a task that raised, not left RUNNING."""
        result = simple_task.enqueue(1, 2)
        DatabaseTask.objects.filter(id=result.id).update(task_path=task_path)
        db_task = DatabaseTask.objects.get(id=result.id)

        final_result = task_backends["default"].run_task(db_task, worker_id="w")

        assert final_result.status == TaskResultStatus.FAILED
        assert final_result.task is None
        (error,) = final_result.errors
        assert error.exception_class_path == error_class
        assert named_in_traceback in error.traceback

        db_task.refresh_from_db()
        assert db_task.status == TaskResultStatus.FAILED
        assert db_task.finished_at is not None
        assert db_task.worker_ids_json == ["w"]
        assert [e["exception_class_path"] for e in db_task.errors_json] == [error_class]

    def test_no_lifecycle_signal_is_sent_for_it(self):
        """There is no task object to put in the signals' result."""
        from django.tasks.signals import task_finished, task_started

        result = simple_task.enqueue(1, 2)
        DatabaseTask.objects.filter(id=result.id).update(
            task_path="tests.tasks.removed_task"
        )
        db_task = DatabaseTask.objects.get(id=result.id)
        sent = []

        def receiver(sender, task_result, **kwargs):
            sent.append(task_result)

        task_started.connect(receiver)
        task_finished.connect(receiver)
        try:
            task_backends["default"].run_task(db_task, worker_id="w")
        finally:
            task_started.disconnect(receiver)
            task_finished.disconnect(receiver)

        assert sent == []

    def test_a_failing_task_started_receiver_fails_the_task(self):
        """An error between the claim and the run must not leave RUNNING."""
        from django.tasks.signals import task_started

        result = simple_task.enqueue(1, 2)
        db_task = DatabaseTask.objects.get(id=result.id)

        def receiver(sender, task_result, **kwargs):
            raise RuntimeError("receiver broke")

        task_started.connect(receiver)
        try:
            final_result = task_backends["default"].run_task(db_task, worker_id="w")
        finally:
            task_started.disconnect(receiver)

        assert final_result.status == TaskResultStatus.FAILED
        assert final_result.task is simple_task
        db_task.refresh_from_db()
        assert db_task.status == TaskResultStatus.FAILED
        assert db_task.errors_json[0]["exception_class_path"] == "builtins.RuntimeError"

    def test_the_error_history_is_kept(self):
        """A retry that fails the same way adds to the record."""
        result = simple_task.enqueue(1, 2)
        DatabaseTask.objects.filter(id=result.id).update(
            task_path="tests.tasks.removed_task",
            errors_json=[
                {"exception_class_path": "builtins.ValueError", "traceback": ""}
            ],
        )
        db_task = DatabaseTask.objects.get(id=result.id)

        task_backends["default"].run_task(db_task, worker_id="w")

        db_task.refresh_from_db()
        assert [e["exception_class_path"] for e in db_task.errors_json] == [
            "builtins.ValueError",
            "builtins.AttributeError",
        ]


@pytest.mark.django_db
class TestJsonSerialization:
    """Tests for JSON serialization validation."""

    def test_enqueue_with_valid_json_types(self):
        """Valid JSON types are accepted."""
        result = simple_task.enqueue(
            1,  # int
            2.5,  # float
        )
        db_task = DatabaseTask.objects.get(id=result.id)
        assert db_task.args_json == [1, 2.5]

    def test_enqueue_with_nested_structures(self):
        """Nested dicts and lists are accepted."""
        from tests.tasks import dict_task

        result = dict_task.enqueue(data={"key": "value", "nested": {"list": [1, 2, 3]}})
        db_task = DatabaseTask.objects.get(id=result.id)
        assert db_task.kwargs_json == {
            "data": {"key": "value", "nested": {"list": [1, 2, 3]}}
        }

    def test_enqueue_with_datetime_raises_error(self):
        """datetime objects raise TypeError."""
        from datetime import datetime

        with pytest.raises(TypeError, match="Unsupported type"):
            simple_task.enqueue(datetime.now(), 1)

    def test_enqueue_with_uuid_raises_error(self):
        """UUID objects raise TypeError."""
        import uuid

        with pytest.raises(TypeError, match="Unsupported type"):
            simple_task.enqueue(uuid.uuid4(), 1)

    def test_enqueue_with_custom_object_raises_error(self):
        """Custom objects raise TypeError."""

        class CustomObject:
            pass

        with pytest.raises(TypeError, match="Unsupported type"):
            simple_task.enqueue(CustomObject(), 1)


@pytest.mark.django_db
class TestAsyncTaskExecution:
    """Tests for async task execution."""

    def test_async_task_success(self):
        """Async task executes successfully."""
        result = async_task.enqueue(10, 20)
        db_task = DatabaseTask.objects.get(id=result.id)

        backend = task_backends["default"]
        final_result = backend.run_task(db_task, worker_id="test-worker")

        assert final_result.status == TaskResultStatus.SUCCESSFUL
        assert final_result.return_value == 30

        db_task.refresh_from_db()
        assert db_task.status == TaskResultStatus.SUCCESSFUL
        assert db_task.return_value_json == 30

    def test_async_task_failure(self):
        """Async task fails with error."""
        result = async_failing_task.enqueue()
        db_task = DatabaseTask.objects.get(id=result.id)

        backend = task_backends["default"]
        final_result = backend.run_task(db_task, worker_id="test-worker")

        assert final_result.status == TaskResultStatus.FAILED
        assert len(final_result.errors) == 1
        assert "ValueError" in final_result.errors[0].exception_class_path

        db_task.refresh_from_db()
        assert db_task.status == TaskResultStatus.FAILED


@pytest.mark.django_db
class TestClaimAndRunSeparately:
    """claim_task() and run_claimed_task() are the two steps of run_task()."""

    def test_claim_then_run(self):
        test_tasks.counting_task_runs.clear()
        result = counting_task.enqueue()
        db_task = DatabaseTask.objects.get(id=result.id)
        backend = task_backends["default"]

        assert backend.claim_task(db_task, worker_id="worker-1") is True
        claimed = DatabaseTask.objects.get(id=result.id)
        assert claimed.status == TaskResultStatus.RUNNING
        assert claimed.worker_ids_json == ["worker-1"]
        assert test_tasks.counting_task_runs == []

        final_result = backend.run_claimed_task(db_task, worker_id="worker-1")

        assert final_result.status == TaskResultStatus.SUCCESSFUL
        assert test_tasks.counting_task_runs == [1]
        assert DatabaseTask.objects.get(id=result.id).status == (
            TaskResultStatus.SUCCESSFUL
        )

    def test_a_lost_claim_returns_false_and_writes_nothing(self):
        result = counting_task.enqueue()
        first = DatabaseTask.objects.get(id=result.id)
        second = DatabaseTask.objects.get(id=result.id)
        backend = task_backends["default"]

        assert backend.claim_task(first, worker_id="worker-1") is True
        assert backend.claim_task(second, worker_id="worker-2") is False

        assert DatabaseTask.objects.get(id=result.id).worker_ids_json == ["worker-1"]

    def test_run_task_is_the_two_steps(self):
        result = simple_task.enqueue(1, 2)
        db_task = DatabaseTask.objects.get(id=result.id)
        backend = task_backends["default"]

        with (
            patch.object(DatabaseTaskBackend, "claim_task", return_value=True) as claim,
            patch.object(DatabaseTaskBackend, "run_claimed_task") as run,
        ):
            backend.run_task(db_task, worker_id="w")

        claim.assert_called_once_with(db_task, "w")
        run.assert_called_once_with(db_task, "w")
