# Changelog

## Unreleased

### Fixed

- A task could run twice when more than one worker polled the same queue.
  `fetch_task()` took the row with `SELECT FOR UPDATE SKIP LOCKED` but
  released the lock on return, and `run_task()` then wrote `RUNNING` without
  checking the status, so a second worker that fetched the row in between ran
  the same task, and its worker id overwrote the first attempt's. `run_task()`
  now moves the row from `READY` to `RUNNING` inside a locked transaction that
  re-checks the status, appends the worker id to the list stored in the
  database, and returns `None` instead of running a task that is no longer
  `READY`. `process_one_task()` moves on to the next task in that case,
  `run_database_tasks` reports it without counting it, and the admin action
  counts it as skipped. Existing projects need no changes; a project calling
  `backend.run_task()` directly should expect `None` for a task another
  worker claimed first.
- A task whose function could not be imported (the module was renamed, the
  function removed, a worker running older code than the enqueuer) was left
  `RUNNING` with no error recorded. `run_task()` wrote `RUNNING` and then
  raised out of the import, before the block that records a failure, so
  nothing told the task apart from one held by a live worker until
  `requeue_stale_database_tasks` requeued it for the next worker to fail on
  in the same way, and a broker redelivered its message for as long as that
  went on. It is now recorded `FAILED` with the traceback in its errors and
  `finished_at` set, the same as a task that raised: the admin shows the
  error, *Retry failed tasks* runs it again once the code is fixed, and the
  broker message is acknowledged. The ERROR record `Task could not be
  started` reports it, with `status` and `error_class`, in place of the
  worker's `Worker could not run task`, which is now only logged when the
  worker itself fails (a database error during the claim, say). The
  `task_started` and `task_finished` signals are not sent for such a task,
  since there is no task object to send them with. Existing projects need
  no changes.
- An invalid `--empty-exit-code` or `--failed-exit-code` given to
  `run_database_tasks` on the command line (`abc`, `4.5`, `-1`, `300`)
  printed a `CommandError` traceback and exited 1. The option parser raised
  `CommandError`, which argparse does not turn into a usage error, and
  `run_from_argv()` parses the arguments outside the block that catches it.
  It now raises `argparse.ArgumentTypeError`, so the command prints its
  usage and a one-line error and exits 2, like any other invalid option.
  `call_command()` still raises `CommandError`. Valid values and the
  defaults are unchanged; existing projects need no changes.
  ([#22](https://github.com/tokibito/django-database-task/issues/22))
- Two records `run_database_tasks` writes on the broker path carried fewer
  fields than the README's *Structured logging* section promises, so they
  could not be filtered like the others. `Worker could not run task from
  broker` carried only `worker_id` and `task_id`; it now carries the full
  task set (`task_path`, `queue_name`, `priority` and `backend_alias` as
  well), read from the task's row, and keeps the two it had when the row
  cannot be read either. `Error receiving from broker` now carries
  `backend_alias` alongside `worker_id`, `queue_name` and `broker`. Both are
  listed in the README's record table. No field was renamed or removed and
  the messages are unchanged, so existing log filters keep matching;
  existing projects need no changes.
  ([#32](https://github.com/tokibito/django-database-task/issues/32))

### Documentation

- The timer-driven systemd unit in the README lists 4 in `SuccessExitStatus`
  as the code for an idle run, but its `ExecStart` did not pass
  `--empty-exit-code=4`, so an idle run exited 0 and the 4 never occurred.
  The `ExecStart` line now passes it, matching the `flock` example above it.

## 0.5.0

### Added

- **Recovery of tasks left in `RUNNING`**
  (`manage.py requeue_stale_database_tasks --older-than 15m`). A worker killed
  outright — SIGKILL, the OOM killer, a node failure — never writes a result,
  so the task it held stays `RUNNING` and no other worker picks it up. The new
  command finds those tasks and puts them back in `READY`. Previously the only
  way out was a hand-written query.
- `--older-than` is required and takes a unit (`90s`, `15m`, `2h`, `1d`). It
  has to be longer than the longest task takes to run: nothing distinguishes a
  dead worker from a slow task, so a threshold below that requeues tasks that
  are still running.
- `--max-attempts` (default 3) marks a task `FAILED` instead of requeueing it
  once it has been handed to that many workers, so a task that kills its own
  worker cannot be requeued forever.
- `--mark-failed` records stale tasks as `FAILED` without requeueing them, for
  tasks that are not safe to run twice, and `--notify-broker` re-notifies the
  broker for workers that only receive from one. Also available as
  `django_database_task.requeue_stale_tasks()` and as a "Requeue tasks stuck in
  running" action in the Django admin.
- **PostgreSQL LISTEN/NOTIFY broker**
  (`django_database_task.postgres.PostgresNotifyDatabaseBackend`). Notifies a
  channel of the database the tasks are already stored in, so a waiting worker
  starts the task in milliseconds instead of on the next poll. It needs no
  queue, no credentials and no extra service — only the PostgreSQL connection
  the project already has.
- The notification is sent with `pg_notify()` on the connection that inserted
  the task and inside the same transaction, so PostgreSQL delivers it on
  commit. A worker is never told about a task it cannot yet see, or one whose
  transaction was rolled back.
- A `postgres` extra, for a project that has not installed a PostgreSQL driver
  yet. psycopg 3 and psycopg2 both work.
- **Exit codes for job schedulers**: `run_database_tasks --empty-exit-code` and
  `--failed-exit-code`. An on-premise scheduler (JP1, Hinemos, Rundeck, cron, a
  systemd timer) decides what happened from the exit code, and the command
  previously exited 0 whether it drained the queue, found nothing, or ran a
  task that failed. Both options default to 0, so nothing changes for an
  existing `cron` line or Kubernetes `Job` until they are set. A failed task
  outranks an idle run; a broker that could not be reached is neither.
- **Structured log fields.** The library's log records now carry their context
  as attributes — `task_id`, `task_path`, `queue_name`, `priority`,
  `backend_alias`, `worker_id`, plus `status`, `duration_ms` and `error_class`
  where they apply — instead of only interpolating it into the message. A JSON
  formatter now emits fields an operator can filter on. `Task started`,
  `Worker started` and `Worker finished` records are new; the last carries
  `tasks_processed`, `tasks_failed` and `exit_code`.
- Documentation for running the worker from a job scheduler: the exit code
  table, `flock` for keeping a slow run from being overlapped by the next one,
  systemd unit samples for both the timer-driven and the long-running shape,
  and a `LOGGING` configuration that produces JSON.

### Changed

- A broker's `enqueue()` method is now called `notify()`. The old name read as
  if it enqueued the task, which is the backend's job: a broker is only told
  about a task the database already holds, and carries nothing but its id. The
  new name matches what the method does, what `notify_broker()` is called and
  what every broker docstring already said.
- The log record for a broker failure now reads `Broker X failed to notify
  about task Y`, in place of `failed to enqueue task`.

### Deprecated

- `TaskBroker.enqueue()`. A broker that overrides it is still called, with a
  `DeprecationWarning`, and stops being called in 0.6. Rename it to `notify()`.
  Bundled brokers and the `BROKER` option are unaffected; only a broker written
  by hand against 0.4 needs the change.

### Removed

- `get_auth_handler()` (singular), deprecated in 0.4 and removed here as
  announced. A backend that overrides it is no longer called and its endpoints
  fall back to whatever `get_auth_handlers()` returns — which, unless the
  backend also overrides that or sets `AUTH_HANDLERS`, is nothing, leaving the
  endpoints unauthenticated. Override `get_auth_handlers()` instead. The
  `CLOUD_TASKS_*` options, `AUTH_HANDLERS` and the bundled backends are
  unaffected.

## 0.4.0

Brokers — the services that trigger execution of a saved task — are now
separate from the task backend, and Amazon SQS joins Cloud Tasks as one of
them.

**Existing projects need no changes.** The settings, the URLs, the management
commands and their defaults all behave as they did in 0.3.

### Added

- **Amazon SQS broker** (`django_database_task.sqs.SQSDatabaseBackend`,
  `pip install django-database-task[sqs]`). Sends a message naming the task,
  and `run_database_tasks` receives it. A task deferred beyond the 15 minute
  SQS delay limit stays in the database for the worker's database sweep.
- **`--source` for `run_database_tasks`**: `auto` (default), `db`, `broker` or
  `both`. `auto` means `both` when the backend has a broker a worker can
  receive from, and `db` otherwise, so the command is run the same way as
  before either way. `--wait-time` and `--max-messages` go with it.
- **The broker abstraction** (`django_database_task.brokers`): `TaskBroker`,
  `HTTPPushBroker` and `PullBroker`. A project attaches its own with the
  `BROKER` option.
- **Several authentication handlers per backend**, through
  `get_auth_handlers()`. A request is accepted as soon as one handler accepts
  it, so the service that calls the endpoints and an external cron job can use
  different credentials. Configure them with the `AUTH_HANDLERS` option, and
  limit one to some endpoints with `ENDPOINTS`.
- **Bundled authentication handlers** in `django_database_task.auth`:
  `SharedSecretAuth`, `HMACAuth` and `StaffOnlyAuth`, with `build_signature()`
  for callers that have to sign a request for `HMACAuth`.
- **AWS environment detection** in `django_database_task.sqs`:
  `detect_aws_region()`, `is_lambda()` and `is_ecs()`, alongside the existing
  Cloud Tasks ones.
- The brokers themselves are importable, for a project that wants one on a
  backend of its own: `django_database_task.sqs.SQSBroker` and
  `django_database_task.cloudtasks.CloudTasksBroker`.

### Fixed

- Enabling Cloud Tasks OIDC no longer locks every other caller out of the task
  endpoints. Since 0.3.1 the OIDC handler was applied to all of them, so an
  external cron job calling `/tasks/run/` or `/tasks/purge/` was rejected.
- The Cloud Tasks tests were skipped on every run, in CI included, because the
  `cloudtasks` extra was never installed. Four of them had gone stale
  unnoticed.

### Deprecated

- `get_auth_handler()` (singular). It still works, with a
  `DeprecationWarning`, and is removed in 0.5. Override `get_auth_handlers()`
  instead.

### Documentation

- A *Task Brokers* section in the README, and an Amazon SQS one with a
  sequence diagram beside the ones the database backend and Cloud Tasks
  already had.
- An SQS walkthrough in `examples/`, run against a local mock: set
  `DEMO_BROKER=sqs` to point the demo project at it.
- `CONTRIBUTING.md`, covering the development setup, the tests, and how to add
  a broker.
- This file. Releases before 0.4.0 are in the git history and the GitHub
  releases.
