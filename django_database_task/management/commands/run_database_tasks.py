import socket
import time
import uuid

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q
from django.tasks import task_backends
from django.tasks.base import TaskResultStatus
from django.utils import timezone

from django_database_task.models import DatabaseTask


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

    def handle(self, *args, **options):
        queue_name = options["queue"]
        backend_name = options["backend"]
        continuous = options["continuous"]
        interval = options["interval"]
        max_tasks = options["max_tasks"]

        worker_id = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
        self.stdout.write(f"Worker ID: {worker_id}")
        self.stdout.write(f"Backend: {backend_name}")
        if queue_name:
            self.stdout.write(f"Queue: {queue_name}")
        if continuous:
            self.stdout.write(f"Continuous mode: interval={interval}s")
        if max_tasks:
            self.stdout.write(f"Max tasks: {max_tasks}")

        backend = task_backends[backend_name]
        tasks_processed = 0

        while True:
            task = self._fetch_and_lock_task(queue_name, backend_name)

            if task is None:
                if continuous:
                    self.stdout.write(".", ending="")
                    self.stdout.flush()
                    time.sleep(interval)
                    continue
                else:
                    self.stdout.write("No more tasks to process.")
                    break

            self.stdout.write(f"\nProcessing task: {task.id} ({task.task_path})")

            try:
                result = backend.run_task(task, worker_id=worker_id)
                if result.status == TaskResultStatus.SUCCESSFUL:
                    self.stdout.write(
                        self.style.SUCCESS(f"  Task completed successfully")
                    )
                else:
                    self.stdout.write(self.style.ERROR(f"  Task failed"))
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"  Error running task: {e}"))

            tasks_processed += 1

            if max_tasks and tasks_processed >= max_tasks:
                self.stdout.write(f"\nReached max tasks limit: {max_tasks}")
                break

        self.stdout.write(f"\nTotal tasks processed: {tasks_processed}")

    def _fetch_and_lock_task(self, queue_name, backend_name):
        """Fetch and lock a single pending task with exclusive lock."""
        now = timezone.now()

        with transaction.atomic():
            queryset = DatabaseTask.objects.select_for_update(skip_locked=True).filter(
                status=TaskResultStatus.READY,
                backend_name=backend_name,
            )

            # run_after condition: NULL or before current time
            queryset = queryset.filter(Q(run_after__isnull=True) | Q(run_after__lte=now))

            if queue_name:
                queryset = queryset.filter(queue_name=queue_name)

            # Order by priority descending, enqueued_at ascending
            task = queryset.order_by("-priority", "enqueued_at").first()

            return task
