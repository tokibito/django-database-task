"""
Tests for SQSBroker against a running SQS implementation.

test_broker.py stubs the boto3 client, which covers the requests the broker
builds but not what a queue does with them: whether a sent message comes
back, whether ack() stops the redelivery and leaving it unacked brings it
back, whether the delay the broker asks for is honoured, whether the
clamping it does is the clamping SQS needs. moto answers those in-process,
with no AWS account and no network.

The stubbed tests stay: they cover the cases a queue will not produce on
demand, like a client that fails.
"""

import json
import time
from datetime import timedelta

import pytest
from django.utils import timezone

from tests.tasks import simple_task

from .conftest import make_task_result

pytest.importorskip("boto3")
moto_server = pytest.importorskip(
    "moto.server", reason="needs moto: pip install -e '.[dev]'"
)

#: Visibility timeout the redelivery tests run with. Short, because a test
#: that waits for it to expire waits this long for real.
VISIBILITY_TIMEOUT = 1

#: How long a test waits for a message it expects. Never reached when the
#: message is there, which is the case these tests are written for.
WAIT = 5

#: Region moto is addressed with. No request leaves the machine.
REGION = "us-east-1"


@pytest.fixture(scope="session")
def sqs_endpoint():
    """A moto server standing in for SQS, for the whole session."""
    server = moto_server.ThreadedMotoServer(port=0)
    server.start()
    host, port = server.get_host_and_port()
    try:
        yield f"http://{host}:{port}"
    finally:
        server.stop()


@pytest.fixture(autouse=True)
def aws_credentials(monkeypatch):
    """Credentials boto3 insists on having, which moto does not check."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_REGION", REGION)


@pytest.fixture
def sqs(sqs_endpoint):
    """A client on the queues the tests use, emptied around each test."""
    import boto3

    client = boto3.client("sqs", region_name=REGION, endpoint_url=sqs_endpoint)
    urls = {
        name: client.create_queue(QueueName=name)["QueueUrl"]
        for name in ("default", "ranking")
    }
    try:
        yield client
    finally:
        # Deleted rather than purged: a queue starts empty, and no test
        # inherits what another one left behind.
        for url in urls.values():
            client.delete_queue(QueueUrl=url)


@pytest.fixture
def broker(sqs, sqs_endpoint):
    """A broker talking to the moto server."""
    from django_database_task.sqs import SQSBroker

    broker = SQSBroker(
        backend=None,
        options={
            "AWS_REGION": REGION,
            "SQS_ENDPOINT_URL": sqs_endpoint,
            "VISIBILITY_TIMEOUT": VISIBILITY_TIMEOUT,
        },
    )
    try:
        yield broker
    finally:
        broker.close()


def task_ids(messages):
    return [message.task_id for message in messages]


class TestDelivery:
    """Tests for a message travelling from enqueue() to receive()."""

    def test_a_sent_message_comes_back(self, broker):
        broker.notify(make_task_result(task_id="abc-123"))

        assert task_ids(broker.receive(wait_seconds=WAIT)) == ["abc-123"]

    def test_the_body_carries_the_task_id_and_nothing_else(self, broker):
        broker.notify(make_task_result(task_id="abc-123"))

        message = broker.receive(wait_seconds=WAIT)[0]

        assert json.loads(message.raw["Body"]) == {"task_id": "abc-123"}

    def test_nothing_comes_back_from_an_empty_queue(self, broker):
        assert broker.receive(wait_seconds=0) == []

    def test_each_queue_keeps_its_own_messages(self, broker):
        broker.notify(make_task_result(task_id="ranked", queue_name="ranking"))

        assert broker.receive(queue_name="default", wait_seconds=0) == []
        assert task_ids(broker.receive(queue_name="ranking", wait_seconds=WAIT)) == [
            "ranked"
        ]

    def test_it_finds_a_queue_by_name(self, broker, sqs):
        expected = sqs.get_queue_url(QueueName="ranking")["QueueUrl"]

        assert broker.get_queue_url("ranking") == expected

    def test_a_queue_that_does_not_exist_is_an_error(self, broker):
        from botocore.exceptions import ClientError

        with pytest.raises(ClientError):
            broker.get_queue_url("no-such-queue")


class TestAcknowledgement:
    """Tests for what a queue does with a message the worker took."""

    def test_an_acked_message_is_not_handed_over_again(self, broker):
        broker.notify(make_task_result(task_id="acked"))

        broker.ack(broker.receive(wait_seconds=WAIT)[0])
        time.sleep(VISIBILITY_TIMEOUT + 0.5)

        assert broker.receive(wait_seconds=0) == []

    def test_a_message_the_worker_died_on_comes_back(self, broker):
        """Nothing acknowledges it, so the visibility timeout returns it."""
        broker.notify(make_task_result(task_id="unacked"))

        broker.receive(wait_seconds=WAIT)
        time.sleep(VISIBILITY_TIMEOUT + 0.5)

        assert task_ids(broker.receive(wait_seconds=WAIT)) == ["unacked"]

    def test_nack_hands_it_back_at_once(self, broker):
        broker.notify(make_task_result(task_id="nacked"))

        broker.nack(broker.receive(wait_seconds=WAIT)[0])

        assert task_ids(broker.receive(wait_seconds=WAIT)) == ["nacked"]

    def test_a_message_no_worker_can_run_is_dropped(self, broker, sqs):
        """Otherwise the queue hands the same unusable message over forever."""
        sqs.send_message(
            QueueUrl=broker.get_queue_url("default"), MessageBody="not json"
        )

        assert broker.receive(wait_seconds=WAIT) == []

        time.sleep(VISIBILITY_TIMEOUT + 0.5)
        assert broker.receive(wait_seconds=0) == []


class TestDeferredTasks:
    """Tests for a task that is not due yet."""

    def test_a_delayed_task_is_held_back_and_then_delivered(self, broker):
        run_after = timezone.now() + timedelta(seconds=2)

        broker.notify(make_task_result(task_id="delayed", run_after=run_after))

        assert broker.receive(wait_seconds=0) == []
        assert task_ids(broker.receive(wait_seconds=WAIT)) == ["delayed"]

    def test_a_task_deferred_past_the_limit_is_never_sent(self, broker):
        run_after = timezone.now() + timedelta(hours=1)

        assert broker.notify(make_task_result(run_after=run_after)) is None
        assert broker.receive(wait_seconds=0) == []


class TestLimits:
    """Tests for the values SQS refuses, which the broker clamps."""

    def test_a_wait_longer_than_sqs_allows_is_accepted(self, broker):
        """SQS rejects WaitTimeSeconds above 20."""
        broker.notify(make_task_result(task_id="clamped-wait"))

        assert task_ids(broker.receive(wait_seconds=600)) == ["clamped-wait"]

    def test_asking_for_more_messages_than_sqs_allows_is_accepted(self, broker):
        """SQS rejects MaxNumberOfMessages above 10."""
        broker.notify(make_task_result(task_id="clamped-count"))

        assert task_ids(broker.receive(max_messages=100, wait_seconds=WAIT)) == [
            "clamped-count"
        ]


@pytest.mark.django_db
class TestBackend:
    """Tests for the backend that attaches the broker."""

    def test_saving_a_task_sends_a_message(self, broker, sqs_endpoint):
        from django_database_task.sqs import SQSDatabaseBackend

        backend = SQSDatabaseBackend(
            alias="default",
            params={
                "OPTIONS": {
                    "AWS_REGION": REGION,
                    "SQS_ENDPOINT_URL": sqs_endpoint,
                }
            },
        )

        result = backend.enqueue(simple_task, (1, 2), {})

        assert task_ids(broker.receive(wait_seconds=WAIT)) == [str(result.id)]
