import socket
import uuid
from contextlib import ExitStack

from django.core.management.base import BaseCommand
from django.tasks import task_backends
from django.tasks.base import TaskResultStatus

from django_database_task.executor import fetch_task
from django_database_task.shutdown import GracefulShutdown, signal_name


class Command(BaseCommand):
    help = "Execute tasks queued in the database"

    def add_arguments(self, parser):
        parser.add_argument(
            "--queue",
            type=str,
            default=None,
            help="Queue name to process (all queues if not specified)",
        )
        parser.add_argument(
            "--backend",
            type=str,
            default="default",
            help="Backend name (default: default)",
        )
        parser.add_argument(
            "--continuous",
            action="store_true",
            help="Continuous mode (keep polling even when no tasks)",
        )
        parser.add_argument(
            "--interval",
            type=float,
            default=5.0,
            help="Polling interval in seconds for continuous mode (default: 5)",
        )
        parser.add_argument(
            "--max-tasks",
            type=int,
            default=0,
            help="Maximum number of tasks to process (0=unlimited, default: 0)",
        )
        parser.add_argument(
            "--shutdown-timeout",
            type=float,
            default=0.0,
            help=(
                "Maximum seconds to wait for the running task after receiving "
                "SIGTERM/SIGINT before forcing exit "
                "(0=wait indefinitely, default: 0)"
            ),
        )
        parser.add_argument(
            "--no-graceful-shutdown",
            action="store_true",
            help=(
                "Do not install SIGTERM/SIGINT handlers; the process is "
                "terminated immediately, even while a task is running"
            ),
        )

    def handle(self, *args, **options):
        queue_name = options["queue"]
        backend_name = options["backend"]
        continuous = options["continuous"]
        interval = options["interval"]
        max_tasks = options["max_tasks"]
        shutdown_timeout = options["shutdown_timeout"]
        graceful = not options["no_graceful_shutdown"]
        verbosity = options["verbosity"]

        worker_id = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
        if verbosity >= 1:
            self.stdout.write(f"Worker ID: {worker_id}")
            self.stdout.write(f"Backend: {backend_name}")
            if queue_name:
                self.stdout.write(f"Queue: {queue_name}")
            if continuous:
                self.stdout.write(f"Continuous mode: interval={interval}s")
            if max_tasks:
                self.stdout.write(f"Max tasks: {max_tasks}")
            if graceful:
                timeout_label = (
                    f"{shutdown_timeout}s" if shutdown_timeout > 0 else "unlimited"
                )
                self.stdout.write(
                    f"Graceful shutdown: enabled (timeout={timeout_label})"
                )
            else:
                self.stdout.write("Graceful shutdown: disabled")

        self.verbosity = verbosity
        shutdown = GracefulShutdown(
            timeout=shutdown_timeout,
            on_signal=self._report_signal,
            # Signals are reported on stdout by the callback above.
            log_signals=False,
        )

        with ExitStack() as stack:
            if graceful:
                stack.enter_context(shutdown)
            tasks_processed = self._process_tasks(
                shutdown=shutdown,
                backend=task_backends[backend_name],
                queue_name=queue_name,
                backend_name=backend_name,
                worker_id=worker_id,
                continuous=continuous,
                interval=interval,
                max_tasks=max_tasks,
                verbosity=verbosity,
            )

        if shutdown.is_set() and verbosity >= 1:
            self.stdout.write(
                self.style.WARNING("\nShutdown complete (no task was interrupted).")
            )

        if verbosity >= 1:
            self.stdout.write(f"\nTotal tasks processed: {tasks_processed}")

    def _process_tasks(
        self,
        shutdown,
        backend,
        queue_name,
        backend_name,
        worker_id,
        continuous,
        interval,
        max_tasks,
        verbosity=1,
    ):
        tasks_processed = 0

        while not shutdown.is_set():
            task = fetch_task(queue_name=queue_name, backend_name=backend_name)

            if task is None:
                if continuous:
                    if verbosity >= 2:
                        # Idle heartbeat; noisy enough to bury real log
                        # output, so it is opt-in via -v 2.
                        self.stdout.write(".", ending="")
                        self.stdout.flush()
                    # Interruptible sleep: returns as soon as a shutdown
                    # is requested instead of waiting out the interval.
                    if shutdown.wait(interval):
                        break
                    continue
                else:
                    if verbosity >= 1:
                        self.stdout.write("No more tasks to process.")
                    break

            if verbosity >= 1:
                self.stdout.write(f"\nProcessing task: {task.id} ({task.task_path})")

            try:
                result = backend.run_task(task, worker_id=worker_id)
                if result.status == TaskResultStatus.SUCCESSFUL:
                    if verbosity >= 1:
                        self.stdout.write(
                            self.style.SUCCESS("  Task completed successfully")
                        )
                else:
                    self.stdout.write(self.style.ERROR("  Task failed"))
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"  Error running task: {e}"))

            tasks_processed += 1

            if max_tasks and tasks_processed >= max_tasks:
                if verbosity >= 1:
                    self.stdout.write(f"\nReached max tasks limit: {max_tasks}")
                break

        return tasks_processed

    def _report_signal(self, signum, count):
        """Report a received shutdown signal (called from the signal handler)."""
        name = signal_name(signum)
        if count == 1:
            message = (
                f"\nReceived {name}: no new tasks will be started. "
                "Waiting for the running task to finish "
                "(send the signal again to force exit)."
            )
        else:
            message = f"\nReceived {name} again: forcing immediate exit."
        if getattr(self, "verbosity", 1) >= 1:
            self.stdout.write(self.style.WARNING(message))
            self.stdout.flush()
