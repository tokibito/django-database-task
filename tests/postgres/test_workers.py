"""
The ``--workers`` option over real workers, against PostgreSQL.

test_supervisor.py drives the supervisor over stub processes and
test_commands.py checks what the command hands it. Here the workers are the
command itself, started as ``python -m django run_database_tasks --workers
2`` against the test database, where the row locks let both of them claim
and run a task at the same time. The SQLite file the other child-process
test shares with its worker cannot take two: the command refuses it.

Run the suite with DJANGO_DATABASE_ENGINE=postgresql to include them.
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from django.db import connections
from django.tasks.base import TaskResultStatus

from django_database_task.models import DatabaseTask
from tests.tasks import slow_task

if connections["default"].vendor != "postgresql":
    pytest.skip(
        "needs a PostgreSQL database; run the suite with "
        "DJANGO_DATABASE_ENGINE=postgresql",
        allow_module_level=True,
    )

# The workers connect from processes of their own, so the rows the test
# enqueues have to be committed for them to see.
pytestmark = pytest.mark.django_db(transaction=True)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def child_env():
    """Point a worker process at the test database."""
    settings = connections["default"].settings_dict
    return {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "tests.settings",
        "DJANGO_DATABASE_ENGINE": "postgresql",
        "POSTGRES_DB": settings["NAME"],
        "POSTGRES_USER": settings["USER"],
        "POSTGRES_PASSWORD": settings["PASSWORD"],
        "POSTGRES_HOST": settings["HOST"],
        "POSTGRES_PORT": str(settings["PORT"]),
        "PYTHONUNBUFFERED": "1",
    }


def start_command(*args):
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(
        [sys.executable, "-m", "django", "run_database_tasks", *args],
        cwd=PROJECT_ROOT,
        env=child_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        **kwargs,
    )


def finish(process, timeout=60):
    try:
        output, _ = process.communicate(timeout=timeout)
    except BaseException:
        process.kill()
        process.communicate()
        raise
    return output


def wait_until(process, condition, timeout=60, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        if process.poll() is not None:
            output, _ = process.communicate()
            pytest.fail(
                f"The command exited with {process.returncode} early:\n{output}"
            )
        time.sleep(0.05)
    pytest.fail(f"{what} not met within {timeout} seconds")


def statuses(task_ids):
    return sorted(
        DatabaseTask.objects.filter(id__in=task_ids).values_list("status", flat=True)
    )


class TestWorkers:
    def test_two_workers_run_two_tasks_at_once_and_stop_on_the_signal(self):
        task_ids = [slow_task.enqueue(seconds=3).id for _ in range(2)]
        if sys.platform == "win32":
            sig, expected_name = signal.CTRL_BREAK_EVENT, "SIGBREAK"
        else:
            sig, expected_name = signal.SIGTERM, "SIGTERM"

        process = start_command("--continuous", "--interval", "0.2", "--workers", "2")
        try:
            wait_until(
                process,
                lambda: statuses(task_ids) == [TaskResultStatus.RUNNING] * 2,
                what="both tasks running at once",
            )
            process.send_signal(sig)
        except BaseException:
            process.kill()
            process.communicate()
            raise
        output = finish(process)

        assert process.returncode == 0, output
        assert statuses(task_ids) == [TaskResultStatus.SUCCESSFUL] * 2
        assert f"Received {expected_name}: stopping 2 worker(s)" in output
        assert output.count("Total tasks processed: 1") == 2
        assert "Shutdown complete: every worker exited." in output

    def test_two_workers_drain_the_queue_and_exit(self):
        task_ids = [slow_task.enqueue(seconds=1).id for _ in range(4)]

        process = start_command("--workers", "2", "--empty-exit-code", "4")
        output = finish(process)

        assert process.returncode == 0, output
        assert statuses(task_ids) == [TaskResultStatus.SUCCESSFUL] * 4
        assert output.count("Worker ID:") == 2
        assert output.count("No more tasks to process.") == 2
        assert "Every worker has exited." in output

    def test_an_idle_run_reports_the_empty_exit_code(self):
        process = start_command("--workers", "2", "--empty-exit-code", "4")
        output = finish(process)

        assert process.returncode == 4, output
        assert output.count("Total tasks processed: 0") == 2
