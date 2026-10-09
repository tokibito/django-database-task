import argparse
import functools
import logging
import socket
import sys
import threading
import uuid
from contextlib import ExitStack

from django.core.management.base import BaseCommand, CommandError
from django.db import connections, router
from django.tasks import task_backends
from django.tasks.base import TaskResultStatus
from django.utils.translation import gettext as _

from django_database_task.backends import task_log_fields
from django_database_task.brokers import PullBroker
from django_database_task.executor import fetch_task, run_task_by_id
from django_database_task.models import DatabaseTask
from django_database_task.shutdown import GracefulShutdown, signal_name
from django_database_task.supervisor import (
    WorkerSupervisor,
    read_worker_index,
    set_worker_index,
    worker_arguments,
    worker_log_fields,
)
from django_database_task.threads import ExecutorThreads

#: Where the worker looks for tasks to run.
SOURCE_AUTO = "auto"
SOURCE_DATABASE = "db"
SOURCE_BROKER = "broker"
SOURCE_BOTH = "both"
SOURCES = [SOURCE_AUTO, SOURCE_DATABASE, SOURCE_BROKER, SOURCE_BOTH]

logger = logging.getLogger("django_database_task")

#: How long the main thread waits for an executor thread to report back
#: before looking at the shutdown flag again. A result wakes it at once;
#: this only bounds the wait.
COLLECT_WAIT = 1.0


def _exit_code_argument(value):
    """Parse an exit code option, rejecting what a shell cannot report."""
    try:
        code = int(value)
    except ValueError:
        # ArgumentTypeError is what argparse turns into a parser error
        # (exit code 2); a CommandError would escape run_from_argv()
        # uncaught as a traceback.
        raise argparse.ArgumentTypeError(
            f"Exit codes must be whole numbers, not {value!r}"
        ) from None
    if not 0 <= code <= 255:
        raise argparse.ArgumentTypeError(
            f"Exit codes must be between 0 and 255, not {code}"
        )
    return code


class Command(BaseCommand):
    @property
    def help(self):
        # argparse formats the description and the option help with a regex,
        # which a lazy string cannot go through, so every string here is
        # translated when the parser is built rather than at import time.
        return _("Execute tasks queued in the database")

    def add_arguments(self, parser):
        parser.add_argument(
            "--queue",
            type=str,
            default=None,
            help=_("Queue name to process (all queues if not specified)"),
        )
        parser.add_argument(
            "--backend",
            type=str,
            default="default",
            help=_("Backend name (default: default)"),
        )
        parser.add_argument(
            "--continuous",
            action="store_true",
            help=_("Continuous mode (keep polling even when no tasks)"),
        )
        parser.add_argument(
            "--interval",
            type=float,
            default=5.0,
            help=_("Polling interval in seconds for continuous mode (default: 5)"),
        )
        parser.add_argument(
            "--max-tasks",
            type=int,
            default=0,
            help=_("Maximum number of tasks to process (0=unlimited, default: 0)"),
        )
        parser.add_argument(
            "--source",
            choices=SOURCES,
            default=SOURCE_AUTO,
            help=_(
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
            help=_(
                "Seconds to wait for a broker message before looking again. "
                "Replaces --interval as the idle wait when receiving from a "
                "broker (default: 20)"
            ),
        )
        parser.add_argument(
            "--max-messages",
            type=int,
            default=1,
            help=_(
                "Maximum number of broker messages to receive at a time (default: 1)"
            ),
        )
        parser.add_argument(
            "--workers",
            type=int,
            default=1,
            metavar="N",
            help=_(
                "Run this many worker processes from one command, each a "
                "copy of this command without --workers. With --continuous "
                "a worker that exits is replaced; without it the command "
                "exits when every worker has (default: 1)"
            ),
        )
        parser.add_argument(
            "--threads",
            type=int,
            default=1,
            metavar="M",
            help=_(
                "Run tasks in this many threads inside the worker. The task "
                "code must be thread-safe. The worker still polls once per "
                "interval; each thread has a database connection of its own "
                "(default: 1)"
            ),
        )
        parser.add_argument(
            "--shutdown-timeout",
            type=float,
            default=0.0,
            help=_(
                "Maximum seconds to wait for the running task after receiving "
                "SIGTERM/SIGINT before forcing exit "
                "(0=wait indefinitely, default: 0)"
            ),
        )
        parser.add_argument(
            "--no-graceful-shutdown",
            action="store_true",
            help=_(
                "Do not install SIGTERM/SIGINT handlers; the process is "
                "terminated immediately, even while a task is running"
            ),
        )
        parser.add_argument(
            "--empty-exit-code",
            type=_exit_code_argument,
            default=0,
            metavar="CODE",
            help=_(
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
            help=_(
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
        if options["workers"] < 1:
            raise CommandError("--workers must be at least 1")
        threads = options["threads"]
        if threads < 1:
            raise CommandError("--threads must be at least 1")

        backend = task_backends[backend_name]
        source = self._resolve_source(backend, options["source"])

        if options["workers"] > 1:
            # The options are checked above so that a mistake is reported
            # once, here, rather than by every worker.
            self._supervise(options)
            return

        worker_id = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
        worker_index = self._read_worker_index()
        if verbosity >= 1:
            self.stdout.write(f"Worker ID: {worker_id}")
            if worker_index is not None:
                self.stdout.write(f"Worker index: {worker_index}")
            self.stdout.write(f"Backend: {backend_name}")
            self.stdout.write(f"Source: {source}")
            if threads > 1:
                self.stdout.write(f"Threads: {threads}")
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
        # named it) and every one of them feeds the same exit code. With
        # --threads it is counted from several threads, hence the lock.
        self.tasks_failed = 0
        self._failures_lock = threading.Lock()

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
                "threads": threads,
                **worker_log_fields(),
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
            executors = None
            if threads > 1:
                self._check_database_supports_concurrency("--threads", threads)
                executors = ExecutorThreads(threads, worker_id).start()
                stack.callback(executors.stop)
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
                executors=executors,
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
                **worker_log_fields(),
            },
        )

        if exit_code:
            sys.exit(exit_code)

    def _read_worker_index(self):
        """
        Read the worker index once, for every record this worker writes.

        A value that is not an index is reported and ignored rather than
        refused: it only labels the log records, and a worker that will
        not start over a label is worse than one that runs without it.
        """
        try:
            index = read_worker_index()
        except ValueError as e:
            index = None
            logger.warning("Ignoring the worker index: %s", e)
            self.stdout.write(self.style.WARNING(f"Ignoring the worker index: {e}"))
        set_worker_index(index)
        return index

    def _supervise(self, options):
        """
        Run ``--workers`` copies of this command and wait for them.

        Each worker is this command run again without ``--workers``, so
        every other option reaches it unchanged, and it is exactly the
        single-process worker: its own worker id, its own database
        connection, its own ``Worker started`` and ``Worker finished``
        records.
        """
        workers = options["workers"]
        verbosity = options["verbosity"]

        self._check_database_supports_concurrency("--workers", workers)

        supervisor = WorkerSupervisor(
            worker_arguments(),
            workers,
            restart=options["continuous"],
            graceful=not options["no_graceful_shutdown"],
            shutdown_timeout=options["shutdown_timeout"],
            empty_exit_code=options["empty_exit_code"],
            failed_exit_code=options["failed_exit_code"],
            stdout=self.stdout,
            style=self.style,
            verbosity=verbosity,
        )
        exit_code = supervisor.run()
        if exit_code:
            sys.exit(exit_code)

    def _check_database_supports_concurrency(self, option, value):
        """
        Refuse several workers, or several threads, on SQLite.

        Nothing runs twice there, since a task is claimed with a conditional
        UPDATE, but SQLite has no row locks: two claims at once both hold the
        read lock and one is refused the write with "database is locked",
        which the worker counts as a failed task. Threads claim from one
        thread when polling the database, but not for broker messages, and
        their finishing writes collide the same way. The README says SQLite
        is for development and single-worker deployments; this is where
        that stops being a note.
        """
        alias = router.db_for_write(DatabaseTask)
        if connections[alias].vendor == "sqlite":
            raise CommandError(
                f"{option} {value} needs a database with row locks, and the "
                f"{alias!r} database is SQLite. Run one worker with one "
                "thread, or use PostgreSQL, MySQL or MariaDB."
            )

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
        executors=None,
    ):
        """
        The worker loop: find tasks and run them until there is reason to stop.

        Without ``executors`` every task is run right here, in the thread
        that found it. With them (``--threads``) this thread only fetches,
        receives and claims, and hands each task to an idle executor; the
        executors report back what they ran, and that is added up here.
        """
        use_broker = source in (SOURCE_BROKER, SOURCE_BOTH)
        use_database = source in (SOURCE_DATABASE, SOURCE_BOTH)
        broker = backend.broker if use_broker else None
        tasks_processed = 0
        exhausted = False

        def in_flight():
            return executors.busy if executors is not None else 0

        def free_slots():
            return executors.idle_count() if executors is not None else 1

        def collect(wait=False):
            """Add up what the executors have finished since last time."""
            nonlocal tasks_processed
            if executors is None:
                return
            outcomes = executors.collect(timeout=COLLECT_WAIT if wait else None)
            tasks_processed += sum(1 for processed in outcomes if processed)

        def remaining():
            """How many more tasks may be started right now."""
            limit = max_messages
            if executors is not None:
                limit = min(limit, free_slots())
            if not max_tasks:
                return limit
            return min(limit, max_tasks - tasks_processed - in_flight())

        def reached_max():
            # Tasks handed to an executor count towards the cap, so no more
            # than --max-tasks are ever started. One that turns out not to
            # run (a broker message for a task that is gone) can leave the
            # run short of the cap by that many.
            return self._reached_max_tasks(
                tasks_processed + in_flight(), max_tasks, verbosity
            )

        while not shutdown.is_set():
            collect()
            if executors is not None and free_slots() == 0:
                # Every executor is busy: wait for one to come back, not
                # for the poll interval.
                collect(wait=True)
                continue

            worked = False

            if broker is not None:
                # In 'both' the broker is polled without waiting so the
                # database gets its turn; the waiting happens when both
                # sources turn out to be idle.
                wait = wait_time if source == SOURCE_BROKER else 0
                count = self._receive_and_run(
                    broker,
                    backend_name,
                    queue_name,
                    worker_id,
                    wait,
                    remaining(),
                    verbosity,
                    executors,
                )
                if executors is None:
                    tasks_processed += count
                worked = count > 0
                if reached_max():
                    break
                if shutdown.is_set():
                    break

            if use_database and not worked:
                task = fetch_task(queue_name=queue_name, backend_name=backend_name)
                if task is not None:
                    worked = True
                    if executors is None:
                        counted = self._run_database_task(
                            backend, task, worker_id, verbosity
                        )
                    else:
                        counted = self._dispatch_database_task(
                            executors, backend, task, verbosity
                        )
                    if counted:
                        tasks_processed += 1
                    if reached_max():
                        break

            if worked:
                continue

            if not continuous:
                exhausted = True
                break

            if verbosity >= 2:
                # Idle heartbeat; noisy enough to bury real log output, so
                # it is opt-in via -v 2.
                self.stdout.write(".", ending="")
                self.stdout.flush()

            if source == SOURCE_BOTH and wait_time > 0:
                # The broker's own wait doubles as the idle interval, so a
                # message wakes the worker up straight away.
                count = self._receive_and_run(
                    broker,
                    backend_name,
                    queue_name,
                    worker_id,
                    wait_time,
                    remaining(),
                    verbosity,
                    executors,
                )
                if executors is None:
                    tasks_processed += count
                if reached_max():
                    break
            elif source == SOURCE_BROKER and wait_time > 0:
                # receive() has already waited.
                continue
            elif shutdown.wait(interval):
                # Interruptible sleep: returns as soon as a shutdown is
                # requested instead of waiting out the interval.
                break

        while in_flight():
            # The tasks already handed out finish first. A shutdown that
            # runs past --shutdown-timeout ends the process from the
            # GracefulShutdown timer, as it does for a task run inline.
            collect(wait=True)

        if exhausted and verbosity >= 1:
            # Reported after the executors are done, so it reads in order.
            self.stdout.write("No more tasks to process.")

        return tasks_processed

    def _reached_max_tasks(self, tasks_processed, max_tasks, verbosity):
        if not max_tasks or tasks_processed < max_tasks:
            return False
        if verbosity >= 1:
            self.stdout.write(f"\nReached max tasks limit: {max_tasks}")
        return True

    def _run_database_task(self, backend, task, worker_id, verbosity):
        """
        Run a task fetched straight from the database, here and now.

        Returns:
            True if the task was run here, False if another worker claimed
            it between the fetch and the run and so it was not.
        """
        if verbosity >= 1:
            self.stdout.write(f"\nProcessing task: {task.id} ({task.task_path})")

        try:
            result = backend.run_task(task, worker_id=worker_id)
        except Exception as e:
            self._report_run_error(task, worker_id, e)
            return True

        if result is None:
            if verbosity >= 1:
                self.stdout.write("  Task is not ready to run; nothing to do")
            return False

        self._record_result(result, verbosity)
        return True

    def _dispatch_database_task(self, executors, backend, task, verbosity):
        """
        Claim a task fetched from the database and hand it to an executor.

        The claim happens here, in the fetching thread, so that the next
        fetch does not return the same READY row; it is made with the
        executor's worker id, which is what the task records. The executor
        reports the run back through the loop's ``collect()``.

        Returns:
            True if the task is to be counted now (the claim itself failed,
            which is a failed task that reached no executor), False if it
            was handed out or was no longer READY.
        """
        index = executors.acquire()
        thread_worker_id = executors.worker_ids[index]
        if verbosity >= 1:
            self.stdout.write(f"\nProcessing task: {task.id} ({task.task_path})")

        try:
            claimed = backend.claim_task(task, worker_id=thread_worker_id)
        except Exception as e:
            executors.release(index)
            self._report_run_error(task, thread_worker_id, e)
            return True

        if not claimed:
            executors.release(index)
            if verbosity >= 1:
                self.stdout.write("  Task is not ready to run; nothing to do")
            return False

        executors.submit(
            index,
            functools.partial(
                self._run_claimed_task, backend, task, thread_worker_id, verbosity
            ),
        )
        return False

    def _run_claimed_task(self, backend, task, worker_id, verbosity):
        """Run a task :meth:`_dispatch_database_task` claimed, in an executor."""
        try:
            result = backend.run_claimed_task(task, worker_id=worker_id)
        except Exception as e:
            self._report_run_error(task, worker_id, e)
            return True
        self._record_result(result, verbosity)
        return True

    def _report_run_error(self, task, worker_id, error):
        """Record that this worker could not run ``task`` at all."""
        logger.exception(
            "Worker could not run task: id=%s path=%s",
            task.id,
            task.task_path,
            extra=task_log_fields(task, worker_id),
        )
        self.stdout.write(self.style.ERROR(f"  Error running task: {error}"))
        self._count_failure()

    def _record_result(self, result, verbosity):
        if result.status != TaskResultStatus.SUCCESSFUL:
            self._count_failure()
        self._report_result(result.status, verbosity)

    def _count_failure(self):
        with self._failures_lock:
            self.tasks_failed += 1

    def _receive_and_run(
        self,
        broker,
        backend_name,
        queue_name,
        worker_id,
        wait_seconds,
        max_messages,
        verbosity,
        executors=None,
    ):
        """
        Receive messages from the broker and run the tasks they name.

        Returns:
            Without executors, how many tasks were run. With them, how many
            messages were handed out; the executors report the runs back
            through the loop's ``collect()``.
        """
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
                    "backend_alias": backend_name,
                    "queue_name": queue_name,
                    "broker": type(broker).__name__,
                    **worker_log_fields(),
                },
            )
            self.stdout.write(self.style.ERROR(f"\nError receiving from broker: {e}"))
            return 0

        messages = list(messages or [])
        if executors is None:
            return sum(
                1
                for message in messages
                if self._run_broker_message(broker, message, worker_id, verbosity)
            )

        for message in messages:
            # remaining() capped the receive by the idle executors, so one
            # is free for each message.
            index = executors.acquire()
            executors.submit(
                index,
                functools.partial(
                    self._run_broker_message,
                    broker,
                    message,
                    executors.worker_ids[index],
                    verbosity,
                ),
            )
        return len(messages)

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
                extra=self._broker_task_log_fields(message, worker_id),
            )
            self.stdout.write(self.style.ERROR(f"  Error running task: {e}"))
            self._count_failure()
            self._nack(broker, message)
            return False

        self._ack(broker, message)

        if result is None:
            if verbosity >= 1:
                self.stdout.write("  Task is not ready to run; nothing to do")
            return False

        self._record_result(result, verbosity)
        return True

    def _broker_task_log_fields(self, message, worker_id):
        """
        Build the log fields for the task a broker message names.

        run_task_by_id() raised before handing the row back, so it is read
        again here. When that read fails as well -- the database is often
        what went wrong -- the record keeps the id the message carried
        rather than losing the original error to a second one.
        """
        try:
            db_task = DatabaseTask.objects.filter(id=message.task_id).first()
        except Exception:
            db_task = None
        if db_task is None:
            return {
                "worker_id": worker_id,
                "task_id": str(message.task_id),
                **worker_log_fields(),
            }
        return task_log_fields(db_task, worker_id)

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
