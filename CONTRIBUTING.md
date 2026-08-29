# Contributing

Thanks for your interest in django-database-task.

Issues and pull requests are welcome at
<https://github.com/tokibito/django-database-task>. Everything that lands in
the repository — code, comments, docstrings, commit messages, pull request
descriptions and documentation — is written in English.

## Getting set up

Python 3.12 or newer and Django 6.0 or newer.

```bash
git clone https://github.com/tokibito/django-database-task.git
cd django-database-task

python -m venv venv
venv/bin/pip install -e ".[dev,cloudtasks,sqs]"
```

**Install every extra, even for a change that touches neither integration.**
The Cloud Tasks and SQS tests guard themselves with `pytest.importorskip`, so
without `google-cloud-tasks` and `boto3` they are quietly skipped rather than
reported as missing. CI installs all of them for the same reason.

## Running the tests

```bash
venv/bin/pytest
```

Tests use pytest-django with the settings in `tests/settings.py`, on an
in-memory SQLite database. Neither the Cloud Tasks nor the SQS tests reach the
network: both stub their client.

Apart from the PostgreSQL integration tests below, a full run reports **no
skipped tests**. If you see others, an extra is missing from your environment —
see above.

To run part of the suite:

```bash
venv/bin/pytest tests/test_backend.py
venv/bin/pytest tests/sqs/
venv/bin/pytest -k "broker and auth"
```

### Against PostgreSQL

`tests/postgres/test_listen_notify.py` runs the LISTEN/NOTIFY broker against a
real server, which is the only way to check that a notification arrives, that
it arrives on commit and not before, and that the driver hands it over. SQLite
cannot answer any of that, so on SQLite the module skips.

Point the suite at a server with `DJANGO_DATABASE_ENGINE`:

```bash
docker run -d --rm --name ddt-postgres \
    -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=django_database_task \
    -p 5432:5432 postgres:16

DJANGO_DATABASE_ENGINE=postgresql venv/bin/pytest
```

The connection details default to that container (`POSTGRES_HOST`,
`POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER` and `POSTGRES_PASSWORD`
override them). The whole suite runs there, so `SELECT FOR UPDATE SKIP LOCKED`
is exercised for real too.

CI runs this against both drivers Django supports, psycopg 3 and psycopg2,
because the broker reads notifications differently on each. To check the
psycopg2 path locally, install it in an environment that has no psycopg 3:
Django picks psycopg 3 whenever both are installed.

## Linting and formatting

```bash
venv/bin/ruff check .
venv/bin/ruff format .
```

Both have to pass; CI runs `ruff format --check`, which fails on unformatted
code rather than fixing it. Ruff is pinned in the `dev` extra so that everyone,
CI included, gets the same version — an unpinned install changes behaviour
between contributors.

## Making a change

A few things are easy to forget:

- **Migrations.** Changing `models.py` needs a migration, committed with it:

  ```bash
  venv/bin/python -m django makemigrations django_database_task --settings tests.settings
  ```

- **Translations.** User-visible strings in `models.py` and `admin.py` go
  through `gettext_lazy`, and there is a Japanese catalogue. Adding a string
  means updating and compiling it, from inside the app directory:

  ```bash
  cd django_database_task
  PYTHONPATH=.. ../venv/bin/python -m django makemessages -l ja --settings tests.settings
  # fill in the new msgstr, then
  PYTHONPATH=.. ../venv/bin/python -m django compilemessages --settings tests.settings
  ```

  Commit both `django.po` and `django.mo`. `makemessages` may also rewrite the
  `#:` source references; that is only bookkeeping.

- **The changelog.** Add a line to the *Unreleased* section of
  `CHANGELOG.md`. Leave the version in `pyproject.toml` alone; releases are cut
  separately.

- **The README.** New options, commands and endpoints are documented there.
  Its diagrams are mermaid, rendered by GitHub.

### Backwards compatibility

Projects depend on the settings, the URLs and the management commands, so a
change to any of them needs a path that keeps working for someone who upgrades
without reading the release notes. Where something has to go, deprecate it
first: keep it working with a `DeprecationWarning` that names the version it is
removed in, as `get_auth_handler()` does.

The tests are the safety net for this. When a refactor leaves the existing
tests passing unmodified, that is the evidence that behaviour did not change.

## Trying it against a real service

The demo project in `examples/` is the place to run a change end to end
against something other than the test suite. `examples/README.md` walks
through the database backend, and through the SQS broker with a local mock
standing in for AWS.

## Adding a broker

A broker tells an external service about a task that was saved. It carries
only the task id — the database stays the source of truth — and subclasses one
of the base classes in `django_database_task/brokers/base.py`:

| Base class | For |
|------------|-----|
| `HTTPPushBroker` | Services that call an HTTP endpoint of the application, like Cloud Tasks |
| `PullBroker` | Services a worker receives from, like SQS or PostgreSQL LISTEN/NOTIFY |
| `TaskBroker` | Anything else |

`django_database_task/sqs/` is the smallest complete example: a broker, a thin
backend that attaches it, environment detection and an extra in
`pyproject.toml`. A broker that needs a third-party client belongs behind an
extra of its own, with its tests guarded by `pytest.importorskip` and the extra
added to the CI install.

## Pull requests

- Branch off `main`.
- **One purpose per pull request.** See below.
- Say what the change is for. A description that explains the problem is worth
  more than one that restates the diff.
- Keep the tests and the linters green. CI runs the suite against Python 3.12
  to 3.14 and Django 6.0 and 6.1.
- Add tests for a behaviour change. A bug fix wants the test that fails
  without it.

### Split by purpose

A pull request should answer one question, so that a reviewer can hold that
question in their head while reading all of it. Several purposes in one branch
means none of them gets read properly, and the risky part hides among the safe
parts.

Split it when a branch contains any of these:

- **A refactor and the feature it makes possible.** Send the refactor first,
  where the existing tests passing unmodified is the whole argument, and build
  on it afterwards. Mixed together, that evidence is gone.
- **A fix for something you noticed on the way.** Worth having, and worth
  having on its own. The Cloud Tasks tests turning out never to have run came
  out of unrelated work and went in as its own pull request.
- **Mechanical churn beside a real change.** Reformatting, renaming, tidying
  imports and dependency bumps drown the few lines that matter.
- **Steps that make sense in sequence.** The 0.4 broker work went in as five
  pull requests — the authentication handlers, the broker abstraction, the
  worker option, the SQS broker, the documentation — each mergeable and
  reviewable on its own.

This holds however the change was produced, including with the help of an AI
tool. A branch that grew in one sitting still has to arrive as the sequence of
changes a reviewer can follow; splitting it afterwards is part of the work, not
an optional tidy-up.

A change that genuinely is one purpose can still be large, and that is fine.
The test is whether the description needs the word "and".
