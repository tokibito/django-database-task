from django.apps import AppConfig


class DemoAppConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "demo_app"
    verbose_name = "Demo Application"
