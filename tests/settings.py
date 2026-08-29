"""
Django settings for tests.
"""

import os

SECRET_KEY = "test-secret-key-for-django-database-task"

DEBUG = True

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.admin",
    "django.contrib.messages",
    "django.contrib.sessions",
    "django_database_task",
]

MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]


def database_from_env():
    """
    Build the database the suite runs against.

    In-memory SQLite by default, so `pytest` needs no server. Setting
    DJANGO_DATABASE_ENGINE to "postgresql" points the suite at a real
    PostgreSQL instead, which is the only way to exercise LISTEN/NOTIFY
    and `SELECT FOR UPDATE SKIP LOCKED` against the thing they talk to.
    CI runs the suite both ways.
    """
    engine = os.environ.get("DJANGO_DATABASE_ENGINE", "sqlite3")

    if engine in ("sqlite", "sqlite3"):
        return {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": ":memory:",
        }

    if engine in ("postgres", "postgresql"):
        return {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("POSTGRES_DB", "django_database_task"),
            "USER": os.environ.get("POSTGRES_USER", "postgres"),
            "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "postgres"),
            "HOST": os.environ.get("POSTGRES_HOST", "127.0.0.1"),
            "PORT": os.environ.get("POSTGRES_PORT", "5432"),
        }

    raise ValueError(
        f"DJANGO_DATABASE_ENGINE is {engine!r}; expected 'sqlite3' or 'postgresql'."
    )


DATABASES = {"default": database_from_env()}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

USE_TZ = True

TIME_ZONE = "UTC"

TASKS = {
    "default": {
        "BACKEND": "django_database_task.backends.DatabaseTaskBackend",
        "QUEUES": [],
        "OPTIONS": {},
    },
}

ROOT_URLCONF = "tests.urls"
