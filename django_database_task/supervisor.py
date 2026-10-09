"""
Keep several copies of a worker command running.

``run_database_tasks --workers N`` hands its own command line to
:class:`WorkerSupervisor`, which starts N child processes running that
command line with ``--workers`` removed, replaces the ones that exit,
forwards a shutdown signal to all of them and turns their exit codes into
its own. It is deliberately small, in the shape of gunicorn's arbiter:
spawn, restart, forward, scale. Health checks, log collection and anything
past that belong to systemd, Kubernetes or supervisord, and a worker that
is alive but stuck is left to ``requeue_stale_database_tasks``.

The children are ``subprocess`` children rather than forks, so nothing the
parent opened (a database connection, a broker's socket) is shared with
them, and the same code runs on Windows, where there is no fork.

On POSIX the children are put in a process group of their own, so a Ctrl-C
typed at the terminal reaches the supervisor alone, which forwards
``SIGTERM``; delivered to both, a child would count it as its second signal
and exit at once. On Windows they are created with
``CREATE_NEW_PROCESS_GROUP`` for the same reason, and the forwarded signal
is Ctrl-Break, which the worker handles as ``SIGBREAK``.

This module depends on nothing in the package but ``shutdown.py``, so that
django-tasks-redis can ship the same one.
"""

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import ExitStack
from dataclasses import dataclass

from django.utils.autoreload import get_child_arguments

from django_database_task.shutdown import (
    FORCED_EXIT_CODE,
    GracefulShutdown,
    signal_name,
)

logger = logging.getLogger("django_database_task")

#: A worker that exits with a nonzero code sooner than this many seconds
#: after starting is treated as failing to start, and its restart is
#: delayed with a growing back-off so that a misconfiguration does not spin.
STABLE_AFTER = 10.0
#: The first delay before restarting a worker that failed to start, and the
#: delay it doubles up to.
RESTART_BACKOFF_START = 1.0
RESTART_BACKOFF_MAX = 30.0
#: How long the supervisor gives the workers past ``--shutdown-timeout``
#: before killing what is left. The workers run with the same timeout and
#: force their own exit when it expires, so this is a backstop, and giving
#: them a moment past it lets their own report of the timeout come out.
SHUTDOWN_GRACE = 1.0
#: The environment variable a worker finds its slot's index in. The
#: supervisor knows a child's pid but not the worker id the child picks for
#: itself, so the worker puts the index and its own pid on its log records
#: (see :func:`worker_log_fields`) under the names the supervisor uses.
WORKER_INDEX_ENV = "DJANGO_DATABASE_TASK_WORKER_INDEX"


def worker_log_fields():
    """
    The fields that join a worker's log records with its supervisor's.

    ``pid`` is this process's, the same value the supervisor's ``Worker
    process started`` and ``Worker process exited`` records carry for it.
    ``worker_index`` is the slot the supervisor started it in, and is left
    out when the process was not started by one.
    """
    fields = {"pid": os.getpid()}
    try:
        fields["worker_index"] = int(os.environ[WORKER_INDEX_ENV])
    except (KeyError, ValueError):
        pass
    return fields


def strip_option(args, name):
    """
    Return ``args`` without the option ``name`` and its value.

    ``--workers 3``, ``--workers=3`` and the unambiguous abbreviations
    argparse accepts (``--worker 3``) are all removed. A prefix of one
    letter (``--w``) is left alone: argparse rejects it as ambiguous before
    the command gets this far, and nothing after a ``--`` is an option.
    """
    stripped = []
    skip_next = False
    for position, arg in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if arg == "--":
            stripped.extend(args[position:])
            break
        key, has_value, _ = arg.partition("=")
        if len(key) >= 4 and key.startswith("--") and name.startswith(key):
            skip_next = not has_value
            continue
        stripped.append(arg)
    return stripped


def worker_arguments(option="--workers"):
    """
    The command line that runs this process again as a single worker.

    It is how this process was started (``manage.py``, ``python -m django``
    or a console script, with the interpreter's ``-W`` and ``-X`` flags),
    less the option that asked for several workers.
    """
    return strip_option([str(arg) for arg in get_child_arguments()], option)


def combine_exit_codes(codes, empty_exit_code=0, failed_exit_code=0):
    """
    Turn the exit codes of the workers into the supervisor's.

    The workers run with the same ``--empty-exit-code`` and
    ``--failed-exit-code``, so those two codes are what they report an idle
    run and a failed task with. A code that is neither of them nor 0 is a
    worker that crashed or was killed, and is passed on as it is (a worker
    killed by a signal reports ``128 + signal``, the way a shell does) since
    it is the one worth waking someone for. Past that, a failure in any
    worker wins over an idle run, and the run was idle only if every worker
    said so.
    """
    if not codes:
        return 0
    normal = {0, empty_exit_code, failed_exit_code}
    for code in codes:
        if code not in normal:
            return code if code > 0 else 128 - code
    if failed_exit_code and failed_exit_code in codes:
        return failed_exit_code
    if empty_exit_code and all(code == empty_exit_code for code in codes):
        return empty_exit_code
    return 0


def _exited_on_signal(code):
    """
    True if ``code`` is what a process exits with on the forwarded signal.

    A worker that has not finished starting has no handler yet, so the
    signal ends it outright: ``-SIGTERM`` on POSIX, ``STATUS_CONTROL_C_EXIT``
    on Windows. During a shutdown that is a worker stopping as asked, not
    one that crashed, and nothing was running in it to abandon.
    """
    if sys.platform == "win32":
        status_control_c_exit = 0xC000013A
        return code in (status_control_c_exit, status_control_c_exit - 2**32)
    return code == -signal.SIGTERM


class _NoStyle:
    def __getattr__(self, name):
        return lambda text: text


@dataclass
class _Worker:
    """One slot the supervisor keeps a process in."""

    #: 1-based, for the messages.
    index: int
    process: subprocess.Popen | None = None
    #: When the process was started, on the monotonic clock.
    started_at: float = 0.0
    #: When the slot is due to (re)start, on the monotonic clock; None
    #: while a process is running or the slot is finished.
    restart_at: float | None = None
    #: Consecutive nonzero exits within STABLE_AFTER of starting.
    failures: int = 0
    #: Asked to stop by :meth:`WorkerSupervisor.remove_worker`; the slot
    #: is dropped when its process exits instead of being restarted.
    retiring: bool = False
    #: The pid of the last process, kept for the exit message.
    pid: int | None = None

    @property
    def running(self):
        return self.process is not None


class WorkerSupervisor:
    """
    Run ``workers`` copies of ``args`` and keep them running.

    Args:
        args: The command line of one worker, as a list.
        workers: How many to run.
        restart: Replace a worker that exits. With False the supervisor
            returns once every worker has exited, which is the run-once
            shape of the command; with True it runs until it is signalled.
        graceful: Forward a shutdown signal and wait for the workers to
            finish. With False they are killed as soon as the signal
            arrives, which is what ``--no-graceful-shutdown`` promises.
        shutdown_timeout: Seconds to wait for the workers after forwarding
            the signal before killing what is left. 0 waits indefinitely.
        empty_exit_code, failed_exit_code: The codes the workers report an
            idle run and a failed task with; see :func:`combine_exit_codes`.
        stdout: Where progress is written; anything with ``write()``.
        style: Something with ``SUCCESS``, ``WARNING`` and ``ERROR``
            callables, such as a command's ``style``; plain text otherwise.
        verbosity: 0 reports errors only, 1 and up every start and exit.
        tick: Seconds between looks at the children.
    """

    def __init__(
        self,
        args,
        workers,
        *,
        restart=True,
        graceful=True,
        shutdown_timeout=0,
        empty_exit_code=0,
        failed_exit_code=0,
        stdout=None,
        style=None,
        verbosity=1,
        tick=0.5,
        stable_after=STABLE_AFTER,
        backoff_start=RESTART_BACKOFF_START,
        backoff_max=RESTART_BACKOFF_MAX,
    ):
        if workers < 1:
            raise ValueError("workers must be at least 1")
        self.args = list(args)
        self.workers = workers
        self.restart = restart
        self.graceful = graceful
        self.shutdown_timeout = shutdown_timeout
        self.empty_exit_code = empty_exit_code
        self.failed_exit_code = failed_exit_code
        self.stdout = stdout if stdout is not None else sys.stdout
        self.style = style if style is not None else _NoStyle()
        self.verbosity = verbosity
        self.tick = tick
        self.stable_after = stable_after
        self.backoff_start = backoff_start
        self.backoff_max = backoff_max

        self._workers = []
        self._next_index = 1
        self._exit_codes = []
        self._forced = False
        self._pending_scale = 0
        self._scaling_handlers = {}
        self._shutdown = GracefulShutdown(
            timeout=0,
            on_signal=self._on_signal,
            # The second signal is acted on by the shutdown loop, which kills
            # the workers first; exiting from the handler would orphan them.
            force_on_repeat=False,
            log_signals=False,
        )

    # -- what the command and a test call ------------------------------

    def run(self):
        """
        Run the workers and return the exit code for the command.

        Signal handlers are installed for as long as this runs, when called
        from the main thread. From another thread the supervisor is driven
        with :meth:`request_shutdown` instead.
        """
        self._report_start()
        with ExitStack() as stack:
            stack.enter_context(self._shutdown)
            self._install_scaling_handlers()
            stack.callback(self._uninstall_scaling_handlers)
            for _ in range(self.workers):
                self._add_slot()
            try:
                self._supervise()
            except BaseException:
                # A bug here must not leave the workers behind, running
                # with nobody to stop them.
                self._shutdown.set()
                self._stop_workers()
                raise
            if self._shutdown.is_set():
                self._stop_workers()
        return self._finish()

    def request_shutdown(self):
        """
        Ask the supervisor to stop, as a shutdown signal would.

        The first request forwards the signal to the workers and waits for
        them; a second one kills what is still running.
        """
        count = self._shutdown.set()
        self._on_signal(None, count)

    def add_worker(self):
        """Start one more worker, as ``SIGTTIN`` does on POSIX."""
        self._pending_scale += 1

    def remove_worker(self):
        """
        Stop the newest worker and do not replace it, as ``SIGTTOU`` does.

        The last worker is kept: a supervisor with nothing to supervise
        would sit idle, and stopping the command is what a signal is for.
        """
        self._pending_scale -= 1

    @property
    def exit_codes(self):
        """Every exit code the workers reported, in the order they exited."""
        return list(self._exit_codes)

    @property
    def running_pids(self):
        return [worker.process.pid for worker in self._workers if worker.running]

    # -- the loop ---------------------------------------------------------

    def _supervise(self):
        while not self._shutdown.is_set():
            self._apply_scaling()
            self._reap()
            self._start_due()
            if not any(
                worker.running or worker.restart_at is not None
                for worker in self._workers
            ):
                return
            self._shutdown.wait(self.tick)

    def _add_slot(self):
        worker = _Worker(index=self._next_index, restart_at=time.monotonic())
        self._next_index += 1
        self._workers.append(worker)
        return worker

    def _apply_scaling(self):
        while self._pending_scale > 0:
            self._pending_scale -= 1
            worker = self._add_slot()
            self._write(f"Adding worker {worker.index}.")
            logger.info(
                "Worker slot added: index=%d",
                worker.index,
                extra={"worker_index": worker.index},
            )
        while self._pending_scale < 0:
            self._pending_scale += 1
            self._retire_newest()

    def _retire_newest(self):
        active = [worker for worker in self._workers if not worker.retiring]
        if len(active) <= 1:
            self._write(
                self.style.WARNING("Not removing the last worker."),
                minimum_verbosity=0,
            )
            return
        worker = active[-1]
        worker.retiring = True
        self._write(f"Removing worker {worker.index}.")
        logger.info(
            "Worker slot removed: index=%d",
            worker.index,
            extra={"worker_index": worker.index},
        )
        if worker.running:
            self._signal_worker(worker)
        else:
            self._workers.remove(worker)

    def _start_due(self):
        now = time.monotonic()
        for worker in self._workers:
            if (
                not worker.running
                and worker.restart_at is not None
                and worker.restart_at <= now
            ):
                self._spawn(worker)

    def _spawn(self, worker):
        if sys.platform == "win32":
            kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        else:
            kwargs = {"process_group": 0}
        env = {**os.environ, WORKER_INDEX_ENV: str(worker.index)}
        worker.process = subprocess.Popen(self.args, env=env, **kwargs)
        worker.pid = worker.process.pid
        worker.started_at = time.monotonic()
        worker.restart_at = None
        self._write(f"Worker {worker.index} started (pid {worker.pid}).")
        logger.info(
            "Worker process started: index=%d pid=%d",
            worker.index,
            worker.pid,
            extra={"worker_index": worker.index, "pid": worker.pid},
        )

    def _reap(self):
        """Record every worker that has exited, and decide what to do about it."""
        now = time.monotonic()
        for worker in list(self._workers):
            if not worker.running or worker.process.poll() is None:
                continue
            code = worker.process.returncode
            uptime = now - worker.started_at
            worker.process = None
            # A worker asked to stop, by the shutdown or by remove_worker(),
            # that had no handler yet is a stopped worker, not a crashed one.
            asked_to_stop = self._shutdown.is_set() or worker.retiring
            stopped = asked_to_stop and _exited_on_signal(code)
            self._exit_codes.append(0 if stopped else code)

            if worker.retiring:
                self._workers.remove(worker)
                delay = None
            elif not self.restart or self._shutdown.is_set():
                delay = None
            else:
                delay = self._restart_delay(worker, code, uptime)
                worker.restart_at = now + delay
            self._report_exit(worker, code, uptime, delay, stopped)

    def _restart_delay(self, worker, code, uptime):
        if code == 0 or uptime >= self.stable_after:
            worker.failures = 0
            return 0.0
        worker.failures += 1
        return min(self.backoff_start * 2 ** (worker.failures - 1), self.backoff_max)

    # -- stopping ---------------------------------------------------------

    def _stop_workers(self):
        """Forward the shutdown to the workers and wait for them to exit."""
        if not any(worker.running for worker in self._workers):
            return
        for worker in self._workers:
            if worker.running:
                self._signal_worker(worker)
        if not self.graceful:
            deadline = time.monotonic()
        elif self.shutdown_timeout and self.shutdown_timeout > 0:
            deadline = time.monotonic() + self.shutdown_timeout + SHUTDOWN_GRACE
        else:
            deadline = None

        while True:
            self._reap()
            if not any(worker.running for worker in self._workers):
                return
            if self._shutdown.request_count > 1:
                self._kill_workers("a second signal was received")
                return
            if deadline is not None and time.monotonic() >= deadline:
                if self.graceful:
                    reason = (
                        f"the shutdown timeout of {self.shutdown_timeout} "
                        "seconds expired"
                    )
                else:
                    reason = "graceful shutdown is disabled"
                self._kill_workers(reason)
                return
            # Not the shutdown event, which is already set.
            time.sleep(self.tick)

    def _kill_workers(self, reason):
        self._forced = True
        killed = [worker for worker in self._workers if worker.running]
        self._write(
            self.style.ERROR(f"Killing {len(killed)} worker(s): {reason}."),
            minimum_verbosity=0,
        )
        for worker in killed:
            try:
                worker.process.kill()
            except OSError:  # pragma: no cover - exited in between
                pass
        for worker in killed:
            worker.process.wait()
            code = worker.process.returncode
            worker.process = None
            self._exit_codes.append(code)
            logger.warning(
                "Worker process killed: index=%d pid=%d reason=%s",
                worker.index,
                worker.pid,
                reason,
                extra={
                    "worker_index": worker.index,
                    "pid": worker.pid,
                    "exit_code": code,
                },
            )

    def _signal_worker(self, worker):
        """Send the worker the signal a supervisor sends on its platform."""
        if sys.platform == "win32":
            signum = signal.CTRL_BREAK_EVENT
        else:
            signum = signal.SIGTERM
        try:
            worker.process.send_signal(signum)
        except OSError:  # pragma: no cover - exited in between
            pass

    def _finish(self):
        if self._forced:
            code = FORCED_EXIT_CODE
            self._write(
                self.style.ERROR("\nShutdown forced: a worker was killed."),
                minimum_verbosity=0,
            )
        else:
            code = combine_exit_codes(
                self._exit_codes, self.empty_exit_code, self.failed_exit_code
            )
            if self._shutdown.is_set():
                self._write(
                    self.style.WARNING("\nShutdown complete: every worker exited.")
                )
            else:
                self._write("\nEvery worker has exited.")
        logger.info(
            "Supervisor finished: exit_code=%d",
            code,
            extra={"exit_code": code, "exit_codes": list(self._exit_codes)},
        )
        return code

    # -- signals ----------------------------------------------------------

    def _on_signal(self, signum, count):
        name = signal_name(signum)
        running = sum(1 for worker in self._workers if worker.running)
        if count == 1:
            if self.graceful:
                message = (
                    f"\nReceived {name}: stopping {running} worker(s). "
                    "Waiting for them to finish "
                    "(send the signal again to force exit)."
                )
            else:
                message = f"\nReceived {name}: terminating {running} worker(s)."
            logger.warning(
                "Received %s: stopping the workers.",
                name,
                extra={"workers_running": running},
            )
        else:
            message = f"\nReceived {name} again: killing the workers."
            logger.warning("Received %s again while shutting down.", name)
        self._write(self.style.WARNING(message))
        self._flush()

    def _install_scaling_handlers(self):
        """
        Handle ``SIGTTIN`` and ``SIGTTOU`` as gunicorn does, on POSIX.

        Their default action stops a background process that touches the
        terminal, which a worker never does, so taking them over costs
        nothing. Only the main thread may install a handler; from another
        thread :meth:`add_worker` and :meth:`remove_worker` still work.
        """
        if not hasattr(signal, "SIGTTIN"):
            return
        if threading.current_thread() is not threading.main_thread():
            return
        for signum in (signal.SIGTTIN, signal.SIGTTOU):
            try:
                self._scaling_handlers[signum] = signal.signal(
                    signum, self._handle_scaling_signal
                )
            except (OSError, ValueError) as e:  # pragma: no cover - defensive
                logger.warning("Could not install handler for signal %s: %s", signum, e)

    def _uninstall_scaling_handlers(self):
        for signum, handler in self._scaling_handlers.items():
            try:
                signal.signal(signum, signal.SIG_DFL if handler is None else handler)
            except (OSError, ValueError) as e:  # pragma: no cover - defensive
                logger.warning("Could not restore handler for signal %s: %s", signum, e)
        self._scaling_handlers.clear()

    def _handle_scaling_signal(self, signum, frame):
        # Only a counter is touched here; the process is started or stopped
        # by the loop, outside the handler.
        if signum == signal.SIGTTIN:
            self.add_worker()
        else:
            self.remove_worker()

    # -- reporting --------------------------------------------------------

    def _report_start(self):
        self._write(f"Workers: {self.workers}")
        self._write(f"Worker command: {subprocess.list2cmdline(self.args)}")
        if self.restart:
            self._write("Workers that exit are restarted.")
        else:
            self._write("The command exits when every worker has.")
        logger.info(
            "Supervisor started: workers=%d",
            self.workers,
            extra={"workers": self.workers, "worker_command": list(self.args)},
        )

    def _report_exit(self, worker, code, uptime, delay, stopped):
        abnormal = not stopped and code not in (0, self.empty_exit_code)
        message = f"Worker {worker.index} (pid {worker.pid}) exited"
        if stopped:
            message += " on the shutdown signal."
        elif delay:
            message += (
                f" with code {code} after {uptime:.1f}s; restarting in {delay:g}s."
            )
        elif delay is not None:
            message += f" with code {code}; restarting."
        else:
            message += f" with code {code}."
        if abnormal:
            self._write(self.style.ERROR(message), minimum_verbosity=0)
        else:
            self._write(message)
        logger.log(
            logging.WARNING if abnormal else logging.INFO,
            "Worker process exited: index=%d pid=%d code=%d",
            worker.index,
            worker.pid,
            code,
            extra={
                "worker_index": worker.index,
                "pid": worker.pid,
                "exit_code": code,
                "uptime": uptime,
                "restart_delay": delay,
            },
        )

    def _write(self, message, minimum_verbosity=1):
        if self.verbosity < minimum_verbosity:
            return
        self.stdout.write(message if message.endswith("\n") else message + "\n")

    def _flush(self):
        flush = getattr(self.stdout, "flush", None)
        if flush is not None:
            flush()
