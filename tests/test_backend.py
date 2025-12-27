"""バックエンドのテスト"""

from datetime import timedelta

import pytest
from django.tasks import task_backends
from django.tasks.base import TaskResultStatus
from django.tasks.exceptions import TaskResultDoesNotExist
from django.utils import timezone

from django_database_task.models import DatabaseTask

from .tasks import (
    context_task,
    failing_task,
    high_priority_task,
    simple_task,
    special_queue_task,
)


@pytest.mark.django_db
class TestDatabaseTaskBackend:
    def test_enqueue_creates_database_task(self):
        """enqueueがデータベースにタスクを作成する"""
        result = simple_task.enqueue(1, 2)

        assert DatabaseTask.objects.filter(id=result.id).exists()
        db_task = DatabaseTask.objects.get(id=result.id)
        assert db_task.task_path == "tests.tasks.simple_task"
        assert db_task.args_json == [1, 2]
        assert db_task.status == TaskResultStatus.READY

    def test_enqueue_returns_task_result(self):
        """enqueueがTaskResultを返す"""
        result = simple_task.enqueue(3, 4)

        assert result.id is not None
        assert result.status == TaskResultStatus.READY
        assert result.args == [3, 4]
        assert result.kwargs == {}
        assert result.enqueued_at is not None

    def test_enqueue_with_kwargs(self):
        """キーワード引数付きのenqueue"""
        result = simple_task.enqueue(x=5, y=6)

        db_task = DatabaseTask.objects.get(id=result.id)
        assert db_task.args_json == []
        assert db_task.kwargs_json == {"x": 5, "y": 6}

    def test_enqueue_with_priority(self):
        """優先度付きのenqueue"""
        result = high_priority_task.enqueue()

        db_task = DatabaseTask.objects.get(id=result.id)
        assert db_task.priority == 10

    def test_enqueue_with_queue_name(self):
        """キュー名付きのenqueue"""
        result = special_queue_task.enqueue()

        db_task = DatabaseTask.objects.get(id=result.id)
        assert db_task.queue_name == "special"

    def test_enqueue_with_run_after(self):
        """遅延実行のenqueue"""
        run_after = timezone.now() + timedelta(hours=1)
        task = simple_task.using(run_after=run_after)
        result = task.enqueue(1, 2)

        db_task = DatabaseTask.objects.get(id=result.id)
        assert db_task.run_after is not None
        assert db_task.run_after >= run_after - timedelta(seconds=1)

    def test_get_result_returns_task(self):
        """get_resultがタスク結果を返す"""
        result = simple_task.enqueue(7, 8)

        backend = task_backends["default"]
        fetched = backend.get_result(result.id)

        assert fetched.id == result.id
        assert fetched.status == TaskResultStatus.READY
        assert fetched.args == [7, 8]

    def test_get_result_not_found(self):
        """存在しないIDでTaskResultDoesNotExist"""
        backend = task_backends["default"]

        with pytest.raises(TaskResultDoesNotExist):
            backend.get_result("00000000-0000-0000-0000-000000000000")


@pytest.mark.django_db
class TestTaskExecution:
    def test_run_task_success(self):
        """タスクの正常実行"""
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
        """タスクの失敗"""
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
        """コンテキスト付きタスクの実行"""
        result = context_task.enqueue()
        db_task = DatabaseTask.objects.get(id=result.id)

        backend = task_backends["default"]
        final_result = backend.run_task(db_task, worker_id="test-worker")

        assert final_result.status == TaskResultStatus.SUCCESSFUL
        assert result.id in final_result.return_value

    def test_run_task_updates_worker_id(self):
        """worker_idが更新される"""
        result = simple_task.enqueue(1, 1)
        db_task = DatabaseTask.objects.get(id=result.id)

        backend = task_backends["default"]
        backend.run_task(db_task, worker_id="my-worker-123")

        db_task.refresh_from_db()
        assert "my-worker-123" in db_task.worker_ids_json

    def test_run_task_updates_timestamps(self):
        """タイムスタンプが更新される"""
        result = simple_task.enqueue(1, 1)
        db_task = DatabaseTask.objects.get(id=result.id)

        backend = task_backends["default"]
        backend.run_task(db_task, worker_id="test-worker")

        db_task.refresh_from_db()
        assert db_task.started_at is not None
        assert db_task.finished_at is not None
        assert db_task.last_attempted_at is not None
        assert db_task.started_at <= db_task.finished_at
