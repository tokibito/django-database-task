"""Tests for the executor threads behind ``--threads``, without a database."""

import threading
import time
from unittest.mock import patch

import pytest

from django_database_task.threads import ExecutorThreads


@pytest.fixture
def executors():
    pool = ExecutorThreads(2, "host-abcd1234").start()
    yield pool
    pool.stop()


def wait_for(condition, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.01)
    pytest.fail("condition not met")


class TestExecutorThreads:
    def test_rejects_less_than_one(self):
        with pytest.raises(ValueError):
            ExecutorThreads(0, "w")

    def test_worker_ids_are_derived_from_the_process_id(self, executors):
        assert executors.worker_ids == ["host-abcd1234-t1", "host-abcd1234-t2"]

    def test_jobs_run_and_report_back(self, executors):
        ran = []
        assert executors.idle_count() == 2

        with patch("django_database_task.threads.close_old_connections") as close:
            for value in ("a", "b"):
                index = executors.acquire()
                executors.submit(index, lambda value=value: ran.append(value) or value)
            assert executors.busy == 2
            assert executors.acquire() is None

            outcomes = []
            while executors.busy:
                outcomes += executors.collect(timeout=5)

        assert sorted(outcomes) == ["a", "b"]
        assert sorted(ran) == ["a", "b"]
        assert executors.idle_count() == 2
        assert close.call_count == 2

    def test_jobs_run_at_the_same_time(self, executors):
        barrier = threading.Barrier(2, timeout=5)

        def meet():
            barrier.wait()
            return "met"

        for _ in range(2):
            executors.submit(executors.acquire(), meet)

        outcomes = []
        while executors.busy:
            outcomes += executors.collect(timeout=5)

        assert outcomes == ["met", "met"]

    def test_release_gives_the_slot_back(self, executors):
        index = executors.acquire()
        assert executors.idle_count() == 1
        executors.release(index)
        assert executors.idle_count() == 2
        assert executors.busy == 0

    def test_a_job_that_raises_does_not_lose_the_slot(self, executors, caplog):
        def boom():
            raise SystemExit(3)

        executors.submit(executors.acquire(), boom)
        outcomes = executors.collect(timeout=5)

        assert outcomes == [None]
        assert executors.idle_count() == 2
        assert "Executor thread host-abcd1234-t" in caplog.text

    def test_collect_without_timeout_does_not_wait(self, executors):
        started = time.monotonic()
        assert executors.collect() == []
        assert time.monotonic() - started < 1

    def test_stop_waits_for_the_running_job(self):
        pool = ExecutorThreads(1, "w").start()
        done = []
        pool.submit(pool.acquire(), lambda: time.sleep(0.2) or done.append(True))

        pool.stop()

        assert done == [True]
