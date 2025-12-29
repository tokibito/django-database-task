"""demo_app URL configuration."""

from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="index"),
    path("enqueue/add/", views.enqueue_add, name="enqueue_add"),
    path("enqueue/email/", views.enqueue_email, name="enqueue_email"),
    path("enqueue/process/", views.enqueue_process, name="enqueue_process"),
    path("enqueue/failing/", views.enqueue_failing, name="enqueue_failing"),
    path("enqueue/priority/", views.enqueue_priority, name="enqueue_priority"),
    path("enqueue/delayed/", views.enqueue_delayed, name="enqueue_delayed"),
    path("enqueue/context/", views.enqueue_context, name="enqueue_context"),
    path("enqueue/newsletter/", views.enqueue_newsletter, name="enqueue_newsletter"),
    path("result/<uuid:task_id>/", views.result, name="result"),
    path("tasks/json/", views.task_list_json, name="task_list_json"),
]
