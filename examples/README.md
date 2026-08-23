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

### Context-Aware Task

1. Enqueue "Context Task" with a message
2. Start the worker
3. Verify the result includes task ID and attempt number from context

### Queue-Specific Tasks (Newsletter)

1. Enqueue "Newsletter" task (uses 'emails' queue)
2. Start the worker with `--queue emails` option:
   ```bash
   ../venv/bin/python manage.py run_database_tasks --queue emails --continuous
   ```
3. Verify the task is processed only by the emails queue worker

## Trying the SQS broker

The demo runs on the plain database backend by default. Set `DEMO_BROKER=sqs`
to run it against Amazon SQS instead, with a local mock standing in for AWS.

### 1. Start a local SQS

[moto](https://github.com/getmoto/moto) is the lightest option: it is a Python
package, so no container is needed.

```bash
../venv/bin/pip install "django-database-task[sqs]" "moto[server]"
../venv/bin/python -m moto.server -p 5555
```

Any other SQS compatible endpoint, LocalStack included, works the same way —
point `SQS_ENDPOINT_URL` at it instead.

### 2. Create the queues

The SQS queue name is the task's `queue_name`, so the demo needs one queue per
queue name its tasks use.

```bash
cd examples

export AWS_ACCESS_KEY_ID=test
export AWS_SECRET_ACCESS_KEY=test
export AWS_REGION=ap-northeast-1
export SQS_ENDPOINT_URL=http://localhost:5555
export DEMO_BROKER=sqs

../venv/bin/python -c "
import boto3, os
sqs = boto3.client('sqs', region_name=os.environ['AWS_REGION'],
                   endpoint_url=os.environ['SQS_ENDPOINT_URL'])
for name in ['default', 'emails']:
    print(sqs.create_queue(QueueName=name)['QueueUrl'])
"
```

### 3. Enqueue a task

```bash
../venv/bin/python manage.py shell -c "
from demo_app.tasks import add_numbers
print(add_numbers.enqueue(2, 3).id)
"
```

The task is saved to the database and a message naming it is sent to the
`default` queue.

### 4. Run the worker

```bash
../venv/bin/python manage.py run_database_tasks
```

```
Worker ID: host-abe60385
Backend: default
Source: both
Broker: SQSBroker (wait=20.0s, max_messages=1)

Processing task from broker: 5eac9590-8515-4ab1-b0ce-4f38f780f909
  Task completed successfully
No more tasks to process.

Total tasks processed: 2
```

Same command as always. `Source: both` is `--source auto` noticing the broker:
the worker receives from SQS and sweeps the database.

### Queues

A worker without `--queue` receives from the `default` SQS queue only, while
its database sweep covers every queue. So a task on another queue is run, but
by the database half of the worker, and its SQS message is left behind. Run one
worker per queue instead:

```bash
../venv/bin/python manage.py run_database_tasks --queue emails --continuous
```

A message for a task that has already run is not a problem: the worker reports
`Task is not ready to run; nothing to do` and deletes it.

### Deferred tasks and the 15 minute limit

SQS cannot hold a message for longer than 15 minutes, so a task deferred
further out is not sent to the queue at all.

```bash
../venv/bin/python manage.py shell -c "
from datetime import timedelta
from django.utils import timezone
from demo_app.tasks import add_numbers

add_numbers.using(run_after=timezone.now() + timedelta(minutes=5)).enqueue(1, 1)
add_numbers.using(run_after=timezone.now() + timedelta(hours=3)).enqueue(2, 2)
"

../venv/bin/python -c "
import boto3, os
sqs = boto3.client('sqs', region_name=os.environ['AWS_REGION'],
                   endpoint_url=os.environ['SQS_ENDPOINT_URL'])
url = sqs.get_queue_url(QueueName='default')['QueueUrl']
print(sqs.get_queue_attributes(QueueUrl=url, AttributeNames=[
    'ApproximateNumberOfMessages', 'ApproximateNumberOfMessagesDelayed',
])['Attributes'])
"
```

Only the five minute task is in the queue, as a delayed message. The three hour
one stays `READY` in the database, and the worker's database sweep runs it once
it is due — which is why the worker should be left running with `--continuous`.

### Back to the database backend

Unset `DEMO_BROKER`, or set it to anything other than `sqs`.

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
from demo_app.tasks import add_numbers, send_email_task, newsletter_task, context_aware_task
result = add_numbers.enqueue(10, 20)
print(f"Task ID: {result.id}")

# Enqueue a task with specific queue
result = newsletter_task.enqueue(100)
print(f"Newsletter Task ID: {result.id}, Queue: emails")

# Enqueue a context-aware task
result = context_aware_task.enqueue("Hello from shell!")
print(f"Context Task ID: {result.id}")

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
