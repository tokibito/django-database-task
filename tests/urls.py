"""URL configuration for tests."""

from django.urls import include, path

urlpatterns = [
    path("tasks/", include("django_database_task.urls")),
]
