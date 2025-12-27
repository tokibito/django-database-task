# Django Database Task Demo Project

A demo Django project for testing `django_database_task`.

## Setup

```bash
cd examples

# Run migrations
../venv/bin/python manage.py migrate
```

## Usage

### 1. Start Web Server

```bash
../venv/bin/python manage.py runserver
```

Open http://localhost:8000/ in your browser.

### 2. Start Worker (in another terminal)

```bash
cd examples

# Run once (exit when no tasks remain)
../venv/bin/python manage.py run_database_tasks

# Continuous mode (poll every 5 seconds)
../venv/bin/python manage.py run_database_tasks --continuous --interval 5

# Process up to 10 tasks
../venv/bin/python manage.py run_database_tasks --max-tasks 10

# Run specific queue only
../venv/bin/python manage.py run_database_tasks --queue emails --continuous
```

## Demo Scenarios

### Basic Task Execution

1. Enqueue "Add Numbers" task from the web form
2. Start the worker
3. Check the result on the result page

### Priority Test

1. Click "Priority Test" button (enqueues 3 low priority + 1 high priority tasks)
2. Start the worker
3. Verify high priority task runs first

### Delayed Execution

1. Enqueue "Delayed Task" with 30 second delay
2. Start the worker in continuous mode
3. Verify task runs after 30 seconds

### Error Handling

1. Enqueue "Failing Task"
2. Start the worker
3. Verify status is FAILED with error information recorded

## Purge Completed Tasks

```bash
# Delete all completed tasks
../venv/bin/python manage.py purge_completed_database_tasks

# Delete tasks completed more than 7 days ago
../venv/bin/python manage.py purge_completed_database_tasks --days 7

# Delete only successful tasks
../venv/bin/python manage.py purge_completed_database_tasks --status SUCCESSFUL

# Dry run (show count without deleting)
../venv/bin/python manage.py purge_completed_database_tasks --dry-run
```

## Shell Operations

```bash
../venv/bin/python manage.py shell
```

```python
# Enqueue a task
from demo_app.tasks import add_numbers, send_email_task
result = add_numbers.enqueue(10, 20)
print(f"Task ID: {result.id}")

# Delayed execution
from datetime import timedelta
from django.utils import timezone
delayed = add_numbers.using(run_after=timezone.now() + timedelta(minutes=5))
result = delayed.enqueue(100, 200)

# List all tasks
from django_database_task.models import DatabaseTask
DatabaseTask.objects.all()

# Get result
from django.tasks import task_backends
backend = task_backends["default"]
result = backend.get_result("task-id-here")
print(result.status, result.return_value)
```
