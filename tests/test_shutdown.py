"""Tests for graceful shutdown support."""

import os
import signal
import threading
import time

import pytest

from django_database_task.shutdown import (
    DEFAULT_SHUTDOWN_SIGNALS,
    GracefulShutdown,
    get_active_shutdown,
    is_shutdown_requested,
    signal_name,
)


class TestGracefulShutdownState:
    def test_not_requested_initially(self):
        """No shutdown is requested before a signal arrives."""
        shutdown = GracefulShutdown()

        assert shutdown.is_set() is False
        assert shutdown.is_requested is False
        assert shutdown.request_count == 0
        assert shutdown.signal_number is None

    def test_set_marks_shutdown(self):
        """set() requests a shutdown."""
        shutdown = GracefulShutdown()

        count = shutdown.set()

        assert count == 1
        assert shutdown.is_set() is True

    def test_set_counts_requests(self):
        """Repeated requests are counted."""
        shutdown = GracefulShutdown()

        assert shutdown.set() == 1
        assert shutdown.set() == 2
        assert shutdown.request_count == 2

    def test_wait_blocks_until_timeout(self):
        """wait() returns False when no shutdown is requested."""
        shutdown = GracefulShutdown()

        started = time.monotonic()
        result = shutdown.wait(0.1)
        elapsed = time.monotonic() - started

        assert result is False
        assert elapsed >= 0.1

    def test_wait_returns_immediately_after_request(self):
        """wait() does not sleep once a shutdown is requested."""
        shutdown = GracefulShutdown()
        shutdown.set()

        started = time.monotonic()
        result = shutdown.wait(30)
        elapsed = time.monotonic() - started

        assert result is True
        assert elapsed < 1

    def test_wait_is_interrupted_by_request(self):
        """A shutdown request wakes up a waiting worker."""
        shutdown = GracefulShutdown()
        timer = threading.Timer(0.1, shutdown.set)
        timer.daemon = True
        timer.start()

        started = time.monotonic()
        result = shutdown.wait(30)
        elapsed = time.monotonic() - started

        assert result is True
        assert elapsed < 5


class TestGracefulShutdownSignals:
    def test_install_and_uninstall_restores_handlers(self):
        """Original signal handlers are restored on exit."""
        original_term = signal.getsignal(signal.SIGTERM)
        original_int = signal.getsignal(signal.SIGINT)

        shutdown = GracefulShutdown()
        with shutdown:
            assert shutdown.installed is True
            assert signal.getsignal(signal.SIGTERM) is not original_term
            assert signal.getsignal(signal.SIGINT) is not original_int

        assert shutdown.installed is False
        assert signal.getsignal(signal.SIGTERM) is original_term
        assert signal.getsignal(signal.SIGINT) is original_int

    def test_sigterm_requests_shutdown(self):
        """SIGTERM requests a graceful shutdown instead of killing."""
        with GracefulShutdown() as shutdown:
            os.kill(os.getpid(), signal.SIGTERM)

            assert shutdown.is_set() is True
            assert shutdown.signal_number == signal.SIGTERM

    def test_sigint_requests_shutdown(self):
        """SIGINT requests a graceful shutdown instead of KeyboardInterrupt."""
        with GracefulShutdown() as shutdown:
            os.kill(os.getpid(), signal.SIGINT)

            assert shutdown.is_set() is True
            assert shutdown.signal_number == signal.SIGINT

    def test_on_signal_callback_is_called(self):
        """The on_signal callback receives the signal and the count."""
        calls = []

        with GracefulShutdown(on_signal=lambda s, c: calls.append((s, c))):
            os.kill(os.getpid(), signal.SIGTERM)

        assert calls == [(signal.SIGTERM, 1)]

    def test_second_signal_forces_exit(self, monkeypatch):
        """A second signal exits the process immediately."""
        forced = []
        monkeypatch.setattr(
            GracefulShutdown, "force_exit", lambda self: forced.append(True)
        )

        with GracefulShutdown() as shutdown:
            os.kill(os.getpid(), signal.SIGTERM)
            assert forced == []

            os.kill(os.getpid(), signal.SIGTERM)
            assert forced == [True]
            assert shutdown.request_count == 2

    def test_force_on_repeat_disabled(self, monkeypatch):
        """force_on_repeat=False keeps waiting for the running task."""
        forced = []
        monkeypatch.setattr(
            GracefulShutdown, "force_exit", lambda self: forced.append(True)
        )

        with GracefulShutdown(force_on_repeat=False):
            os.kill(os.getpid(), signal.SIGTERM)
            os.kill(os.getpid(), signal.SIGTERM)

        assert forced == []

    def test_signals_are_logged_by_default(self, caplog):
        """A received signal is logged for library users."""
        with caplog.at_level("WARNING", logger="django_database_task"):
            with GracefulShutdown():
                os.kill(os.getpid(), signal.SIGTERM)

        assert "Received SIGTERM" in caplog.text

    def test_log_signals_can_be_disabled(self, caplog):
        """log_signals=False avoids duplicate reporting."""
        with caplog.at_level("WARNING", logger="django_database_task"):
            with GracefulShutdown(log_signals=False):
                os.kill(os.getpid(), signal.SIGTERM)

        assert "Received SIGTERM" not in caplog.text

    def test_custom_signals(self):
        """Only the requested signals are handled."""
        original_int = signal.getsignal(signal.SIGINT)

        with GracefulShutdown(signals=(signal.SIGTERM,)):
            assert signal.getsignal(signal.SIGINT) is original_int

    def test_install_outside_main_thread_is_ignored(self):
        """Installing from a non-main thread logs instead of raising."""
        shutdown = GracefulShutdown()
        errors = []

        def install():
            try:
                shutdown.install()
            except Exception as e:  # pragma: no cover - should not happen
                errors.append(e)

        thread = threading.Thread(target=install)
        thread.start()
        thread.join()

        assert errors == []
        assert shutdown.installed is False


class TestShutdownTimeout:
    def test_timeout_forces_exit(self, monkeypatch):
        """The process is killed when the shutdown timeout expires."""
        forced = threading.Event()
        monkeypatch.setattr(GracefulShutdown, "force_exit", lambda self: forced.set())

        shutdown = GracefulShutdown(timeout=0.1)
        shutdown.set()

        assert forced.wait(5) is True

    def test_no_timeout_waits_forever(self, monkeypatch):
        """timeout=0 never forces an exit."""
        forced = threading.Event()
        monkeypatch.setattr(GracefulShutdown, "force_exit", lambda self: forced.set())

        shutdown = GracefulShutdown(timeout=0)
        shutdown.set()

        assert forced.wait(0.3) is False

    def test_timer_is_cancelled_on_uninstall(self, monkeypatch):
        """Leaving the context cancels the pending forced exit."""
        forced = threading.Event()
        monkeypatch.setattr(GracefulShutdown, "force_exit", lambda self: forced.set())

        with GracefulShutdown(timeout=0.3) as shutdown:
            shutdown.set()

        assert forced.wait(0.6) is False


class TestActiveShutdown:
    def test_no_active_shutdown_by_default(self):
        """There is no active shutdown outside of the context manager."""
        assert get_active_shutdown() is None
        assert is_shutdown_requested() is False

    def test_active_shutdown_inside_context(self):
        """The context manager registers itself as active."""
        with GracefulShutdown() as shutdown:
            assert get_active_shutdown() is shutdown
            assert is_shutdown_requested() is False

            shutdown.set()
            assert is_shutdown_requested() is True

        assert get_active_shutdown() is None
        assert is_shutdown_requested() is False

    def test_nested_contexts_restore_previous(self):
        """Nested contexts restore the outer shutdown object."""
        with GracefulShutdown() as outer:
            with GracefulShutdown() as inner:
                assert get_active_shutdown() is inner
            assert get_active_shutdown() is outer

        assert get_active_shutdown() is None


class TestSignalName:
    @pytest.mark.parametrize(
        "signum,expected",
        [
            (signal.SIGTERM, "SIGTERM"),
            (signal.SIGINT, "SIGINT"),
            (None, "shutdown request"),
        ],
    )
    def test_signal_name(self, signum, expected):
        """Signal numbers are rendered with their name."""
        assert signal_name(signum) == expected

    def test_default_signals(self):
        """SIGINT and SIGTERM are handled by default."""
        assert set(DEFAULT_SHUTDOWN_SIGNALS) == {signal.SIGINT, signal.SIGTERM}
