"""管理コマンドのテスト"""

from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.tasks.base import TaskResultStatus
from django.utils import timezone

from django_database_task.models import DatabaseTask

from .tasks import (
    failing_task,
    high_priority_task,
    low_priority_task,
    simple_task,
    special_queue_task,
)


@pytest.mark.django_db
class TestRunDatabaseTasks:
    def test_run_database_tasks_executes_task(self):
        """タスクが実行される"""
        simple_task.enqueue(5, 3)

        out = StringIO()
        call_command("run_database_tasks", stdout=out)

        assert DatabaseTask.objects.filter(status=TaskResultStatus.SUCCESSFUL).count() == 1

    def test_run_database_tasks_updates_status(self):
        """ステータスが更新される"""
        result = simple_task.enqueue(1, 2)

        call_command("run_database_tasks", stdout=StringIO())

        db_task = DatabaseTask.objects.get(id=result.id)
        assert db_task.status == TaskResultStatus.SUCCESSFUL
        assert db_task.return_value_json == 3

    def test_run_database_tasks_respects_priority(self):
        """優先度順で実行される"""
        # 低優先度を先にキューイング
        low_result = low_priority_task.enqueue()
        high_result = high_priority_task.enqueue()

        # 1タスクだけ実行
        call_command("run_database_tasks", max_tasks=1, stdout=StringIO())

        # 高優先度が先に実行される
        high_task = DatabaseTask.objects.get(id=high_result.id)
        low_task = DatabaseTask.objects.get(id=low_result.id)

        assert high_task.status == TaskResultStatus.SUCCESSFUL
        assert low_task.status == TaskResultStatus.READY

    def test_run_database_tasks_respects_run_after(self):
        """run_afterを尊重する"""
        # 未来の実行時刻を設定
        future = timezone.now() + timedelta(hours=1)
        future_task = simple_task.using(run_after=future)
        future_result = future_task.enqueue(1, 1)

        # 現在実行可能なタスク
        now_result = simple_task.enqueue(2, 2)

        call_command("run_database_tasks", stdout=StringIO())

        # 未来のタスクは実行されない
        future_db = DatabaseTask.objects.get(id=future_result.id)
        now_db = DatabaseTask.objects.get(id=now_result.id)

        assert future_db.status == TaskResultStatus.READY
        assert now_db.status == TaskResultStatus.SUCCESSFUL

    def test_run_database_tasks_handles_error(self):
        """エラー時にFAILED"""
        result = failing_task.enqueue()

        call_command("run_database_tasks", stdout=StringIO())

        db_task = DatabaseTask.objects.get(id=result.id)
        assert db_task.status == TaskResultStatus.FAILED
        assert len(db_task.errors_json) > 0

    def test_run_database_tasks_queue_filter(self):
        """キューフィルタが動作する"""
        default_result = simple_task.enqueue(1, 1)
        special_result = special_queue_task.enqueue()

        # specialキューのみ実行
        call_command("run_database_tasks", queue="special", stdout=StringIO())

        default_db = DatabaseTask.objects.get(id=default_result.id)
        special_db = DatabaseTask.objects.get(id=special_result.id)

        assert default_db.status == TaskResultStatus.READY
        assert special_db.status == TaskResultStatus.SUCCESSFUL

    def test_run_database_tasks_max_tasks(self):
        """max_tasksオプションが動作する"""
        simple_task.enqueue(1, 1)
        simple_task.enqueue(2, 2)
        simple_task.enqueue(3, 3)

        call_command("run_database_tasks", max_tasks=2, stdout=StringIO())

        assert DatabaseTask.objects.filter(status=TaskResultStatus.SUCCESSFUL).count() == 2
        assert DatabaseTask.objects.filter(status=TaskResultStatus.READY).count() == 1

    def test_run_database_tasks_no_tasks(self):
        """タスクがない場合"""
        out = StringIO()
        call_command("run_database_tasks", stdout=out)

        assert "No more tasks to process" in out.getvalue()


@pytest.mark.django_db
class TestPurgeCompletedDatabaseTasks:
    def test_purge_deletes_completed_tasks(self):
        """完了タスクが削除される"""
        # タスクを作成して実行
        simple_task.enqueue(1, 1)
        failing_task.enqueue()

        call_command("run_database_tasks", stdout=StringIO())

        assert DatabaseTask.objects.count() == 2

        # 削除
        call_command("purge_completed_database_tasks", stdout=StringIO())

        assert DatabaseTask.objects.count() == 0

    def test_purge_respects_status_option(self):
        """statusオプションが動作する"""
        simple_task.enqueue(1, 1)
        failing_task.enqueue()

        call_command("run_database_tasks", stdout=StringIO())

        # SUCCESSFULのみ削除
        call_command(
            "purge_completed_database_tasks", status="SUCCESSFUL", stdout=StringIO()
        )

        assert DatabaseTask.objects.count() == 1
        assert DatabaseTask.objects.first().status == TaskResultStatus.FAILED

    def test_purge_respects_days_option(self):
        """daysオプションが動作する"""
        # タスク作成・実行
        result = simple_task.enqueue(1, 1)
        call_command("run_database_tasks", stdout=StringIO())

        # finished_atを過去に設定
        db_task = DatabaseTask.objects.get(id=result.id)
        db_task.finished_at = timezone.now() - timedelta(days=10)
        db_task.save()

        # 5日より古いものを削除
        call_command("purge_completed_database_tasks", days=5, stdout=StringIO())

        assert DatabaseTask.objects.count() == 0

    def test_purge_keeps_recent_tasks(self):
        """新しいタスクは削除されない"""
        simple_task.enqueue(1, 1)
        call_command("run_database_tasks", stdout=StringIO())

        # 5日より古いものを削除（最近のタスクは残る）
        call_command("purge_completed_database_tasks", days=5, stdout=StringIO())

        assert DatabaseTask.objects.count() == 1

    def test_purge_dry_run(self):
        """dry-runモードで削除されない"""
        simple_task.enqueue(1, 1)
        call_command("run_database_tasks", stdout=StringIO())

        out = StringIO()
        call_command("purge_completed_database_tasks", dry_run=True, stdout=out)

        assert DatabaseTask.objects.count() == 1
        assert "Dry run" in out.getvalue()

    def test_purge_no_tasks(self):
        """削除対象がない場合"""
        out = StringIO()
        call_command("purge_completed_database_tasks", stdout=out)

        assert "No tasks to delete" in out.getvalue()


@pytest.mark.django_db
class TestPurgeWithPendingTasks:
    def test_purge_does_not_delete_ready_tasks(self):
        """READY状態のタスクは削除されない"""
        simple_task.enqueue(1, 1)  # 実行しない

        call_command("purge_completed_database_tasks", stdout=StringIO())

        assert DatabaseTask.objects.count() == 1
        assert DatabaseTask.objects.first().status == TaskResultStatus.READY
