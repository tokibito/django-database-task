"""Sample task definitions."""

import time

from django.tasks import task


@task
def add_numbers(x, y):
    """Add two numbers together."""
    return x + y


@task
def send_email_task(to, subject, body):
    """Simulate sending an email (does not actually send)."""
    time.sleep(1)  # Simulate sending
    return f"Email sent to {to}: {subject}"


@task
def process_data(data_size=100):
    """Simulate data processing."""
    result = []
    for i in range(data_size):
        result.append(i * 2)
        if i % 10 == 0:
            time.sleep(0.1)  # Simulate processing time
    return {"processed": len(result), "sum": sum(result)}


@task
def failing_task():
    """A task that intentionally fails."""
    raise ValueError("This task is designed to fail!")


@task(priority=50)
def high_priority_report(report_name):
    """High priority report generation task."""
    time.sleep(0.5)
    return f"High priority report '{report_name}' generated"


@task(priority=-50)
def low_priority_cleanup():
    """Low priority cleanup task."""
    time.sleep(0.5)
    return "Cleanup completed"


@task(queue_name="emails")
def newsletter_task(subscriber_count):
    """Newsletter task for the email queue."""
    time.sleep(subscriber_count * 0.01)
    return f"Newsletter sent to {subscriber_count} subscribers"


@task(takes_context=True)
def context_aware_task(context, message):
    """A task that uses task context."""
    task_id = context.task_result.id
    attempt = context.attempt
    return f"Task {task_id} (attempt {attempt}): {message}"
