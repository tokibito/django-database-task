# django-database-task

[![CI](https://github.com/tokibito/django-database-task/actions/workflows/ci.yml/badge.svg)](https://github.com/tokibito/django-database-task/actions/workflows/ci.yml)
[![PyPI version](https://badge.fury.io/py/django-database-task.svg)](https://badge.fury.io/py/django-database-task)
[![Python versions](https://img.shields.io/pypi/pyversions/django-database-task.svg)](https://pypi.org/project/django-database-task/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A database-backed task queue backend for Django 6.0's built-in task framework.

## Features

- **No external dependencies** - Uses your existing database, no Redis or message broker required
- **Priority support** - Tasks can have priorities from -100 to 100
- **Delayed execution** - Schedule tasks to run at a specific time with `run_after`
- **Exclusive locking** - Prevents duplicate task execution with `SELECT FOR UPDATE SKIP LOCKED`
- **Django Admin integration** - View and manage tasks from the admin interface
- **Async support** - Supports async task functions
- **Google Cloud Tasks integration** - Optional backend for GAE/Cloud Run with auto-detection

## Architecture

```mermaid
sequenceDiagram
    participant App as Application
    participant Backend as DatabaseTaskBackend
    participant DB as Database
    participant Worker as Worker Process

    Note over App,Worker: Task Enqueue
    App->>Backend: task.enqueue(args, kwargs)
    Backend->>Backend: Validate & serialize args
    Backend->>DB: INSERT task (status=READY)
    DB-->>Backend: Task ID
    Backend-->>App: TaskResult (id, status=READY)

    Note over App,Worker: Task Execution
    Worker->>DB: SELECT FOR UPDATE SKIP LOCKED<br/>(status=READY, run_after <= now)
    DB-->>Worker: Task record (with lock)
    Worker->>DB: UPDATE status=RUNNING
    Worker->>Worker: Execute task function
    alt Success
        Worker->>DB: UPDATE status=SUCCESSFUL,<br/>return_value, finished_at
    else Failure
        Worker->>DB: UPDATE status=FAILED,<br/>errors, finished_at
    end

    Note over App,Worker: Result Retrieval (Optional)
    App->>Backend: backend.get_result(task_id)
    Backend->>DB: SELECT task
    DB-->>Backend: Task record
    Backend-->>App: TaskResult (status, return_value, errors)
```

## Requirements

- Python 3.12+
- Django 6.0+

### Supported Databases

Django 6.0 officially supports the following database versions:

| Database | Minimum Version | Notes |
|----------|-----------------|-------|
| PostgreSQL | 14+ | Recommended for production. Full `SELECT FOR UPDATE SKIP LOCKED` support. |
| MySQL | 8.0.11+ | Full `SELECT FOR UPDATE SKIP LOCKED` support. |
| MariaDB | 10.6+ | Full `SELECT FOR UPDATE SKIP LOCKED` support. |
| SQLite | 3.31.0+ | Works for development/testing, but no row-level locking. |
| Oracle | 19c+ | Supported but not tested with this package. |

**Note**: `SELECT FOR UPDATE SKIP LOCKED` is used to prevent duplicate task execution in multi-worker environments. SQLite does not support row-level locking, so it is only recommended for development or single-worker deployments.

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

@task
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

### Important: JSON-Serializable Parameters

Task arguments, keyword arguments, and return values **must be JSON-serializable**.

Supported types:
- `str`, `int`, `float`, `bool`, `None`
- `dict` (with JSON-serializable keys and values)
- `list`, `tuple` (with JSON-serializable elements)
- `bytes` (UTF-8 decodable only)

**Not supported** (will raise `TypeError`):
- `datetime`, `date`, `time` - convert to ISO string: `dt.isoformat()`
- `UUID` - convert to string: `str(uuid)`
- `Decimal` - convert to float or string
- Custom objects - serialize manually

```python
from django.tasks import task

# ❌ This will raise TypeError
@task
def bad_task(user_id, created_at):
    pass
bad_task.enqueue(123, datetime.now())  # TypeError!

# ✅ Convert to JSON-serializable types
@task
def good_task(user_id, created_at_iso):
    created_at = datetime.fromisoformat(created_at_iso)
    # ...
good_task.enqueue(123, datetime.now().isoformat())  # OK
```

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

### Async tasks

```python
@task
async def fetch_data(url):
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            return await response.text()

# Enqueue like normal tasks
result = fetch_data.enqueue("https://example.com/api")
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

## Programmatic API

You can also process tasks programmatically without management commands:

```python
from django_database_task import (
    process_one_task,
    process_tasks,
    get_pending_task_count,
    run_task_by_id,
)

# Process a single task
result = process_one_task()
if result:
    print(f"Processed: {result.id}, status: {result.status}")

# Process multiple tasks
results = process_tasks(max_tasks=10)
print(f"Processed {len(results)} tasks")

# Process tasks from a specific queue
results = process_tasks(queue_name="emails", max_tasks=5)

# Get pending task count
count = get_pending_task_count()
print(f"Pending tasks: {count}")

# Execute a specific task by ID
result = run_task_by_id("550e8400-e29b-41d4-a716-446655440000")
if result:
    print(f"Executed: {result.id}, status: {result.status}")

# Retry a failed task
result = run_task_by_id("...", allow_retry=True)
```

## HTTP Endpoints (Optional)

For environments where cron or direct command execution is not available
(e.g., serverless, PaaS), you can use HTTP endpoints to trigger task processing.

### Setup

Include the URLs in your project:

```python
# urls.py
from django.urls import path, include

urlpatterns = [
    path("tasks/", include("django_database_task.urls")),
]
```

### Available Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/tasks/run/` | POST | Process multiple pending tasks |
| `/tasks/run-one/` | POST | Process a single pending task |
| `/tasks/status/` | GET | Get pending task count |
| `/tasks/execute/<uuid>/` | POST | Execute a specific task by ID |
| `/tasks/purge/` | POST | Delete completed tasks |

### Request Parameters

#### POST `/tasks/run/`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `max_tasks` | int | 10 | Maximum tasks to process (1-100) |
| `queue_name` | string | null | Filter by queue name |
| `backend_name` | string | "default" | Task backend name |

Response:
```json
{
  "processed": 3,
  "results": [
    {"id": "uuid", "status": "SUCCESSFUL", "task_path": "myapp.tasks.send_email"},
    {"id": "uuid", "status": "FAILED", "task_path": "myapp.tasks.process_data"}
  ]
}
```

#### POST `/tasks/run-one/`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `queue_name` | string | null | Filter by queue name |
| `backend_name` | string | "default" | Task backend name |

Response:
```json
{"processed": true, "result": {"id": "uuid", "status": "SUCCESSFUL", "task_path": "..."}}
```
or
```json
{"processed": false, "result": null}
```

#### GET `/tasks/status/`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `queue_name` | string | null | Filter by queue name |
| `backend_name` | string | "default" | Task backend name |

Response:
```json
{"pending_count": 5}
```

#### POST `/tasks/execute/<uuid>/`

Execute a specific task by ID. This endpoint is designed for external trigger systems
(e.g., Cloud Tasks, webhooks) that need to execute a specific task.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `fail_on_error` | query string | "false" | Return HTTP 500 on task failure |
| `allow_retry` | query string | "false" | Allow re-execution of FAILED tasks |

Response (success):
```json
{"executed": true, "result": {"id": "uuid", "status": "SUCCESSFUL", "task_path": "..."}}
```

Response (task not in executable status):
```json
{"executed": false, "reason": "Task is not in READY status"}
```

Response (task not found):
```json
{"error": "Task not found"}  // HTTP 404
```

#### POST `/tasks/purge/`

Delete completed tasks from the database. Useful for cron-based cleanup.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `days` | int | 0 | Delete tasks completed more than N days ago (0=all) |
| `status` | string | "SUCCESSFUL,FAILED" | Target statuses, comma-separated |
| `batch_size` | int | 1000 | Number of tasks to delete at once (max: 10000) |
| `dry_run` | bool | false | If true, return count without deleting |

Response:
```json
{"deleted": 150, "dry_run": false}
```

Response (dry run):
```json
{"count": 150, "dry_run": true}
```

### Example Usage

```bash
# Process up to 10 tasks
curl -X POST http://localhost:8000/tasks/run/ \
  -H "Content-Type: application/json" \
  -d '{"max_tasks": 10}'

# Process tasks from a specific queue
curl -X POST http://localhost:8000/tasks/run/ \
  -H "Content-Type: application/json" \
  -d '{"queue_name": "emails", "max_tasks": 5}'

# Get pending task count
curl http://localhost:8000/tasks/status/

# Delete tasks completed more than 7 days ago
curl -X POST http://localhost:8000/tasks/purge/ \
  -H "Content-Type: application/json" \
  -d '{"days": 7}'

# Dry run to check how many tasks would be deleted
curl -X POST http://localhost:8000/tasks/purge/ \
  -H "Content-Type: application/json" \
  -d '{"days": 30, "dry_run": true}'
```

### Use Cases

#### Cloud Scheduler / Cron Job

Call the endpoint periodically to process tasks:

```bash
# Every minute via cron or Cloud Scheduler
curl -X POST https://your-app.com/tasks/run/ \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"max_tasks": 50}'
```

#### Webhook Trigger

Trigger task processing after an event:

```python
# In your webhook handler
import requests

def handle_webhook(request):
    # ... process webhook ...

    # Trigger background task processing
    requests.post(
        "http://localhost:8000/tasks/run/",
        json={"max_tasks": 10}
    )
```

#### Health Check with Task Status

Monitor pending task count:

```bash
# Alert if too many pending tasks
count=$(curl -s http://localhost:8000/tasks/status/ | jq '.pending_count')
if [ "$count" -gt 100 ]; then
  echo "Warning: $count pending tasks"
fi
```

#### Scheduled Cleanup

Use cron or Cloud Scheduler to delete old completed tasks:

```bash
# Daily cleanup via cron or Cloud Scheduler
# Delete tasks completed more than 30 days ago
curl -X POST https://your-app.com/tasks/purge/ \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"days": 30}'
```

### Security

The endpoints are CSRF-exempt for API/webhook use. **Always add authentication in production:**

```python
from django.contrib.admin.views.decorators import staff_member_required
from django_database_task.views import (
    RunTasksView,
    RunOneTaskView,
    TaskStatusView,
    PurgeCompletedTasksView,
)

urlpatterns = [
    path(
        "tasks/run/",
        staff_member_required(RunTasksView.as_view()),
        name="run_tasks",
    ),
    path(
        "tasks/run-one/",
        staff_member_required(RunOneTaskView.as_view()),
        name="run_one_task",
    ),
    path(
        "tasks/status/",
        staff_member_required(TaskStatusView.as_view()),
        name="task_status",
    ),
    path(
        "tasks/purge/",
        staff_member_required(PurgeCompletedTasksView.as_view()),
        name="purge_completed_tasks",
    ),
]
```

Or use token-based authentication:

```python
from django.http import HttpResponseForbidden
from django.conf import settings

def require_api_token(view_func):
    def wrapper(request, *args, **kwargs):
        token = request.headers.get("Authorization", "").replace("Bearer ", "")
        if token != settings.TASK_API_TOKEN:
            return HttpResponseForbidden("Invalid token")
        return view_func(request, *args, **kwargs)
    return wrapper

urlpatterns = [
    path("tasks/run/", require_api_token(RunTasksView.as_view())),
]
```

## Google Cloud Tasks Integration

For serverless environments like Google App Engine or Cloud Run, you can use the Cloud Tasks backend to automatically create Cloud Tasks when tasks are enqueued.

### Installation

```bash
pip install django-database-task[cloudtasks]
```

### Quick Setup

```python
# settings.py
TASKS = {
    "default": {
        "BACKEND": "django_database_task.cloudtasks.CloudTasksDatabaseBackend",
        "QUEUES": [],  # Allow all queue names
    },
}
```

Project ID, location, and handler URL are auto-detected from GAE/Cloud Run environment.

**Important**: Set `QUEUES: []` to allow any queue name, or list the queues you use:
```python
"QUEUES": ["default", "emails", "batch"],  # Only these queues allowed
```

The Cloud Tasks queue name is determined by the task's `queue_name` attribute:

```python
@task  # Uses "default" queue
def normal_task():
    pass

@task(queue="batch")  # Uses "batch" queue
def batch_task():
    pass

@task(queue="high-priority")  # Uses "high-priority" queue
def urgent_task():
    pass
```

This allows you to configure different rate limits and concurrency settings per queue in Cloud Tasks.

### How It Works

```mermaid
sequenceDiagram
    participant App as Application
    participant Backend as CloudTasksDatabaseBackend
    participant DB as Database
    participant CT as Cloud Tasks
    participant Handler as /tasks/execute/

    Note over App,Handler: Task Enqueue
    App->>Backend: task.enqueue(args, kwargs)
    Backend->>DB: INSERT task (status=READY)
    DB-->>Backend: Task ID
    Backend->>CT: Create Cloud Task (task_id only)
    CT-->>Backend: OK
    Backend-->>App: TaskResult (id, status=READY)

    Note over App,Handler: Task Execution (triggered by Cloud Tasks)
    CT->>Handler: POST /tasks/execute/<task_id>/<br/>(with OIDC token if configured)
    Handler->>Handler: Verify OIDC token (optional)
    Handler->>DB: SELECT task by ID
    DB-->>Handler: Task record
    Handler->>DB: UPDATE status=RUNNING
    Handler->>Handler: Execute task function
    alt Success
        Handler->>DB: UPDATE status=SUCCESSFUL
        Handler-->>CT: HTTP 200
    else Failure
        Handler->>DB: UPDATE status=FAILED
        Handler-->>CT: HTTP 500 (triggers retry)
    end
```

The Cloud Task only contains the task ID. All task parameters are stored in the database, ensuring:
- **Blue/Green deployment support**: Tasks execute on the same version that enqueued them
- **Database as source of truth**: Task parameters are never lost
- **Automatic retry**: Cloud Tasks handles retry with the task ID

### Configuration Options

```python
TASKS = {
    "default": {
        "BACKEND": "django_database_task.cloudtasks.CloudTasksDatabaseBackend",
        "OPTIONS": {
            # All settings are optional - auto-detected from environment

            # Override auto-detection if needed
            # "CLOUD_TASKS_PROJECT": "my-project",
            # "CLOUD_TASKS_LOCATION": "asia-northeast1",
            # "TASK_HANDLER_URL": "https://myapp.example.com/tasks/execute/{task_id}/",
            # "TASK_HANDLER_PATH": "/tasks/execute/{task_id}/",

            # OIDC authentication (optional)
            # "OIDC_SERVICE_ACCOUNT_EMAIL": "...",
            # "OIDC_AUDIENCE": "https://...",
        },
    },
}
```

### Auto-Detection

| Setting | Detection Method | Description |
|---------|------------------|-------------|
| Project | `GOOGLE_CLOUD_PROJECT` env var | GCP project ID |
| Location | `CLOUD_RUN_REGION` env var, or metadata server | Cloud Tasks region |
| Handler URL | Built from `K_SERVICE`, `GAE_SERVICE`, `GAE_VERSION` | Task execution endpoint |
| Queue name | Task's `queue_name` attribute | Defaults to "default" |

### OIDC Authentication

When `OIDC_SERVICE_ACCOUNT_EMAIL` is configured, Cloud Tasks will send OIDC tokens with each request. The backend automatically verifies these tokens on the `/tasks/execute/` and `/tasks/purge/` endpoints.

```python
# settings.py - Automatic OIDC verification
TASKS = {
    "default": {
        "BACKEND": "django_database_task.cloudtasks.CloudTasksDatabaseBackend",
        "QUEUES": [],  # Allow all queue names
        "OPTIONS": {
            "OIDC_SERVICE_ACCOUNT_EMAIL": "my-sa@project.iam.gserviceaccount.com",
            # OIDC_AUDIENCE is auto-detected from handler URL if not set
        },
    },
}
```

Alternatively, you can use the decorator directly on your URL configuration:

```python
# urls.py
from django.urls import path
from django_database_task.views import ExecuteTaskView
from django_database_task.cloudtasks import verify_cloud_tasks_oidc

urlpatterns = [
    path(
        "tasks/execute/<uuid:task_id>/",
        verify_cloud_tasks_oidc(
            ExecuteTaskView.as_view(),
            audience="https://myapp.example.com"
        ),
        name="execute_task",
    ),
]
```

### Detection Utilities

You can use the detection functions directly:

```python
from django_database_task.cloudtasks import (
    detect_gcp_project,
    detect_gcp_location,
    detect_task_handler_host,
    is_cloud_run,
    is_app_engine,
)

if is_cloud_run():
    print(f"Running on Cloud Run in {detect_gcp_location()}")
elif is_app_engine():
    print(f"Running on App Engine in project {detect_gcp_project()}")
```

## Django Admin

The package includes a Django Admin integration to view and manage tasks:

- Task list with status badges
- Filter by status, queue, backend
- Search by task ID or path
- View task arguments and results

### Admin Actions

The admin interface provides the following bulk actions:

| Action | Description |
|--------|-------------|
| **Run selected tasks** | Execute selected tasks that are in READY status |
| **Retry failed tasks** | Reset FAILED tasks to READY status and re-execute them |

These actions are useful for:
- Manually triggering task execution from the admin
- Retrying failed tasks after fixing issues
- Testing task execution during development

## License

MIT License - see [LICENSE](LICENSE) for details.
