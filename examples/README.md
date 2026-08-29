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
It stays on SQLite; only the broker changes.

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

## Trying the PostgreSQL LISTEN/NOTIFY broker

The demo runs on the plain database backend and SQLite by default. Set
`DEMO_BROKER=postgres` to run it against PostgreSQL instead, where saving a
task notifies a channel and the worker starts it at once rather than on the
next poll.

Unlike the SQS walkthrough, this one needs a PostgreSQL database — the
notification travels through the same connection the tasks are stored in.

### 1. Start a PostgreSQL

```bash
docker run -d --name ddt-demo-pg \
    -e POSTGRES_USER=demo -e POSTGRES_PASSWORD=demo -e POSTGRES_DB=demo \
    -p 55432:5432 postgres:16-alpine
```

Port 55432 is the default the demo settings look for, so it does not collide
with a PostgreSQL you may already be running on 5432. Override it, and the
rest of the connection, with `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`,
`POSTGRES_USER` and `POSTGRES_PASSWORD`.

Install a driver if the environment has none:

```bash
pip install "psycopg[binary]"
```

### 2. Create the tables

The demo keeps its PostgreSQL data separate from the SQLite file, so migrate
again:

```bash
DEMO_BROKER=postgres python manage.py migrate
```

### 3. Start the worker

```bash
DEMO_BROKER=postgres python manage.py run_database_tasks --continuous
```

```
Worker ID: myhost-3f9c2a10
Backend: default
Source: both
Continuous mode: interval=5.0s
Broker: PostgresNotifyBroker (wait=20.0s, max_messages=1)
Graceful shutdown: enabled (timeout=unlimited)
```

`Source: both` is the default for a backend with a broker a worker can wait
on: the worker waits on the channel *and* sweeps the database, which is what
runs the deferred tasks below.

### 4. Enqueue a task

In another terminal:

```bash
DEMO_BROKER=postgres python manage.py shell
```

```python
from demo_app.tasks import add_numbers
add_numbers.enqueue(21, 21)
```

The worker prints the task within milliseconds — there is no polling interval
to wait out:

```
Processing task from broker: e90e87dc-b1cd-4976-933c-69604f097c9f
  Task completed successfully
```

Start the web server with `DEMO_BROKER=postgres python manage.py runserver`
and the demo pages enqueue through the broker too.

### It only fires on commit

`pg_notify()` runs inside the transaction that inserted the task, so the
notification is delivered when that transaction commits and not before:

```python
from django.db import transaction
from demo_app.tasks import add_numbers

with transaction.atomic():
    result = add_numbers.enqueue(1, 2)
    input("the worker has not been told yet — press Enter to commit")
```

The worker stays quiet until the block exits, then runs the task at once. Roll
the transaction back instead and it is never told at all, because the task is
no longer there.

### Broadcast, not a queue

Start a second worker in another terminal and enqueue a task. Both receive the
notification, both go for the task, and one of them reports:

```
Processing task from broker: b1352efb-2907-4d07-93ee-d9d4f646720d
  Task is not ready to run; nothing to do
```

That is the loser of the race finding the task already taken by the `READY`
check and the row lock. Nothing runs twice; the wasted query is the cost of a
broadcast channel.

### Queues

A worker started with `--queue` receives every notification and keeps only the
ones for its queue:

```bash
DEMO_BROKER=postgres python manage.py run_database_tasks --queue emails --continuous
```

```python
from demo_app.tasks import newsletter_task
newsletter_task.enqueue(100)
```

### Deferred tasks

A notification cannot be held back, so a task with a future `run_after` is not
announced at all — a worker acting on it would run the task early:

```python
from datetime import timedelta
from django.utils import timezone
from demo_app.tasks import add_numbers

add_numbers.using(run_after=timezone.now() + timedelta(seconds=30)).enqueue(3, 4)
```

The worker stays quiet, then runs it from the database sweep once it is due.
The sweep happens when the wait on the channel times out, so the task can be
up to `--wait-time` seconds (20 by default) late. Lower it to see the
difference:

```bash
DEMO_BROKER=postgres python manage.py run_database_tasks --continuous --wait-time 2
```

The same sweep is what runs tasks enqueued while no worker was connected: a
notification only reaches whoever is listening at that moment.

### Back to the database backend

Unset `DEMO_BROKER`, or set it to anything other than `postgres`, which also
puts the demo back on SQLite. Remove the container with:

```bash
docker rm -f ddt-demo-pg
```

## Recovering a Task Left in RUNNING

A worker killed with `SIGKILL` never gets to write a result, so the task it was
running stays in `RUNNING` forever. Here is how to produce that state and
recover from it.

### 1. Start a long task and kill the worker

```bash
cd examples

# A task that takes about 20 seconds
../venv/bin/python manage.py shell -c "
from demo_app.tasks import process_data
print(process_data.enqueue(2000).id)
"

# Start a worker, let it pick the task up, then kill it outright
../venv/bin/python manage.py run_database_tasks &
WORKER=$!
sleep 4
kill -9 $WORKER
```

The task is now stranded:

```bash
../venv/bin/python manage.py shell -c "
from django_database_task.models import DatabaseTask
t = DatabaseTask.objects.order_by('-created_at').first()
print(t.status, t.last_attempted_at, t.worker_ids_json)
"
# RUNNING 2026-01-01 12:00:00+00:00 ['myhost-10b3cb8f']
```

Starting another worker does nothing - a `RUNNING` task is not offered to
anyone.

### 2. Recover it

```bash
# See what would happen first
../venv/bin/python manage.py requeue_stale_database_tasks --older-than 1s --dry-run
```

```console
Stale after: 1s
Mode: requeue (max attempts: 3)
Found 1 stale tasks
Dry run mode - nothing was changed (1 would be requeued, 0 would be marked failed)
```

```bash
../venv/bin/python manage.py requeue_stale_database_tasks --older-than 1s
```

```console
Stale after: 1s
Mode: requeue (max attempts: 3)
Found 1 stale tasks
Requeued 1 tasks, marked 0 as failed
```

`--older-than 1s` is fine for this demo because you know the worker is dead.
**In production the threshold has to be longer than your longest task** - a
task still running when the threshold passes is requeued and ends up running
twice. See
[Choosing --older-than](../README.md#choosing---older-than).

### 3. Watch a worker finish it

```bash
../venv/bin/python manage.py run_database_tasks
```

```console
Processing task: 9dbf00aa-... (demo_app.tasks.process_data)
  Task completed successfully
```

The task ran from the beginning on the second worker, and both attempts are
recorded:

```bash
../venv/bin/python manage.py shell -c "
from django_database_task.models import DatabaseTask
t = DatabaseTask.objects.order_by('-created_at').first()
print(t.status, t.worker_ids_json)
"
# SUCCESSFUL ['myhost-10b3cb8f', 'myhost-b92f4fab']
```

### 4. A task that keeps killing its worker

After `--max-attempts` workers (3 by default) the task is marked `FAILED`
instead of being handed to a fourth. Fake a task that has used them up:

```bash
../venv/bin/python manage.py shell -c "
from datetime import timedelta
from django.tasks.base import TaskResultStatus
from django.utils import timezone
from django_database_task.models import DatabaseTask
from demo_app.tasks import process_data

t = DatabaseTask.objects.get(id=process_data.enqueue(10).id)
t.status = TaskResultStatus.RUNNING
t.last_attempted_at = t.started_at = timezone.now() - timedelta(hours=2)
t.worker_ids_json = ['w-1', 'w-2', 'w-3']
t.save()
"

../venv/bin/python manage.py requeue_stale_database_tasks --older-than 1h
```

```console
Found 1 stale tasks
Requeued 0 tasks, marked 1 as failed
```

The recorded error says what happened, in the place a traceback would go:

```bash
../venv/bin/python manage.py shell -c "
from django_database_task.models import DatabaseTask
t = DatabaseTask.objects.filter(worker_ids_json=['w-1','w-2','w-3']).first()
print(t.errors_json[0]['traceback'])
"
# django_database_task.exceptions.WorkerLost: demo_app.tasks.process_data was
# handed to w-3 at ... and was still RUNNING at ..., so the worker is presumed
# dead. Attempts so far: 3. No exception was raised by the task itself.
```

Use `--mark-failed` to get this for every stale task, without requeueing
anything - the right choice for tasks that are not safe to run twice.

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
