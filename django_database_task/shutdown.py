"""
Graceful shutdown support for task runners.

When a worker process receives a termination signal (typically ``SIGTERM``
sent by a container orchestrator during a deployment), it should stop
picking up new tasks but let the task it is currently running finish,
instead of being killed in the middle of the work.

Example usage:
    from django_database_task import GracefulShutdown, is_shutdown_requested

    with GracefulShutdown() as shutdown:
        while not shutdown.is_set():
            task = fetch_task()
            if task is None:
                if shutdown.wait(5):  # interruptible sleep
                    break
                continue
            run(task)

Long running task functions can cooperate with the shutdown by checking
``is_shutdown_requested()`` and returning early:

    from django_database_task import is_shutdown_requested

    @task
    def process_rows(row_ids):
        for row_id in row_ids:
            if is_shutdown_requested():
                break
            handle(row_id)
"""

import logging
import os
import signal
import sys
import threading

logger = logging.getLogger("django_database_task")

#: Signals handled by default. ``SIGTERM`` is what container orchestrators
#: (Kubernetes, Cloud Run, systemd, Docker) send before killing a process,
#: ``SIGINT`` is Ctrl-C during development.
DEFAULT_SHUTDOWN_SIGNALS = (signal.SIGINT, signal.SIGTERM)

#: Exit code used when the graceful shutdown period expires or when a second
#: signal forces an immediate exit.
FORCED_EXIT_CODE = 1

_active_shutdown = None


class GracefulShutdown:
    """
    Track shutdown requests coming from OS signals.

    The instance duck-types :class:`threading.Event`, so it can be passed
    anywhere a stop event is expected (for example the ``stop_event``
    argument of :func:`django_database_task.process_tasks`).

    Args:
        signals: Signals to handle (default: ``SIGINT`` and ``SIGTERM``).
        timeout: Maximum number of seconds to keep running after a shutdown
            request before forcing the process to exit. ``0`` (default)
            waits indefinitely for the current task to finish.
        on_signal: Optional callable invoked as ``on_signal(signum, count)``
            from the signal handler, where ``count`` is the number of
            shutdown requests received so far. Useful for reporting to the
            console.
        force_on_repeat: If True (default), a second signal exits the
            process immediately instead of waiting for the current task.
        log_signals: If True (default), received signals are logged. Set to
            False when ``on_signal`` already reports them, to avoid printing
            the same information twice.
    """

    def __init__(
        self,
        signals=None,
        timeout=0,
        on_signal=None,
        force_on_repeat=True,
        log_signals=True,
    ):
        self._signals = tuple(DEFAULT_SHUTDOWN_SIGNALS if signals is None else signals)
        self.timeout = timeout
        self.on_signal = on_signal
        self.force_on_repeat = force_on_repeat
        self.log_signals = log_signals

        self._event = threading.Event()
        self._lock = threading.Lock()
        self._original_handlers = {}
        self._installed = False
        self._request_count = 0
        self._signum = None
        self._timer = None

    # -- state ---------------------------------------------------------

    def is_set(self):
        """Return True if a shutdown has been requested."""
        return self._event.is_set()

    @property
    def is_requested(self):
        """True if a shutdown has been requested (alias of :meth:`is_set`)."""
        return self._event.is_set()

    @property
    def event(self):
        """The underlying :class:`threading.Event`."""
        return self._event

    @property
    def signal_number(self):
        """Signal number that triggered the shutdown, or None."""
        return self._signum

    @property
    def request_count(self):
        """Number of shutdown requests received so far."""
        return self._request_count

    @property
    def installed(self):
        """True if signal handlers are currently installed."""
        return self._installed

    # -- control -------------------------------------------------------

    def set(self, signum=None):
        """
        Request a shutdown programmatically.

        Returns:
            The number of shutdown requests received so far.
        """
        with self._lock:
            self._request_count += 1
            count = self._request_count
            first = not self._event.is_set()
            if first:
                self._signum = signum
                self._event.set()
        if first:
            self._start_timer()
        return count

    def wait(self, timeout=None):
        """
        Sleep for up to ``timeout`` seconds, returning early on shutdown.

        Returns:
            True if a shutdown has been requested, False on timeout.
        """
        return self._event.wait(timeout)

    # -- signal handling -----------------------------------------------

    def install(self):
        """
        Install the signal handlers.

        Signal handlers can only be installed from the main thread; when
        called from another thread a warning is logged and the shutdown
        object stays usable but is never triggered by signals.
        """
        if self._installed:
            return self

        if threading.current_thread() is not threading.main_thread():
            logger.warning(
                "Graceful shutdown signal handlers can only be installed "
                "from the main thread; signals will not be handled."
            )
            return self

        for signum in self._signals:
            try:
                self._original_handlers[signum] = signal.signal(
                    signum, self._handle_signal
                )
            except (OSError, ValueError) as e:
                logger.warning("Could not install handler for signal %s: %s", signum, e)

        self._installed = bool(self._original_handlers)
        return self

    def uninstall(self):
        """Restore the previously installed signal handlers."""
        for signum, handler in self._original_handlers.items():
            try:
                # A handler set outside of Python is reported as None and
                # cannot be restored; fall back to the default behaviour.
                signal.signal(signum, signal.SIG_DFL if handler is None else handler)
            except (OSError, ValueError) as e:  # pragma: no cover - defensive
                logger.warning("Could not restore handler for signal %s: %s", signum, e)
        self._original_handlers.clear()
        self._installed = False
        self._cancel_timer()

    def _handle_signal(self, signum, frame):
        count = self.set(signum)

        if self.on_signal is not None:
            try:
                self.on_signal(signum, count)
            except Exception:  # pragma: no cover - defensive
                logger.exception("Error in shutdown signal callback")

        name = signal_name(signum)
        if count == 1:
            if self.log_signals:
                logger.warning(
                    "Received %s: finishing the current task before shutting down.",
                    name,
                )
        else:
            if self.log_signals:
                logger.warning("Received %s again while shutting down.", name)
            if self.force_on_repeat:
                self.force_exit()

    # -- forced exit ----------------------------------------------------

    def _start_timer(self):
        if not self.timeout or self.timeout <= 0:
            return
        self._timer = threading.Timer(self.timeout, self._on_timeout)
        self._timer.daemon = True
        self._timer.start()

    def _cancel_timer(self):
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _on_timeout(self):
        logger.error(
            "Graceful shutdown timed out after %s seconds; forcing exit.",
            self.timeout,
        )
        self.force_exit()

    def force_exit(self):
        """
        Terminate the process immediately.

        The task being executed is abandoned and stays in ``RUNNING`` status
        in the database, so this is only used when the caller explicitly
        asked for it (a second signal or an expired shutdown timeout).
        """
        _flush_std_streams()
        os._exit(FORCED_EXIT_CODE)

    # -- context manager -------------------------------------------------

    def __enter__(self):
        global _active_shutdown

        self._previous_active = _active_shutdown
        _active_shutdown = self
        self.install()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        global _active_shutdown

        self.uninstall()
        _active_shutdown = self._previous_active
        self._previous_active = None
        return False


def signal_name(signum):
    """Return a human readable name for a signal number."""
    if signum is None:
        return "shutdown request"
    try:
        return signal.Signals(signum).name
    except ValueError:  # pragma: no cover - defensive
        return f"signal {signum}"


def _flush_std_streams():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:  # pragma: no cover - defensive
            pass


def get_active_shutdown():
    """
    Return the :class:`GracefulShutdown` currently active, or None.

    A ``GracefulShutdown`` becomes active while it is used as a context
    manager, which is what the ``run_database_tasks`` command does.
    """
    return _active_shutdown


def is_shutdown_requested():
    """
    Return True if the active worker has been asked to shut down.

    Task functions can call this to stop long running work early and let
    the worker exit quickly.
    """
    shutdown = _active_shutdown
    return shutdown is not None and shutdown.is_set()
