"""
Tests for claiming a task with real row locks.

SQLite ignores ``SELECT FOR UPDATE``, so on SQLite two workers that hold the
same READY row are only kept apart by the status check in the UPDATE. This
module puts workers on separate connections against a PostgreSQL server,
where the lock itself is what keeps them apart.

Run the suite with DJANGO_DATABASE_ENGINE=postgresql to include them.
"""

import threading

import pytest
from django.db import connection, connections
from django.tasks import task_backends
from django.tasks.base import TaskResultStatus

from django_database_task import process_tasks
from django_database_task.models import DatabaseTask
from tests import tasks as test_tasks
from tests.tasks import counting_task

if connections["default"].vendor != "postgresql":
    pytest.skip(
        "needs a PostgreSQL database; run the suite with "
        "DJANGO_DATABASE_ENGINE=postgresql",
        allow_module_level=True,
    )

# Real commits are the point: a worker on another connection must be able
# to see the row, and pytest-django's default keeps it inside the test's
# own transaction.
pytestmark = pytest.mark.django_db(transaction=True)


def in_threads(count, target):
    """Run ``target(index)`` in ``count`` threads, each on its own connection."""
    outcomes = [None] * count
    errors = []

    def run(index):
        try:
            outcomes[index] = target(index)
        except Exception as e:
            errors.append(e)
        finally:
            connection.close()

    threads = [threading.Thread(target=run, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    return outcomes


class TestClaimUnderContention:
    def test_two_workers_holding_the_same_row_run_it_once(self):
        """Both workers fetched the row; the lock lets only one claim it."""
        test_tasks.counting_task_runs.clear()
        result = counting_task.enqueue()
        backend = task_backends["default"]
        # Both copies were fetched while the task was READY, the situation
        # fetch_task() leaves two workers in when their polls overlap.
        copies = [DatabaseTask.objects.get(id=result.id) for _ in range(2)]
        ready = threading.Barrier(2)

        def claim_and_run(index):
            ready.wait()
            return backend.run_task(copies[index], worker_id=f"worker-{index}")

        outcomes = in_threads(2, claim_and_run)

        ran = [outcome for outcome in outcomes if outcome is not None]
        assert len(ran) == 1
        assert ran[0].status == TaskResultStatus.SUCCESSFUL
        assert test_tasks.counting_task_runs == [1]
        db_task = DatabaseTask.objects.get(id=result.id)
        assert db_task.worker_ids_json == [ran[0].worker_ids[0]]

    def test_workers_polling_the_same_queue_run_each_task_once(self):
        """Fetching and claiming race freely; no task runs twice."""
        test_tasks.counting_task_runs.clear()
        task_count = 20
        ids = [str(counting_task.enqueue().id) for _ in range(task_count)]
        workers = 4
        ready = threading.Barrier(workers)

        def work(index):
            ready.wait()
            return process_tasks(worker_id=f"worker-{index}")

        outcomes = in_threads(workers, work)

        processed = sorted(result.id for results in outcomes for result in results)
        assert processed == sorted(ids)
        assert len(test_tasks.counting_task_runs) == task_count
        for db_task in DatabaseTask.objects.filter(id__in=ids):
            assert db_task.status == TaskResultStatus.SUCCESSFUL
            assert len(db_task.worker_ids_json) == 1
