from datetime import timedelta

from django.core.management.base import BaseCommand
from django.tasks.base import TaskResultStatus
from django.utils import timezone

from django_database_task.models import DatabaseTask


class Command(BaseCommand):
    help = "Delete completed task records from the database"

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=0,
            help="Delete tasks completed more than N days ago (0=all, default: 0)",
        )
        parser.add_argument(
            "--status",
            type=str,
            default="SUCCESSFUL,FAILED",
            help="Target statuses, comma-separated (default: SUCCESSFUL,FAILED)",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=1000,
            help="Number of tasks to delete at once (default: 1000)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show count only without deleting",
        )

    def handle(self, *args, **options):
        days = options["days"]
        status_str = options["status"]
        batch_size = options["batch_size"]
        dry_run = options["dry_run"]

        # Parse statuses
        statuses = [s.strip().upper() for s in status_str.split(",")]
        valid_statuses = [TaskResultStatus.SUCCESSFUL, TaskResultStatus.FAILED]
        statuses = [s for s in statuses if s in [v.value for v in valid_statuses]]

        if not statuses:
            self.stdout.write(self.style.ERROR("No valid statuses specified"))
            return

        self.stdout.write(f"Target statuses: {', '.join(statuses)}")

        # Build query
        queryset = DatabaseTask.objects.filter(status__in=statuses)

        if days > 0:
            cutoff_date = timezone.now() - timedelta(days=days)
            queryset = queryset.filter(finished_at__lt=cutoff_date)
            self.stdout.write(f"Cutoff date: {cutoff_date}")

        total_count = queryset.count()
        self.stdout.write(f"Found {total_count} tasks to delete")

        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run mode - no tasks deleted"))
            return

        if total_count == 0:
            self.stdout.write("No tasks to delete")
            return

        # Batch delete
        deleted_total = 0
        while True:
            # Get batch of IDs and delete
            task_ids = list(queryset.values_list("id", flat=True)[:batch_size])
            if not task_ids:
                break

            deleted_count, _ = DatabaseTask.objects.filter(id__in=task_ids).delete()
            deleted_total += deleted_count
            self.stdout.write(f"Deleted {deleted_total}/{total_count} tasks...")

        self.stdout.write(
            self.style.SUCCESS(f"Successfully deleted {deleted_total} tasks")
        )
