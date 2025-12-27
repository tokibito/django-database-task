import os

import django
import pytest


def pytest_configure():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.settings")
    django.setup()


@pytest.fixture
def db_setup(db):
    """Database setup fixture."""
    pass
