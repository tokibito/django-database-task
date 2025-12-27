import socket
import time
import uuid

from django.core.management.base import BaseCommand
from django.tasks import task_backends
from django.tasks.base import TaskResultStatus

from django_database_task.executor import fetch_task


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
            task = fetch_task(queue_name=queue_name, backend_name=backend_name)

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
                        self.style.SUCCESS("  Task completed successfully")
                    )
                else:
                    self.stdout.write(self.style.ERROR("  Task failed"))
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"  Error running task: {e}"))

            tasks_processed += 1

            if max_tasks and tasks_processed >= max_tasks:
                self.stdout.write(f"\nReached max tasks limit: {max_tasks}")
                break

        self.stdout.write(f"\nTotal tasks processed: {tasks_processed}")
