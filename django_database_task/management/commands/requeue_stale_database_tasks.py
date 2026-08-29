import re
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError

from django_database_task.executor import DEFAULT_MAX_ATTEMPTS, requeue_stale_tasks

#: Units accepted by --older-than, as seconds.
DURATION_UNITS = {
    "s": 1,
    "m": 60,
    "h": 60 * 60,
    "d": 24 * 60 * 60,
}

DURATION_RE = re.compile(r"^(\d+)([smhd])$")


def parse_older_than(value):
    """
    Parse a --older-than value such as "15m" into a timedelta.

    The unit is required: a bare number reads as minutes to one person and
    seconds to the next, and getting it wrong here means requeueing tasks
    that are still running.

    Raises:
        CommandError: If the value is not a positive number with a unit.
    """
    match = DURATION_RE.match(value.strip())
    if match is None:
        raise CommandError(
            f"Could not read --older-than {value!r}. Give a whole number "
            f"followed by a unit: s (seconds), m (minutes), h (hours) or "
            f"d (days), for example 15m or 2h."
        )

    amount, unit = match.groups()
    seconds = int(amount) * DURATION_UNITS[unit]
    if seconds <= 0:
        raise CommandError("--older-than must be greater than zero.")

    return timedelta(seconds=seconds)


class Command(BaseCommand):
    help = (
        "Recover tasks left in RUNNING status by a worker that was killed "
        "before it could write a result"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--older-than",
            type=str,
            required=True,
            help=(
                "Only touch tasks that have been RUNNING for longer than this, "
                "as a number and a unit: 90s, 15m, 2h, 1d. Required, and it "
                "must be longer than your longest running task - a task still "
                "running when its threshold passes is requeued and ends up "
                "running twice"
            ),
        )
        parser.add_argument(
            "--queue",
            type=str,
            default=None,
            help="Queue name to recover (all queues if not specified)",
        )
        parser.add_argument(
            "--backend",
            type=str,
            default=None,
            help="Backend name to recover (all backends if not specified)",
        )
        parser.add_argument(
            "--max-attempts",
            type=int,
            default=DEFAULT_MAX_ATTEMPTS,
            help=(
                "Mark a task FAILED instead of requeueing it once it has been "
                f"handed to this many workers (0=no limit, default: "
                f"{DEFAULT_MAX_ATTEMPTS}). Stops a task that kills its worker "
                "from being requeued forever"
            ),
        )
        parser.add_argument(
            "--mark-failed",
            action="store_true",
            help=(
                "Mark every stale task FAILED instead of requeueing it. For "
                "tasks that are not safe to run twice"
            ),
        )
        parser.add_argument(
            "--notify-broker",
            action="store_true",
            help=(
                "Tell the backend's broker about each requeued task. Needed "
                "when workers only receive from a broker, because the message "
                "for the killed attempt is already gone"
            ),
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=1000,
            help="Number of tasks to process at once (default: 1000)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would happen without changing anything",
        )

    def handle(self, *args, **options):
        older_than = parse_older_than(options["older_than"])
        queue_name = options["queue"]
        backend_name = options["backend"]
        max_attempts = options["max_attempts"]
        mark_failed = options["mark_failed"]
        notify_broker = options["notify_broker"]
        batch_size = options["batch_size"]
        dry_run = options["dry_run"]
        verbosity = options["verbosity"]

        if max_attempts < 0:
            raise CommandError("--max-attempts cannot be negative")
        if batch_size < 1:
            raise CommandError("--batch-size must be at least 1")

        if verbosity >= 1:
            self.stdout.write(f"Stale after: {options['older_than']}")
            if queue_name:
                self.stdout.write(f"Queue: {queue_name}")
            if backend_name:
                self.stdout.write(f"Backend: {backend_name}")
            if mark_failed:
                self.stdout.write("Mode: mark failed (no task is requeued)")
            else:
                limit = max_attempts if max_attempts else "no limit"
                self.stdout.write(f"Mode: requeue (max attempts: {limit})")

        summary = requeue_stale_tasks(
            older_than=older_than,
            queue_name=queue_name,
            backend_name=backend_name,
            max_attempts=max_attempts,
            mark_failed=mark_failed,
            notify_broker=notify_broker,
            batch_size=batch_size,
            dry_run=dry_run,
        )

        if verbosity >= 1:
            self.stdout.write(f"Found {summary['found']} stale tasks")

        if dry_run:
            if verbosity >= 1:
                self.stdout.write(
                    self.style.WARNING(
                        f"Dry run mode - nothing was changed "
                        f"({summary['requeued']} would be requeued, "
                        f"{summary['failed']} would be marked failed)"
                    )
                )
            return

        if verbosity >= 1:
            if summary["found"] == 0:
                self.stdout.write("No stale tasks to recover")
                return

            self.stdout.write(
                self.style.SUCCESS(
                    f"Requeued {summary['requeued']} tasks, "
                    f"marked {summary['failed']} as failed"
                )
            )
