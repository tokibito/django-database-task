"""
Django settings for tests.
"""

SECRET_KEY = "test-secret-key-for-django-database-task"

DEBUG = True

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django_database_task",
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

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
