"""
Tests for the worker supervisor behind ``--workers``.

The workers here are small Python scripts rather than the real command, so
that what is checked is the supervisor alone: that it starts the number of
processes asked for, replaces the ones that exit, backs off when they keep
failing, forwards a shutdown signal and waits, kills what ignores it, and
turns the exit codes into its own. The real command behind the supervisor
is covered in test_commands.py and, against PostgreSQL, in
tests/postgres/test_workers.py.

Most tests drive the supervisor from a thread with ``request_shutdown()``,
which is what the signal handler calls; the handler itself is only
installed in the main thread, and the last tests send real signals there.
"""

import logging
import os
import signal
import sys
import threading
import time
from io import StringIO
from unittest.mock import patch

import pytest

from django_database_task.shutdown import FORCED_EXIT_CODE
from django_database_task.supervisor import (
    WORKER_INDEX_ENV,
    WorkerSupervisor,
    combine_exit_codes,
    read_worker_index,
    strip_option,
    worker_arguments,
)

posix_signals = pytest.mark.skipif(
    sys.platform == "win32",
    reason="os.kill() cannot deliver a signal to the test process on Windows",
)

# The scripts the supervisor runs. Each marks itself ready by creating a file
# named after its pid in READY, so a test can wait for the process to have
# its handlers installed before signalling it, and marks a graceful exit the
# same way in STOPPED. The shutdown signal is SIGTERM on POSIX and Ctrl-Break
# (SIGBREAK) on Windows; both are handled, as the real worker does.
PRELUDE = """
import os, signal, sys, time
ready, stopped = sys.argv[1], sys.argv[2]
def install(handler):
    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        signum = getattr(signal, name, None)
        if signum is not None:
            signal.signal(signum, handler)
def mark(directory):
    open(os.path.join(directory, str(os.getpid())), "w").close()
"""

GRACEFUL_WORKER = (
    PRELUDE
    + """
asked = False
def handler(signum, frame):
    global asked
    asked = True
install(handler)
mark(ready)
while not asked:
    time.sleep(0.02)
mark(stopped)
sys.exit(int(sys.argv[3]) if len(sys.argv) > 3 else 0)
"""
)

STUBBORN_WORKER = (
    PRELUDE
    + """
install(lambda signum, frame: None)
mark(ready)
while True:
    time.sleep(0.05)
"""
)

EXITING_WORKER = (
    PRELUDE
    + """
mark(ready)
sys.exit(int(sys.argv[3]))
"""
)

# Writes the slot index the supervisor gave it into its ready file.
INDEX_WORKER = (
    PRELUDE
    + f"""
with open(os.path.join(ready, str(os.getpid())), "w") as f:
    f.write(os.environ.get("{WORKER_INDEX_ENV}", ""))
sys.exit(int(sys.argv[3]))
"""
)


# Exits with 1 for its first argv[3] launches, counted by the ready files,
# then stays up until signalled.
FAILS_THEN_STAYS_UP_WORKER = (
    PRELUDE
    + """
mark(ready)
if len(os.listdir(ready)) <= int(sys.argv[3]):
    sys.exit(1)
asked = False
def handler(signum, frame):
    global asked
    asked = True
install(handler)
while not asked:
    time.sleep(0.02)
"""
)

# Exits with 1 for its first argv[3] launches, then with 0.
FAILS_THEN_EXITS_CLEANLY_WORKER = (
    PRELUDE
    + """
mark(ready)
sys.exit(1 if len(os.listdir(ready)) <= int(sys.argv[3]) else 0)
"""
)

# The first worker to start stays up until signalled; every other one exits
# with 1.
ONE_STAYS_UP_WORKER = (
    PRELUDE
    + """
mark(ready)
try:
    os.close(os.open(os.path.join(stopped, "first"), os.O_CREAT | os.O_EXCL))
except FileExistsError:
    sys.exit(1)
asked = False
def handler(signum, frame):
    global asked
    asked = True
install(handler)
while not asked:
    time.sleep(0.02)
"""
)


def wait_for(condition, timeout=20, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.02)
    pytest.fail(f"{what} not met within {timeout} seconds")


class Harness:
    """A supervisor over one of the scripts above, driven from a thread."""

    def __init__(self, tmp_path, script, *extra, **options):
        self.ready = tmp_path / "ready"
        self.stopped = tmp_path / "stopped"
        self.ready.mkdir()
        self.stopped.mkdir()
        self.out = StringIO()
        args = [sys.executable, "-c", script, str(self.ready), str(self.stopped)]
        args += [str(arg) for arg in extra]
        options.setdefault("tick", 0.05)
        options.setdefault("stdout", self.out)
        self.supervisor = WorkerSupervisor(args, **options)
        self.result = None
        self.error = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        try:
            self.result = self.supervisor.run()
        except BaseException as e:  # reported by join()
            self.error = e

    def start(self):
        self.thread.start()
        return self

    def join(self, timeout=30):
        self.thread.join(timeout)
        assert not self.thread.is_alive(), "the supervisor did not return"
        if self.error is not None:
            raise self.error
        return self.result

    def ready_count(self):
        return len(os.listdir(self.ready))

    def stopped_count(self):
        return len(os.listdir(self.stopped))

    def wait_ready(self, count):
        wait_for(lambda: self.ready_count() >= count, what=f"{count} workers ready")

    def wait_stopped(self, count):
        wait_for(lambda: self.stopped_count() >= count, what=f"{count} workers stopped")

    def wait_exits(self, count):
        wait_for(
            lambda: len(self.supervisor.exit_codes) >= count,
            what=f"{count} worker exits",
        )


class TestStripOption:
    @pytest.mark.parametrize(
        "args, expected",
        [
            (["--workers", "3"], []),
            (["--workers=3"], []),
            (["--worker", "3"], []),
            (["--wor=3"], []),
            (["a", "--workers", "3", "--continuous"], ["a", "--continuous"]),
            (["--workers", "3", "--workers=4"], []),
            (["--continuous"], ["--continuous"]),
            (["--wait-time", "3"], ["--wait-time", "3"]),
            (["--w", "3"], ["--w", "3"]),
            (["--", "--workers"], ["--", "--workers"]),
        ],
    )
    def test_removes_the_option_and_its_value(self, args, expected):
        assert strip_option(args, "--workers") == expected


class TestWorkerArguments:
    def test_is_how_this_process_started_without_workers(self):
        started_as = [sys.executable, "-Werror", "manage.py", "run", "--workers", "2"]
        with patch(
            "django_database_task.supervisor.get_child_arguments",
            return_value=started_as,
        ):
            assert worker_arguments() == [
                sys.executable,
                "-Werror",
                "manage.py",
                "run",
            ]


class TestCombineExitCodes:
    @pytest.mark.parametrize(
        "codes, empty, failed, expected",
        [
            ([], 0, 0, 0),
            ([0, 0], 0, 0, 0),
            ([0, 0], 4, 1, 0),
            ([4, 4], 4, 1, 4),
            ([4, 0], 4, 1, 0),
            ([4, 1], 4, 1, 1),
            ([0, 1, 4], 4, 1, 1),
            # A crash is passed on even when the codes are not opted into.
            ([0, 1], 0, 0, 1),
            ([2, 4], 4, 1, 2),
            ([0, 3], 4, 1, 3),
            # Killed by SIGKILL, reported the way a shell would.
            ([-9, 0], 0, 0, 137),
        ],
    )
    def test_precedence(self, codes, empty, failed, expected):
        assert combine_exit_codes(codes, empty, failed) == expected


class TestReadWorkerIndex:
    @pytest.mark.parametrize(
        "value, expected", [("1", 1), ("12", 12), ("01", 1), (None, None)]
    )
    def test_reads_a_whole_number_of_one_or_more(self, value, expected):
        environ = {} if value is None else {WORKER_INDEX_ENV: value}
        assert read_worker_index(environ) == expected

    @pytest.mark.parametrize(
        "value", ["", "0", "-1", "+3", " 3", "1_0", "3.0", "abc", "\u0663"]
    )
    def test_refuses_anything_else(self, value):
        with pytest.raises(ValueError, match=WORKER_INDEX_ENV):
            read_worker_index({WORKER_INDEX_ENV: value})

    def test_reads_the_process_environment_by_default(self, monkeypatch):
        monkeypatch.setenv(WORKER_INDEX_ENV, "5")
        assert read_worker_index() == 5


class TestSupervisorRunOnce:
    def test_rejects_less_than_one_worker(self):
        with pytest.raises(ValueError):
            WorkerSupervisor([sys.executable], 0)

    def test_runs_every_worker_once_and_returns_zero(self, tmp_path):
        harness = Harness(tmp_path, EXITING_WORKER, 0, workers=3, restart=False)
        result = harness.start().join()

        assert result == 0
        assert harness.supervisor.exit_codes == [0, 0, 0]
        assert harness.ready_count() == 3
        output = harness.out.getvalue()
        assert "Workers: 3" in output
        assert "Worker 3 started" in output
        assert "exited with code 0." in output
        assert "Every worker has exited." in output

    def test_reports_the_workers_exit_codes(self, tmp_path):
        harness = Harness(
            tmp_path,
            EXITING_WORKER,
            4,
            workers=2,
            restart=False,
            empty_exit_code=4,
            failed_exit_code=1,
        )
        assert harness.start().join() == 4

    def test_passes_a_crash_on(self, tmp_path):
        harness = Harness(tmp_path, EXITING_WORKER, 3, workers=2, restart=False)
        assert harness.start().join() == 3
        assert "exited with code 3." in harness.out.getvalue()

    def test_silent_at_verbosity_zero_except_for_errors(self, tmp_path):
        harness = Harness(
            tmp_path, EXITING_WORKER, 3, workers=1, restart=False, verbosity=0
        )
        harness.start().join()
        output = harness.out.getvalue()
        assert "Workers:" not in output
        assert "started" not in output
        assert "exited with code 3." in output


class TestSupervisorRestart:
    def test_replaces_a_worker_that_exits(self, tmp_path):
        harness = Harness(tmp_path, EXITING_WORKER, 0, workers=1, restart=True)
        harness.start()
        harness.wait_exits(3)
        harness.supervisor.request_shutdown()

        assert harness.join() == 0
        assert "; restarting." in harness.out.getvalue()

    def test_backs_off_a_worker_that_keeps_failing(self, tmp_path, caplog):
        harness = Harness(
            tmp_path,
            EXITING_WORKER,
            1,
            workers=1,
            restart=True,
            backoff_start=0.2,
            backoff_max=0.4,
            stable_after=60,
        )
        with caplog.at_level(logging.INFO, logger="django_database_task"):
            harness.start()
            harness.wait_exits(4)
            harness.supervisor.request_shutdown()
            harness.join()

        delays = [
            record.restart_delay
            for record in caplog.records
            if record.getMessage().startswith("Worker process exited")
        ]
        assert delays[:4] == [0.2, 0.4, 0.4, 0.4]
        assert "restarting in 0s" not in harness.out.getvalue()

    def test_a_clean_exit_is_restarted_at_once(self, tmp_path, caplog):
        harness = Harness(
            tmp_path, EXITING_WORKER, 0, workers=1, restart=True, stable_after=60
        )
        with caplog.at_level(logging.INFO, logger="django_database_task"):
            harness.start()
            harness.wait_exits(2)
            harness.supervisor.request_shutdown()
            harness.join()

        delays = [
            record.restart_delay
            for record in caplog.records
            if record.getMessage().startswith("Worker process exited")
        ]
        assert delays[:2] == [0.0, 0.0]


class TestSupervisorJoinsTheWorkersLogs:
    def started_records(self, caplog):
        return {
            record.pid: record.worker_index
            for record in caplog.records
            if record.getMessage().startswith("Worker process started")
        }

    def indexes_seen_by_workers(self, harness):
        return {
            int(name): (harness.ready / name).read_text()
            for name in os.listdir(harness.ready)
        }

    def test_each_worker_is_told_its_index(self, tmp_path, caplog):
        harness = Harness(tmp_path, INDEX_WORKER, 0, workers=3, restart=False)
        with caplog.at_level(logging.INFO, logger="django_database_task"):
            harness.start().join()

        started = self.started_records(caplog)
        seen = self.indexes_seen_by_workers(harness)
        assert sorted(started.values()) == [1, 2, 3]
        assert seen == {pid: str(index) for pid, index in started.items()}

    def test_a_restarted_worker_keeps_its_slots_index(self, tmp_path, caplog):
        harness = Harness(tmp_path, INDEX_WORKER, 0, workers=1, restart=True)
        with caplog.at_level(logging.INFO, logger="django_database_task"):
            harness.start()
            harness.wait_exits(2)
            harness.supervisor.request_shutdown()
            harness.join()

        seen = self.indexes_seen_by_workers(harness)
        assert len(seen) >= 2
        assert set(seen.values()) == {"1"}

    def test_the_supervisors_own_environment_is_left_alone(self, tmp_path, monkeypatch):
        monkeypatch.delenv(WORKER_INDEX_ENV, raising=False)
        harness = Harness(tmp_path, INDEX_WORKER, 0, workers=2, restart=False)
        harness.start().join()

        assert WORKER_INDEX_ENV not in os.environ


def messages_starting(caplog, prefix):
    return [r for r in caplog.records if r.getMessage().startswith(prefix)]


class TestSupervisorState:
    def test_every_worker_failing_is_reported_once(self, tmp_path, caplog):
        harness = Harness(
            tmp_path,
            EXITING_WORKER,
            1,
            workers=2,
            restart=True,
            backoff_start=0.05,
            backoff_max=0.1,
            stable_after=60,
        )
        with caplog.at_level(logging.INFO, logger="django_database_task"):
            harness.start()
            harness.wait_exits(6)
            harness.supervisor.request_shutdown()
            harness.join()

        (record,) = messages_starting(caplog, "Every worker is failing to start")
        assert record.levelno == logging.WARNING
        assert record.workers == 2
        assert record.exit_codes == [1, 1]
        assert not messages_starting(caplog, "Workers recovered")

    def test_one_worker_failing_is_not_every_worker(self, tmp_path, caplog):
        harness = Harness(
            tmp_path,
            ONE_STAYS_UP_WORKER,
            workers=2,
            restart=True,
            backoff_start=0.05,
            backoff_max=0.1,
            stable_after=60,
        )
        with caplog.at_level(logging.INFO, logger="django_database_task"):
            harness.start()
            harness.wait_exits(4)
            harness.supervisor.request_shutdown()
            harness.join()

        assert not messages_starting(caplog, "Every worker is failing to start")

    def test_a_worker_past_the_stable_window_clears_it(self, tmp_path, caplog):
        harness = Harness(
            tmp_path,
            FAILS_THEN_STAYS_UP_WORKER,
            2,
            workers=1,
            restart=True,
            backoff_start=0.05,
            backoff_max=0.1,
            stable_after=0.5,
        )
        with caplog.at_level(logging.INFO, logger="django_database_task"):
            harness.start()
            wait_for(
                lambda: messages_starting(caplog, "Workers recovered"),
                what="the recovery record",
            )
            (running,) = harness.supervisor.running_pids
            harness.supervisor.request_shutdown()
            harness.join()

        (failing,) = messages_starting(caplog, "Every worker is failing to start")
        (recovered,) = messages_starting(caplog, "Workers recovered")
        assert caplog.records.index(failing) < caplog.records.index(recovered)
        assert recovered.levelno == logging.INFO
        assert recovered.worker_index == 1
        assert recovered.pid == running
        assert recovered.failing_for >= 0.5
        assert recovered.recovered_by == "uptime"

    def test_a_clean_exit_clears_it(self, tmp_path, caplog):
        """A worker reaching --max-tasks quickly is working, not failing."""
        harness = Harness(
            tmp_path,
            FAILS_THEN_EXITS_CLEANLY_WORKER,
            2,
            workers=1,
            restart=True,
            backoff_start=0.05,
            backoff_max=0.1,
            stable_after=60,
        )
        with caplog.at_level(logging.INFO, logger="django_database_task"):
            harness.start()
            wait_for(
                lambda: messages_starting(caplog, "Workers recovered"),
                what="the recovery record",
            )
            harness.supervisor.request_shutdown()
            harness.join()

        (failing,) = messages_starting(caplog, "Every worker is failing to start")
        (recovered,) = messages_starting(caplog, "Workers recovered")
        clean_exits = [
            record
            for record in messages_starting(caplog, "Worker process exited")
            if record.exit_code == 0
        ]
        assert caplog.records.index(failing) < caplog.records.index(recovered)
        assert recovered.recovered_by == "clean_exit"
        assert recovered.pid == clean_exits[0].pid

    def test_a_new_slot_is_not_a_recovery(self, tmp_path, caplog):
        harness = Harness(
            tmp_path,
            EXITING_WORKER,
            1,
            workers=1,
            restart=True,
            backoff_start=0.05,
            backoff_max=0.1,
            stable_after=60,
        )
        with caplog.at_level(logging.INFO, logger="django_database_task"):
            harness.start()
            wait_for(
                lambda: messages_starting(caplog, "Every worker is failing"),
                what="the failing record",
            )
            harness.supervisor.add_worker()
            harness.wait_exits(6)
            harness.supervisor.request_shutdown()
            harness.join()

        assert not messages_starting(caplog, "Workers recovered")

    def test_finish_counts_the_restarts(self, tmp_path, caplog):
        harness = Harness(
            tmp_path,
            EXITING_WORKER,
            1,
            workers=1,
            restart=True,
            backoff_start=0.05,
            backoff_max=0.05,
            stable_after=60,
        )
        with caplog.at_level(logging.INFO, logger="django_database_task"):
            harness.start()
            harness.wait_exits(3)
            harness.supervisor.request_shutdown()
            harness.join()

        (record,) = messages_starting(caplog, "Supervisor finished")
        started = messages_starting(caplog, "Worker process started")
        assert record.restarts == len(started) - 1
        assert record.restarts >= 2
        assert record.abnormal_restarts == record.restarts

    def test_a_clean_exit_is_not_an_abnormal_restart(self, tmp_path, caplog):
        harness = Harness(
            tmp_path, EXITING_WORKER, 0, workers=1, restart=True, stable_after=60
        )
        with caplog.at_level(logging.INFO, logger="django_database_task"):
            harness.start()
            harness.wait_exits(3)
            harness.supervisor.request_shutdown()
            harness.join()

        (record,) = messages_starting(caplog, "Supervisor finished")
        assert record.restarts >= 2
        assert record.abnormal_restarts == 0
        assert not messages_starting(caplog, "Every worker is failing to start")

    def test_a_run_once_supervisor_restarts_nothing(self, tmp_path, caplog):
        harness = Harness(tmp_path, EXITING_WORKER, 3, workers=2, restart=False)
        with caplog.at_level(logging.INFO, logger="django_database_task"):
            harness.start().join()

        (record,) = messages_starting(caplog, "Supervisor finished")
        assert record.restarts == 0
        assert record.abnormal_restarts == 0
        assert not messages_starting(caplog, "Every worker is failing to start")


class TestSupervisorShutdown:
    def test_forwards_the_signal_and_waits_for_the_workers(self, tmp_path):
        harness = Harness(tmp_path, GRACEFUL_WORKER, workers=2, restart=True)
        harness.start()
        harness.wait_ready(2)
        harness.supervisor.request_shutdown()

        assert harness.join() == 0
        assert harness.stopped_count() == 2
        assert harness.supervisor.exit_codes == [0, 0]
        output = harness.out.getvalue()
        assert "stopping 2 worker(s)" in output
        assert "Shutdown complete: every worker exited." in output

    def test_the_workers_exit_code_is_kept_through_the_shutdown(self, tmp_path):
        harness = Harness(
            tmp_path, GRACEFUL_WORKER, 1, workers=2, restart=True, failed_exit_code=1
        )
        harness.start()
        harness.wait_ready(2)
        harness.supervisor.request_shutdown()

        assert harness.join() == 1

    def test_kills_the_workers_when_the_timeout_expires(self, tmp_path):
        harness = Harness(
            tmp_path, STUBBORN_WORKER, workers=2, restart=True, shutdown_timeout=0.5
        )
        harness.start()
        harness.wait_ready(2)
        started = time.monotonic()
        harness.supervisor.request_shutdown()

        assert harness.join() == FORCED_EXIT_CODE
        assert time.monotonic() - started < 15
        assert harness.supervisor.running_pids == []
        output = harness.out.getvalue()
        assert "Killing 2 worker(s): the shutdown timeout of 0.5 seconds expired" in (
            output
        )
        assert "Shutdown forced" in output

    def test_a_second_request_kills_the_workers(self, tmp_path):
        harness = Harness(tmp_path, STUBBORN_WORKER, workers=1, restart=True)
        harness.start()
        harness.wait_ready(1)
        harness.supervisor.request_shutdown()
        time.sleep(0.3)
        assert harness.thread.is_alive()
        harness.supervisor.request_shutdown()

        assert harness.join() == FORCED_EXIT_CODE
        output = harness.out.getvalue()
        assert "again: killing the workers" in output
        assert "a second signal was received" in output

    def test_without_graceful_shutdown_the_workers_are_killed_at_once(self, tmp_path):
        harness = Harness(
            tmp_path, STUBBORN_WORKER, workers=2, restart=True, graceful=False
        )
        harness.start()
        harness.wait_ready(2)
        harness.supervisor.request_shutdown()

        assert harness.join() == FORCED_EXIT_CODE
        assert "graceful shutdown is disabled" in harness.out.getvalue()

    def test_a_failure_in_the_supervisor_stops_the_workers(self, tmp_path):
        harness = Harness(tmp_path, GRACEFUL_WORKER, workers=2, restart=True)
        harness.supervisor._reap = None  # noqa: SLF001 - the bug under test
        with pytest.raises(TypeError):
            harness.start().join()
        assert harness.supervisor.running_pids == []


class TestSupervisorScaling:
    def test_add_and_remove_a_worker(self, tmp_path):
        harness = Harness(tmp_path, GRACEFUL_WORKER, workers=2, restart=True)
        harness.start()
        harness.wait_ready(2)

        harness.supervisor.add_worker()
        harness.wait_ready(3)
        assert len(harness.supervisor.running_pids) == 3

        harness.supervisor.remove_worker()
        harness.wait_stopped(1)
        wait_for(
            lambda: len(harness.supervisor.running_pids) == 2, what="2 workers left"
        )

        harness.supervisor.request_shutdown()
        assert harness.join() == 0
        output = harness.out.getvalue()
        assert "Adding worker 3." in output
        assert "Removing worker 3." in output
        # The removed worker was not replaced.
        assert harness.ready_count() == 3

    def test_the_last_worker_is_kept(self, tmp_path):
        harness = Harness(tmp_path, GRACEFUL_WORKER, workers=1, restart=True)
        harness.start()
        harness.wait_ready(1)

        harness.supervisor.remove_worker()
        wait_for(
            lambda: "Not removing the last worker." in harness.out.getvalue(),
            what="refusal reported",
        )
        assert len(harness.supervisor.running_pids) == 1

        harness.supervisor.request_shutdown()
        assert harness.join() == 0


@posix_signals
class TestSupervisorSignals:
    """The handlers themselves, installed in the main thread."""

    def run_with_signals(self, tmp_path, *signals):
        harness = Harness(tmp_path, GRACEFUL_WORKER, workers=1, restart=True)
        pid = os.getpid()

        def send():
            harness.wait_ready(1)
            for signum, expected_ready in signals:
                if expected_ready:
                    harness.wait_ready(expected_ready)
                    # Let the supervisor record the process it started.
                    wait_for(
                        lambda count=expected_ready: (
                            len(harness.supervisor.running_pids) == count
                        ),
                        what="running workers",
                    )
                os.kill(pid, signum)
                time.sleep(0.3)

        sender = threading.Thread(target=send, daemon=True)
        sender.start()
        result = harness.supervisor.run()
        sender.join(10)
        return harness, result

    def test_sigterm_is_forwarded(self, tmp_path):
        harness, result = self.run_with_signals(tmp_path, (signal.SIGTERM, 0))

        assert result == 0
        assert harness.stopped_count() == 1
        assert "Received SIGTERM: stopping 1 worker(s)" in harness.out.getvalue()

    def test_sigttin_adds_and_sigttou_removes_a_worker(self, tmp_path):
        harness, result = self.run_with_signals(
            tmp_path,
            (signal.SIGTTIN, 0),
            (signal.SIGTTOU, 2),
            (signal.SIGTERM, 0),
        )

        assert result == 0
        output = harness.out.getvalue()
        assert "Adding worker 2." in output
        assert "Removing worker 2." in output
        assert harness.ready_count() == 2
        assert harness.stopped_count() == 2

    def test_handlers_are_restored(self, tmp_path):
        before = {
            signum: signal.getsignal(signum)
            for signum in (
                signal.SIGTERM,
                signal.SIGINT,
                signal.SIGTTIN,
                signal.SIGTTOU,
            )
        }
        self.run_with_signals(tmp_path, (signal.SIGTERM, 0))
        after = {signum: signal.getsignal(signum) for signum in before}

        assert after == before
