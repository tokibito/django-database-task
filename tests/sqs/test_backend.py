"""Tests for SQSDatabaseBackend."""

import json
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.tasks.base import TaskResultStatus

from django_database_task.models import DatabaseTask
from tests.tasks import simple_task

# Skip all tests if boto3 is not installed
pytest.importorskip("boto3")

QUEUE_URL = "https://sqs.ap-northeast-1.amazonaws.com/1/default"


@pytest.fixture
def backend(monkeypatch):
    """A backend whose SQS client is a stub."""
    monkeypatch.setenv("AWS_REGION", "ap-northeast-1")

    from django_database_task.sqs import SQSDatabaseBackend

    backend = SQSDatabaseBackend(
        "default",
        {"QUEUES": [], "OPTIONS": {"SQS_QUEUE_URL_TEMPLATE": QUEUE_URL}},
    )
    backend.broker._client = MagicMock()
    backend.broker._client.receive_message.return_value = {}
    return backend


class TestSQSDatabaseBackend:
    """Tests for the backend wiring."""

    def test_it_uses_the_sqs_broker(self, backend):
        from django_database_task.sqs import SQSBroker

        assert isinstance(backend.broker, SQSBroker)

    def test_it_adds_no_authentication(self, backend):
        """Nothing calls back over HTTP, so there is nothing to verify."""
        assert backend.get_auth_handlers() == []

    def test_options_reach_the_broker(self, backend):
        assert backend.broker.queue_url_template == QUEUE_URL


@pytest.mark.django_db
class TestSQSDatabaseBackendEnqueue:
    """Tests for saving a task and telling SQS about it."""

    def test_enqueue_saves_and_sends(self, backend):
        result = backend.enqueue(simple_task, (2, 3), {})

        assert DatabaseTask.objects.get(id=result.id).status == TaskResultStatus.READY
        request = backend.broker._client.send_message.call_args.kwargs
        assert json.loads(request["MessageBody"]) == {"task_id": str(result.id)}

    def test_a_send_failure_does_not_lose_the_task(self, backend, caplog):
        backend.broker._client.send_message.side_effect = RuntimeError("SQS is down")

        result = backend.enqueue(simple_task, (2, 3), {})

        assert DatabaseTask.objects.get(id=result.id).status == TaskResultStatus.READY
        assert "SQSBroker failed to enqueue task" in caplog.text


@pytest.mark.django_db
class TestSQSWorker:
    """Tests for the worker running what SQS hands it."""

    @staticmethod
    def _run(backend, **options):
        out = StringIO()
        with patch.dict(
            "django_database_task.management.commands.run_database_tasks.task_backends",
            {"default": backend},
            clear=False,
        ):
            call_command("run_database_tasks", stdout=out, stderr=out, **options)
        return out.getvalue()

    def test_the_default_source_receives_from_sqs(self, backend):
        """A configured broker turns the usual command into an SQS worker."""
        output = self._run(backend)

        assert "Source: both" in output
        assert "Broker: SQSBroker" in output

    def test_a_message_runs_its_task_and_is_deleted(self, backend):
        result = backend.enqueue(simple_task, (2, 3), {})
        backend.broker._client.receive_message.side_effect = [
            {
                "Messages": [
                    {
                        "MessageId": "m1",
                        "ReceiptHandle": "receipt-1",
                        "Body": json.dumps({"task_id": str(result.id)}),
                    }
                ]
            },
            {},
            {},
        ]

        # The worker closes the broker on the way out, dropping the client.
        client = backend.broker._client

        self._run(backend)

        db_task = DatabaseTask.objects.get(id=result.id)
        assert db_task.status == TaskResultStatus.SUCCESSFUL
        assert db_task.return_value_json == 5
        client.delete_message.assert_called_once_with(
            QueueUrl=QUEUE_URL, ReceiptHandle="receipt-1"
        )
        assert backend.broker._client is None

    def test_a_deferred_task_is_run_from_the_database(self, backend):
        """What SQS could not carry is picked up by the database sweep."""
        from datetime import timedelta

        from django.utils import timezone

        result = simple_task.using(
            run_after=timezone.now() - timedelta(seconds=1)
        ).enqueue(4, 4)
        DatabaseTask.objects.filter(id=result.id).update(backend_name="default")
        backend.broker._client.send_message.reset_mock()

        self._run(backend)

        assert DatabaseTask.objects.get(id=result.id).status == (
            TaskResultStatus.SUCCESSFUL
        )
