"""Tests for SQSBroker."""

import json
from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone

from .conftest import make_task_result

# Skip all tests if boto3 is not installed
pytest.importorskip("boto3")


def make_broker(monkeypatch, client=None, **options):
    """Build a broker with a stubbed SQS client."""
    monkeypatch.setenv("AWS_REGION", "ap-northeast-1")

    from django_database_task.sqs import SQSBroker

    broker = SQSBroker(backend=None, options=options)
    broker._client = client if client is not None else MagicMock()
    broker._client.get_queue_url.return_value = {
        "QueueUrl": "https://sqs.ap-northeast-1.amazonaws.com/1/default"
    }
    return broker


class TestSQSBrokerInit:
    """Tests for the configuration the broker reads."""

    def test_detects_the_region(self, monkeypatch):
        assert make_broker(monkeypatch).region == "ap-northeast-1"

    def test_explicit_region_wins(self, monkeypatch):
        broker = make_broker(monkeypatch, AWS_REGION="us-east-1")

        assert broker.region == "us-east-1"

    def test_requires_a_region(self, monkeypatch):
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)

        from django_database_task.sqs import SQSBroker

        with pytest.raises(
            ImproperlyConfigured, match="Could not detect the AWS region"
        ):
            SQSBroker(backend=None, options={})

    def test_rejects_a_delay_beyond_what_sqs_accepts(self, monkeypatch):
        monkeypatch.setenv("AWS_REGION", "ap-northeast-1")

        from django_database_task.sqs import SQSBroker

        with pytest.raises(ImproperlyConfigured, match="cannot exceed 900"):
            SQSBroker(backend=None, options={"MAX_DELAY_SECONDS": 1800})


class TestSQSBrokerQueueUrl:
    """Tests for turning a queue name into a queue URL."""

    def test_looks_the_url_up(self, monkeypatch):
        broker = make_broker(monkeypatch)

        url = broker.get_queue_url("ranking")

        assert url == "https://sqs.ap-northeast-1.amazonaws.com/1/default"
        broker.client.get_queue_url.assert_called_once_with(QueueName="ranking")

    def test_the_url_is_looked_up_once_per_queue(self, monkeypatch):
        broker = make_broker(monkeypatch)

        broker.get_queue_url("ranking")
        broker.get_queue_url("ranking")
        broker.get_queue_url("agents")

        assert broker.client.get_queue_url.call_count == 2

    def test_a_template_avoids_the_lookup(self, monkeypatch):
        broker = make_broker(
            monkeypatch,
            SQS_QUEUE_URL_TEMPLATE="https://sqs.example.com/1/{queue_name}",
        )

        assert broker.get_queue_url("ranking") == "https://sqs.example.com/1/ranking"
        broker.client.get_queue_url.assert_not_called()

    def test_the_queue_name_is_used_as_it_is(self, monkeypatch):
        """No mapping is applied: the SQS queue is the Django queue."""
        assert make_broker(monkeypatch).resolve_queue("ranking") == "ranking"

    def test_close_forgets_the_urls_and_the_client(self, monkeypatch):
        broker = make_broker(monkeypatch)
        broker.get_queue_url("ranking")

        broker.close()

        assert broker._client is None
        assert broker._queue_urls == {}


class TestSQSBrokerEnqueue:
    """Tests for sending a message when a task is saved."""

    def test_sends_the_task_id(self, monkeypatch):
        broker = make_broker(monkeypatch)

        broker.notify(make_task_result(task_id="abc-123"))

        request = broker.client.send_message.call_args.kwargs
        assert json.loads(request["MessageBody"]) == {"task_id": "abc-123"}
        assert request["DelaySeconds"] == 0

    def test_sends_to_the_queue_of_the_task(self, monkeypatch):
        broker = make_broker(
            monkeypatch,
            SQS_QUEUE_URL_TEMPLATE="https://sqs.example.com/1/{queue_name}",
        )

        broker.notify(make_task_result(queue_name="ranking"))

        request = broker.client.send_message.call_args.kwargs
        assert request["QueueUrl"] == "https://sqs.example.com/1/ranking"

    def test_a_near_future_task_is_delayed(self, monkeypatch):
        broker = make_broker(monkeypatch)
        run_after = timezone.now() + timedelta(minutes=10)

        broker.notify(make_task_result(run_after=run_after))

        delay = broker.client.send_message.call_args.kwargs["DelaySeconds"]
        assert 590 <= delay <= 600

    def test_a_past_run_after_is_sent_without_delay(self, monkeypatch):
        broker = make_broker(monkeypatch)
        run_after = timezone.now() - timedelta(minutes=10)

        broker.notify(make_task_result(run_after=run_after))

        assert broker.client.send_message.call_args.kwargs["DelaySeconds"] == 0

    @pytest.mark.parametrize("seconds,sent", [(899, True), (901, False)])
    def test_the_delay_limit_decides_whether_to_send(self, monkeypatch, seconds, sent):
        broker = make_broker(monkeypatch)
        # A second of headroom, so the clock cannot cross the boundary.
        run_after = timezone.now() + timedelta(seconds=seconds + 1)

        broker.notify(make_task_result(run_after=run_after))

        assert broker.client.send_message.called is sent

    def test_a_far_future_task_is_left_in_the_database(self, monkeypatch, caplog):
        """SQS cannot hold it, so the database sweep has to run it."""
        broker = make_broker(monkeypatch)
        run_after = timezone.now() + timedelta(days=1)

        result = broker.notify(make_task_result(run_after=run_after))

        assert result is None
        broker.client.send_message.assert_not_called()

    def test_the_delay_limit_can_be_lowered(self, monkeypatch):
        broker = make_broker(monkeypatch, MAX_DELAY_SECONDS=60)
        run_after = timezone.now() + timedelta(minutes=5)

        broker.notify(make_task_result(run_after=run_after))

        broker.client.send_message.assert_not_called()


class TestSQSBrokerReceive:
    """Tests for receiving messages."""

    @staticmethod
    def _message(task_id="abc-123", handle="receipt-1", message_id="m1"):
        return {
            "MessageId": message_id,
            "ReceiptHandle": handle,
            "Body": json.dumps({"task_id": task_id}),
        }

    def test_returns_the_task_ids(self, monkeypatch):
        broker = make_broker(monkeypatch)
        broker.client.receive_message.return_value = {
            "Messages": [
                self._message("abc-123"),
                self._message("def-456", "receipt-2"),
            ]
        }

        messages = broker.receive(max_messages=2)

        assert [m.task_id for m in messages] == ["abc-123", "def-456"]
        assert messages[0].handle[1] == "receipt-1"

    def test_an_empty_queue_returns_nothing(self, monkeypatch):
        broker = make_broker(monkeypatch)
        broker.client.receive_message.return_value = {}

        assert broker.receive() == []

    def test_long_polls_the_queue_of_the_worker(self, monkeypatch):
        broker = make_broker(
            monkeypatch,
            SQS_QUEUE_URL_TEMPLATE="https://sqs.example.com/1/{queue_name}",
        )
        broker.client.receive_message.return_value = {}

        broker.receive(queue_name="ranking", max_messages=3, wait_seconds=15)

        request = broker.client.receive_message.call_args.kwargs
        assert request["QueueUrl"] == "https://sqs.example.com/1/ranking"
        assert request["MaxNumberOfMessages"] == 3
        assert request["WaitTimeSeconds"] == 15

    def test_the_default_queue_is_used_without_one(self, monkeypatch):
        broker = make_broker(
            monkeypatch,
            SQS_QUEUE_URL_TEMPLATE="https://sqs.example.com/1/{queue_name}",
        )
        broker.client.receive_message.return_value = {}

        broker.receive()

        request = broker.client.receive_message.call_args.kwargs
        assert request["QueueUrl"] == "https://sqs.example.com/1/default"

    def test_the_sqs_limits_are_applied(self, monkeypatch):
        """Asking for more than SQS allows is clamped, not an error."""
        broker = make_broker(monkeypatch)
        broker.client.receive_message.return_value = {}

        broker.receive(max_messages=50, wait_seconds=120)

        request = broker.client.receive_message.call_args.kwargs
        assert request["MaxNumberOfMessages"] == 10
        assert request["WaitTimeSeconds"] == 20

    def test_a_visibility_timeout_is_passed_on(self, monkeypatch):
        broker = make_broker(monkeypatch, VISIBILITY_TIMEOUT=300)
        broker.client.receive_message.return_value = {}

        broker.receive()

        assert (
            broker.client.receive_message.call_args.kwargs["VisibilityTimeout"] == 300
        )

    def test_the_queue_setting_is_used_without_one(self, monkeypatch):
        broker = make_broker(monkeypatch)
        broker.client.receive_message.return_value = {}

        broker.receive()

        assert "VisibilityTimeout" not in broker.client.receive_message.call_args.kwargs

    def test_a_message_that_is_not_json_is_dropped(self, monkeypatch, caplog):
        """It can never run, so it must not be handed over forever."""
        broker = make_broker(monkeypatch)
        broker.client.receive_message.return_value = {
            "Messages": [
                {"MessageId": "m1", "ReceiptHandle": "receipt-1", "Body": "not json"}
            ]
        }

        assert broker.receive() == []
        broker.client.delete_message.assert_called_once()
        assert "body is not JSON" in caplog.text

    def test_a_message_without_a_task_id_is_dropped(self, monkeypatch, caplog):
        broker = make_broker(monkeypatch)
        broker.client.receive_message.return_value = {
            "Messages": [
                {
                    "MessageId": "m1",
                    "ReceiptHandle": "receipt-1",
                    "Body": json.dumps({"something": "else"}),
                }
            ]
        }

        assert broker.receive() == []
        assert "no task_id" in caplog.text


class TestSQSBrokerAcknowledgement:
    """Tests for acknowledging and returning messages."""

    @staticmethod
    def _received(broker, handle="receipt-1"):
        broker.client.receive_message.return_value = {
            "Messages": [
                {
                    "MessageId": "m1",
                    "ReceiptHandle": handle,
                    "Body": json.dumps({"task_id": "abc-123"}),
                }
            ]
        }
        return broker.receive()[0]

    def test_ack_deletes_the_message(self, monkeypatch):
        broker = make_broker(monkeypatch)
        message = self._received(broker)

        broker.ack(message)

        broker.client.delete_message.assert_called_once_with(
            QueueUrl="https://sqs.ap-northeast-1.amazonaws.com/1/default",
            ReceiptHandle="receipt-1",
        )

    def test_a_failed_delete_is_logged_not_raised(self, monkeypatch, caplog):
        """The message reappears; the task status stops it running twice."""
        from botocore.exceptions import ClientError

        broker = make_broker(monkeypatch)
        message = self._received(broker)
        broker.client.delete_message.side_effect = ClientError({}, "DeleteMessage")

        broker.ack(message)

        assert "Failed to delete SQS message" in caplog.text

    def test_nack_makes_the_message_visible_again(self, monkeypatch):
        broker = make_broker(monkeypatch)
        message = self._received(broker)

        broker.nack(message)

        broker.client.change_message_visibility.assert_called_once_with(
            QueueUrl="https://sqs.ap-northeast-1.amazonaws.com/1/default",
            ReceiptHandle="receipt-1",
            VisibilityTimeout=0,
        )

    def test_nack_can_hold_the_message_back(self, monkeypatch):
        broker = make_broker(monkeypatch)
        message = self._received(broker)

        broker.nack(message, delay=30)

        request = broker.client.change_message_visibility.call_args.kwargs
        assert request["VisibilityTimeout"] == 30
