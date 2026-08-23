"""
Django settings for example_project.
"""

import os
import sys
from pathlib import Path

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# Add django_database_task to the path
sys.path.insert(0, str(BASE_DIR.parent))

SECRET_KEY = "example-secret-key-for-development-only"

DEBUG = True

ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_database_task",
    "demo_app",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "example_project.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "example_project.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

LANGUAGE_CODE = "ja"

TIME_ZONE = "Asia/Tokyo"

USE_I18N = True

USE_TZ = True

STATIC_URL = "static/"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Task backend settings
#
# The demo runs on the plain database backend by default. Set
# DEMO_BROKER=sqs to try the SQS broker against LocalStack instead; see
# the "Trying the SQS broker" section of examples/README.md.
DEMO_BROKER = os.environ.get("DEMO_BROKER", "database")

if DEMO_BROKER == "sqs":
    TASKS = {
        "default": {
            "BACKEND": "django_database_task.sqs.SQSDatabaseBackend",
            "QUEUES": [],
            "OPTIONS": {
                "AWS_REGION": os.environ.get("AWS_REGION", "ap-northeast-1"),
                # LocalStack, rather than the real SQS
                "SQS_ENDPOINT_URL": os.environ.get(
                    "SQS_ENDPOINT_URL", "http://localhost:4566"
                ),
            },
        },
    }
else:
    TASKS = {
        "default": {
            "BACKEND": "django_database_task.backends.DatabaseTaskBackend",
            "QUEUES": [],
            "OPTIONS": {},
        },
    }
