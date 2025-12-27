# django-database-task

A database-backed task queue backend for Django 6.0's built-in task framework.

## Features

- **No external dependencies** - Uses your existing database, no Redis or message broker required
- **Priority support** - Tasks can have priorities from -100 to 100
- **Delayed execution** - Schedule tasks to run at a specific time with `run_after`
- **Exclusive locking** - Prevents duplicate task execution with `SELECT FOR UPDATE SKIP LOCKED`
- **Django Admin integration** - View and manage tasks from the admin interface
- **Async support** - Supports async task functions

## Requirements

- Python 3.12+
- Django 6.0+
- PostgreSQL 9.5+ or MySQL 8.0+ (for `SELECT FOR UPDATE SKIP LOCKED` support)
  - SQLite works for development but doesn't support row-level locking

## Installation

```bash
pip install django-database-task
```

## Quick Start

### 1. Add to INSTALLED_APPS

```python
INSTALLED_APPS = [
    # ...
    'django_database_task',
]
```

### 2. Configure the task backend

```python
TASKS = {
    'default': {
        'BACKEND': 'django_database_task.backends.DatabaseTaskBackend',
        'QUEUES': [],  # Empty list means all queues
        'OPTIONS': {},
    },
}
```

### 3. Run migrations

```bash
python manage.py migrate django_database_task
```

### 4. Define a task

```python
from django.tasks import task

@task()
def send_welcome_email(user_id):
    user = User.objects.get(id=user_id)
    # Send email...
    return f"Email sent to {user.email}"
```

### 5. Enqueue the task

```python
result = send_welcome_email.enqueue(user_id=123)
print(f"Task ID: {result.id}")
```

### 6. Run the worker

```bash
# Run once (exit when no tasks)
python manage.py run_database_tasks

# Run continuously (poll every 5 seconds)
python manage.py run_database_tasks --continuous --interval 5
```

## Usage

### Task with priority

```python
@task(priority=10)  # Higher priority, runs first
def urgent_task():
    pass

@task(priority=-10)  # Lower priority
def background_task():
    pass
```

### Delayed execution

```python
from datetime import timedelta
from django.utils import timezone

# Run 1 hour from now
delayed_task = my_task.using(run_after=timezone.now() + timedelta(hours=1))
result = delayed_task.enqueue()
```

### Task with context

```python
@task(takes_context=True)
def task_with_context(context, message):
    task_id = context.task_result.id
    attempt = context.attempt
    return f"Task {task_id} (attempt {attempt}): {message}"
```

### Queue-specific tasks

```python
@task(queue_name="emails")
def send_newsletter():
    pass

# Run worker for specific queue
# python manage.py run_database_tasks --queue emails
```

## Management Commands

### run_database_tasks

Execute tasks queued in the database.

```bash
python manage.py run_database_tasks [options]
```

| Option | Description |
|--------|-------------|
| `--queue` | Queue name to process (all queues if not specified) |
| `--backend` | Backend name (default: "default") |
| `--continuous` | Keep polling even when no tasks |
| `--interval` | Polling interval in seconds (default: 5) |
| `--max-tasks` | Maximum number of tasks to process (0=unlimited) |

### purge_completed_database_tasks

Delete completed task records from the database.

```bash
python manage.py purge_completed_database_tasks [options]
```

| Option | Description |
|--------|-------------|
| `--days` | Delete tasks completed more than N days ago (0=all) |
| `--status` | Target statuses, comma-separated (default: "SUCCESSFUL,FAILED") |
| `--batch-size` | Number of tasks to delete at once (default: 1000) |
| `--dry-run` | Show count only without deleting |

## Django Admin

The package includes a Django Admin integration to view task status:

- Task list with status badges
- Filter by status, queue, backend
- Search by task ID or path
- View task arguments and results

## License

MIT License - see [LICENSE](LICENSE) for details.
