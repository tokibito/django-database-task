"""Tests for the translation catalogues."""

import gettext
from pathlib import Path

import pytest
from django.core.management import load_command_class
from django.utils import translation
from django.utils.text import capfirst

import django_database_task
from django_database_task.admin import DatabaseTaskAdmin
from django_database_task.models import DatabaseTask

LOCALE_DIR = Path(django_database_task.__file__).parent / "locale"

# language code -> (verbose_name_plural, the run_database_tasks help)
LANGUAGES = {
    "ja": ("データベースタスク", "データベースのキューに登録されたタスクを実行"),
    "zh-hans": ("数据库任务", "执行在数据库中排队的任务"),
    "pt-br": (
        "Tarefas do banco de dados",
        "Executa as tarefas enfileiradas no banco de dados",
    ),
    "es": (
        "Tareas de base de datos",
        "Ejecuta las tareas encoladas en la base de datos",
    ),
}


def load_catalog(locale):
    with open(LOCALE_DIR / locale / "LC_MESSAGES" / "django.mo", "rb") as f:
        return gettext.GNUTranslations(f)._catalog


@pytest.mark.parametrize("language", LANGUAGES)
def test_catalogue_is_complete(language):
    """Every catalogue translates every string the Japanese one does."""
    japanese = load_catalog("ja")
    catalog = load_catalog(translation.to_locale(language))

    assert set(catalog) == set(japanese)
    assert all(catalog.values())


@pytest.mark.parametrize("language", LANGUAGES)
def test_model_and_admin_are_translated(language):
    with translation.override(language):
        name = str(capfirst(DatabaseTask._meta.verbose_name_plural))
        action = str(DatabaseTaskAdmin.run_selected_tasks.short_description)

    assert name == LANGUAGES[language][0]
    assert action != "Run selected tasks"


@pytest.mark.parametrize("language", LANGUAGES)
def test_command_help_is_translated(language):
    command = load_command_class("django_database_task", "run_database_tasks")

    with translation.override(language):
        help_text = command.create_parser(
            "manage.py", "run_database_tasks"
        ).format_help()

    # argparse wraps the help, and between CJK characters a wrap leaves no space
    assert "".join(LANGUAGES[language][1].split()) in "".join(help_text.split())
