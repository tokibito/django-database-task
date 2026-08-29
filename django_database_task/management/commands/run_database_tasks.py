import logging
import socket
import sys
import uuid
from contextlib import ExitStack

from django.core.management.base import BaseCommand, CommandError
from django.tasks import task_backends
from django.tasks.base import TaskResultStatus

from django_database_task.backends import task_log_fields
from django_database_task.brokers import PullBroker
from django_database_task.executor import fetch_task, run_task_by_id
from django_database_task.models import DatabaseTask
from django_database_task.shutdown import GracefulShutdown, signal_name

#: Where the worker looks for tasks to run.
SOURCE_AUTO = "auto"
SOURCE_DATABASE = "db"
SOURCE_BROKER = "broker"
SOURCE_BOTH = "both"
SOURCES = [SOURCE_AUTO, SOURCE_DATABASE, SOURCE_BROKER, SOURCE_BOTH]

logger = logging.getLogger("django_database_task")


def _exit_code_argument(value):
    """Parse an exit code option, rejecting what a shell cannot report."""
    try:
        code = int(value)
    except ValueError:
        raise CommandError(f"Exit codes must be whole numbers, not {value!r}") from None
    if not 0 <= code <= 255:
        raise CommandError(f"Exit codes must be between 0 and 255, not {code}")
    return code


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
            "--source",
            choices=SOURCES,
            default=SOURCE_AUTO,
            help=(
                "Where to look for tasks: 'db' polls the database, 'broker' "
                "receives from the backend's broker, 'both' does the broker "
                "first and falls back to the database, and 'auto' (default) "
                "means 'both' when the backend has a broker to receive from "
                "and 'db' otherwise"
            ),
        )
        parser.add_argument(
            "--wait-time",
            type=float,
            default=20.0,
            help=(
                "Seconds to wait for a broker message before looking again. "
                "Replaces --interval as the idle wait when receiving from a "
                "broker (default: 20)"
            ),
        )
        parser.add_argument(
            "--max-messages",
            type=int,
            default=1,
            help=(
                "Maximum number of broker messages to receive at a time (default: 1)"
            ),
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
        parser.add_argument(
            "--empty-exit-code",
            type=_exit_code_argument,
            default=0,
            metavar="CODE",
            help=(
                "Exit with this code when no task was processed, so a job "
                "scheduler can tell an idle run from a real one "
                "(0=exit normally, default: 0)"
            ),
        )
        parser.add_argument(
            "--failed-exit-code",
            type=_exit_code_argument,
            default=0,
            metavar="CODE",
            help=(
                "Exit with this code when at least one task failed or could "
                "not be run. Takes precedence over --empty-exit-code "
                "(0=exit normally, default: 0)"
            ),
        )

    def handle(self, *args, **options):
        queue_name = options["queue"]
        backend_name = options["backend"]
        continuous = options["continuous"]
        interval = options["interval"]
        max_tasks = options["max_tasks"]
        wait_time = options["wait_time"]
        max_messages = options["max_messages"]
        shutdown_timeout = options["shutdown_timeout"]
        graceful = not options["no_graceful_shutdown"]
        empty_exit_code = options["empty_exit_code"]
        failed_exit_code = options["failed_exit_code"]
        verbosity = options["verbosity"]

        if max_messages < 1:
            raise CommandError("--max-messages must be at least 1")

        backend = task_backends[backend_name]
        source = self._resolve_source(backend, options["source"])

        worker_id = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
        if verbosity >= 1:
            self.stdout.write(f"Worker ID: {worker_id}")
            self.stdout.write(f"Backend: {backend_name}")
            self.stdout.write(f"Source: {source}")
            if queue_name:
                self.stdout.write(f"Queue: {queue_name}")
            if continuous:
                self.stdout.write(f"Continuous mode: interval={interval}s")
            if source in (SOURCE_BROKER, SOURCE_BOTH):
                self.stdout.write(
                    f"Broker: {type(backend.broker).__name__} "
                    f"(wait={wait_time}s, max_messages={max_messages})"
                )
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
        # Counted here rather than returned from the loop because a task can
        # fail at several depths (the run itself, the broker message that
        # named it) and every one of them feeds the same exit code.
        self.tasks_failed = 0

        logger.info(
            "Worker started: id=%s backend=%s source=%s",
            worker_id,
            backend_name,
            source,
            extra={
                "worker_id": worker_id,
                "backend_alias": backend_name,
                "source": source,
                "queue_name": queue_name,
                "continuous": continuous,
            },
        )

        shutdown = GracefulShutdown(
            timeout=shutdown_timeout,
            on_signal=self._report_signal,
            # Signals are reported on stdout by the callback above.
            log_signals=False,
        )

        with ExitStack() as stack:
            if graceful:
                stack.enter_context(shutdown)
            if source in (SOURCE_BROKER, SOURCE_BOTH):
                # A broker a worker receives from may hold a connection
                # open, so release it however the loop ends.
                stack.callback(self._close_broker, backend.broker)
            tasks_processed = self._process_tasks(
                shutdown=shutdown,
                backend=backend,
                queue_name=queue_name,
                backend_name=backend_name,
                worker_id=worker_id,
                continuous=continuous,
                interval=interval,
                max_tasks=max_tasks,
                source=source,
                wait_time=wait_time,
                max_messages=max_messages,
                verbosity=verbosity,
            )

        if shutdown.is_set() and verbosity >= 1:
            self.stdout.write(
                self.style.WARNING("\nShutdown complete (no task was interrupted).")
            )

        exit_code = self._exit_code(tasks_processed, empty_exit_code, failed_exit_code)

        if verbosity >= 1:
            self.stdout.write(f"\nTotal tasks processed: {tasks_processed}")
            if self.tasks_failed:
                self.stdout.write(
                    self.style.ERROR(f"Tasks failed: {self.tasks_failed}")
                )

        logger.info(
            "Worker finished: id=%s processed=%d failed=%d",
            worker_id,
            tasks_processed,
            self.tasks_failed,
            extra={
                "worker_id": worker_id,
                "backend_alias": backend_name,
                "queue_name": queue_name,
                "tasks_processed": tasks_processed,
                "tasks_failed": self.tasks_failed,
                "exit_code": exit_code,
            },
        )

        if exit_code:
            sys.exit(exit_code)

    def _exit_code(self, tasks_processed, empty_exit_code, failed_exit_code):
        """
        Work out what to report to whatever started the worker.

        Both codes default to 0, which leaves the run indistinguishable from
        any other successful command -- the behaviour before these options
        existed. A failure wins over an idle run: a broker message the
        worker could not run at all counts as a failure without adding to
        the processed count, so both conditions can hold at once, and the
        failure is the one worth waking someone for.
        """
        if self.tasks_failed and failed_exit_code:
            return failed_exit_code
        if not tasks_processed and empty_exit_code:
            return empty_exit_code
        return 0

    def _resolve_source(self, backend, source):
        """
        Work out where to read tasks from, and check the backend can.

        'auto' picks 'both' when the backend has a broker a worker can
        receive from, so a project that configures one gets a worker for it
        without changing how the command is run.
        """
        broker = getattr(backend, "broker", None)
        has_pull_broker = isinstance(broker, PullBroker)

        if source == SOURCE_AUTO:
            return SOURCE_BOTH if has_pull_broker else SOURCE_DATABASE

        if source in (SOURCE_BROKER, SOURCE_BOTH) and not has_pull_broker:
            raise CommandError(
                f"--source {source} needs a backend whose broker can be "
                f"received from (a PullBroker), but the {type(backend).__name__} "
                f"backend has {type(broker).__name__ if broker else 'none'}. "
                f"Use --source db."
            )

        return source

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
        source=SOURCE_DATABASE,
        wait_time=20.0,
        max_messages=1,
        verbosity=1,
    ):
        use_broker = source in (SOURCE_BROKER, SOURCE_BOTH)
        use_database = source in (SOURCE_DATABASE, SOURCE_BOTH)
        broker = backend.broker if use_broker else None
        tasks_processed = 0

        def remaining():
            """How many more tasks may be run, capped by --max-messages."""
            if not max_tasks:
                return max_messages
            return min(max_messages, max_tasks - tasks_processed)

        while not shutdown.is_set():
            worked = False

            if broker is not None:
                # In 'both' the broker is polled without waiting so the
                # database gets its turn; the waiting happens when both
                # sources turn out to be idle.
                wait = wait_time if source == SOURCE_BROKER else 0
                count = self._receive_and_run(
                    broker, queue_name, worker_id, wait, remaining(), verbosity
                )
                tasks_processed += count
                worked = count > 0
                if self._reached_max_tasks(tasks_processed, max_tasks, verbosity):
                    break
                if shutdown.is_set():
                    break

            if use_database and not worked:
                task = fetch_task(queue_name=queue_name, backend_name=backend_name)
                if task is not None:
                    self._run_database_task(backend, task, worker_id, verbosity)
                    tasks_processed += 1
                    worked = True
                    if self._reached_max_tasks(tasks_processed, max_tasks, verbosity):
                        break

            if worked:
                continue

            if not continuous:
                if verbosity >= 1:
                    self.stdout.write("No more tasks to process.")
                break

            if verbosity >= 2:
                # Idle heartbeat; noisy enough to bury real log output, so
                # it is opt-in via -v 2.
                self.stdout.write(".", ending="")
                self.stdout.flush()

            if source == SOURCE_BOTH and wait_time > 0:
                # The broker's own wait doubles as the idle interval, so a
                # message wakes the worker up straight away.
                tasks_processed += self._receive_and_run(
                    broker, queue_name, worker_id, wait_time, remaining(), verbosity
                )
                if self._reached_max_tasks(tasks_processed, max_tasks, verbosity):
                    break
            elif source == SOURCE_BROKER and wait_time > 0:
                # receive() has already waited.
                continue
            elif shutdown.wait(interval):
                # Interruptible sleep: returns as soon as a shutdown is
                # requested instead of waiting out the interval.
                break

        return tasks_processed

    def _reached_max_tasks(self, tasks_processed, max_tasks, verbosity):
        if not max_tasks or tasks_processed < max_tasks:
            return False
        if verbosity >= 1:
            self.stdout.write(f"\nReached max tasks limit: {max_tasks}")
        return True

    def _run_database_task(self, backend, task, worker_id, verbosity):
        """Run a task fetched straight from the database."""
        if verbosity >= 1:
            self.stdout.write(f"\nProcessing task: {task.id} ({task.task_path})")

        try:
            result = backend.run_task(task, worker_id=worker_id)
        except Exception as e:
            logger.exception(
                "Worker could not run task: id=%s path=%s",
                task.id,
                task.task_path,
                extra=task_log_fields(task, worker_id),
            )
            self.stdout.write(self.style.ERROR(f"  Error running task: {e}"))
            self.tasks_failed += 1
            return

        if result.status != TaskResultStatus.SUCCESSFUL:
            self.tasks_failed += 1
        self._report_result(result.status, verbosity)

    def _receive_and_run(
        self, broker, queue_name, worker_id, wait_seconds, max_messages, verbosity
    ):
        """Receive messages from the broker and run the tasks they name."""
        if max_messages < 1:
            return 0

        try:
            messages = broker.receive(
                queue_name=queue_name,
                max_messages=max_messages,
                wait_seconds=wait_seconds,
            )
        except Exception as e:
            logger.exception(
                "Error receiving from broker %s",
                type(broker).__name__,
                extra={
                    "worker_id": worker_id,
                    "queue_name": queue_name,
                    "broker": type(broker).__name__,
                },
            )
            self.stdout.write(self.style.ERROR(f"\nError receiving from broker: {e}"))
            return 0

        return sum(
            1
            for message in messages or []
            if self._run_broker_message(broker, message, worker_id, verbosity)
        )

    def _run_broker_message(self, broker, message, worker_id, verbosity):
        """
        Run the task a broker message names.

        The message is acknowledged whenever redelivering it would not
        help: the task ran, it is already gone, or another worker has it.
        Anything else leaves the message for the broker to deliver again.
        """
        if verbosity >= 1:
            self.stdout.write(f"\nProcessing task from broker: {message.task_id}")

        try:
            result = run_task_by_id(message.task_id, worker_id=worker_id)
        except DatabaseTask.DoesNotExist:
            if verbosity >= 1:
                self.stdout.write(
                    self.style.WARNING("  Task no longer exists; dropping message")
                )
            self._ack(broker, message)
            return False
        except Exception as e:
            # The task itself is recorded as failed by run_task(); getting
            # here means this worker could not run it at all.
            logger.exception(
                "Worker could not run task from broker: id=%s",
                message.task_id,
                extra={"worker_id": worker_id, "task_id": str(message.task_id)},
            )
            self.stdout.write(self.style.ERROR(f"  Error running task: {e}"))
            self.tasks_failed += 1
            self._nack(broker, message)
            return False

        self._ack(broker, message)

        if result is None:
            if verbosity >= 1:
                self.stdout.write("  Task is not ready to run; nothing to do")
            return False

        if result.status != TaskResultStatus.SUCCESSFUL:
            self.tasks_failed += 1
        self._report_result(result.status, verbosity)
        return True

    def _report_result(self, status, verbosity):
        if status == TaskResultStatus.SUCCESSFUL:
            if verbosity >= 1:
                self.stdout.write(self.style.SUCCESS("  Task completed successfully"))
        else:
            self.stdout.write(self.style.ERROR("  Task failed"))

    def _close_broker(self, broker):
        try:
            broker.close()
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"\nError closing the broker: {e}"))

    def _ack(self, broker, message):
        try:
            broker.ack(message)
        except Exception as e:
            # The message comes back later; the task is guarded against
            # running twice by its status and the row lock.
            self.stdout.write(
                self.style.ERROR(f"  Error acknowledging broker message: {e}")
            )

    def _nack(self, broker, message):
        try:
            broker.nack(message)
        except Exception as e:
            self.stdout.write(
                self.style.ERROR(f"  Error returning broker message: {e}")
            )

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
