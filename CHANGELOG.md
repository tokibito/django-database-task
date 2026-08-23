# Changelog

## Unreleased

To be released as 0.4.0.

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
  `SharedSecretAuth`, `HMACAuth` and `StaffOnlyAuth`.

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
