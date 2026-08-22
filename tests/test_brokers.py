"""Tests for the broker abstraction on DatabaseTaskBackend."""

import pytest
from django.core.exceptions import ImproperlyConfigured

from django_database_task.backends import DatabaseTaskBackend
from django_database_task.brokers import (
    BrokerMessage,
    HTTPPushBroker,
    PullBroker,
    TaskBroker,
)
from django_database_task.models import DatabaseTask
from tests.tasks import simple_task


class RecordingBroker(TaskBroker):
    """A broker that records what it was asked to enqueue."""

    def __init__(self, backend, options=None):
        super().__init__(backend, options)
        self.enqueued = []

    def enqueue(self, task_result):
        self.enqueued.append(task_result)


class FailingBroker(TaskBroker):
    """A broker that is always down."""

    def enqueue(self, task_result):
        raise RuntimeError("broker is down")


def make_backend(**options):
    return DatabaseTaskBackend(alias="default", params={"OPTIONS": options})


class TestBrokerSelection:
    """Tests for choosing the broker a backend notifies."""

    def test_no_broker_by_default(self):
        assert make_backend().broker is None

    def test_the_broker_option_names_a_broker(self):
        backend = make_backend(BROKER="tests.test_brokers.RecordingBroker")

        assert isinstance(backend.broker, RecordingBroker)

    def test_the_broker_option_accepts_a_class(self):
        backend = make_backend(BROKER=RecordingBroker)

        assert isinstance(backend.broker, RecordingBroker)

    def test_a_subclass_can_fix_its_broker(self):
        class BrokeredBackend(DatabaseTaskBackend):
            broker_class = RecordingBroker

        backend = BrokeredBackend(alias="default", params={})

        assert isinstance(backend.broker, RecordingBroker)

    def test_the_broker_option_wins_over_the_class(self):
        class BrokeredBackend(DatabaseTaskBackend):
            broker_class = FailingBroker

        backend = BrokeredBackend(
            alias="default", params={"OPTIONS": {"BROKER": RecordingBroker}}
        )

        assert isinstance(backend.broker, RecordingBroker)

    def test_the_broker_receives_the_backend_options(self):
        backend = make_backend(BROKER=RecordingBroker, SOMETHING="else")

        assert backend.broker.backend is backend
        assert backend.broker.options["SOMETHING"] == "else"

    def test_an_unimportable_broker_is_reported_when_the_backend_is_built(self):
        """A typo must fail loudly, not on the first enqueue."""
        with pytest.raises(ImproperlyConfigured, match="Could not import task broker"):
            make_backend(BROKER="does.not.exist.Broker")


@pytest.mark.django_db
class TestNotifyBroker:
    """Tests for notifying the broker when a task is saved."""

    def test_enqueue_notifies_the_broker(self):
        backend = make_backend(BROKER=RecordingBroker)

        result = backend.enqueue(simple_task, (1, 2), {})

        assert [r.id for r in backend.broker.enqueued] == [result.id]

    def test_the_task_is_saved_before_the_broker_is_told(self):
        """The broker only carries an id, so the row has to exist first."""
        seen = []

        class CheckingBroker(TaskBroker):
            def enqueue(self, task_result):
                seen.append(DatabaseTask.objects.filter(id=task_result.id).exists())

        backend = make_backend(BROKER=CheckingBroker)
        backend.enqueue(simple_task, (1, 2), {})

        assert seen == [True]

    def test_a_broker_failure_does_not_lose_the_task(self, caplog):
        """The database is the fallback when the broker is down."""
        backend = make_backend(BROKER=FailingBroker)

        result = backend.enqueue(simple_task, (1, 2), {})

        assert DatabaseTask.objects.get(id=result.id).status == "READY"
        assert "FailingBroker failed to enqueue task" in caplog.text
        assert "broker is down" in caplog.text

    def test_enqueue_works_without_a_broker(self):
        backend = make_backend()

        result = backend.enqueue(simple_task, (1, 2), {})

        assert DatabaseTask.objects.filter(id=result.id).exists()


class TestBrokerAuthHandlers:
    """Tests for the handlers a broker contributes."""

    def test_a_broker_contributes_no_handler_by_default(self):
        backend = make_backend(BROKER=RecordingBroker)

        assert backend.get_auth_handlers() == []

    def test_broker_handlers_come_before_the_configured_ones(self):
        def broker_handler(request):
            return None

        class AuthenticatingBroker(TaskBroker):
            def get_auth_handlers(self, endpoint=None):
                return [broker_handler]

        backend = make_backend(
            BROKER=AuthenticatingBroker,
            AUTH_HANDLERS=["django_database_task.auth.SharedSecretAuth"],
            AUTH_HANDLER_OPTIONS={"TOKEN": "s3cret"},
        )

        handlers = backend.get_auth_handlers()

        assert len(handlers) == 2
        assert handlers[0] is broker_handler

    def test_the_endpoint_is_passed_to_the_broker(self):
        seen = []

        class RecordingAuthBroker(TaskBroker):
            def get_auth_handlers(self, endpoint=None):
                seen.append(endpoint)
                return []

        make_backend(BROKER=RecordingAuthBroker).get_auth_handlers("execute")

        assert seen == ["execute"]


class TestBaseClasses:
    """Tests for the contract the base classes define."""

    def test_task_broker_requires_enqueue(self):
        with pytest.raises(NotImplementedError, match="must implement enqueue"):
            TaskBroker(backend=None).enqueue(None)

    def test_queue_names_pass_through_by_default(self):
        assert TaskBroker(backend=None).resolve_queue("ranking") == "ranking"

    def test_close_is_a_no_op_by_default(self):
        assert TaskBroker(backend=None).close() is None

    def test_pull_broker_requires_receive_and_ack(self):
        broker = PullBroker(backend=None)

        with pytest.raises(NotImplementedError, match="must implement receive"):
            broker.receive()
        with pytest.raises(NotImplementedError, match="must implement ack"):
            broker.ack(None)

    def test_nack_is_a_no_op_by_default(self):
        assert PullBroker(backend=None).nack(None) is None

    def test_broker_message_carries_the_task_id(self):
        message = BrokerMessage("abc-123", handle="receipt", raw={"a": 1})

        assert message.task_id == "abc-123"
        assert message.handle == "receipt"
        assert message.raw == {"a": 1}
        assert "abc-123" in repr(message)


class TestHTTPPushBroker:
    """Tests for the shared handler URL logic."""

    def test_uses_an_explicit_url(self):
        broker = HTTPPushBroker(
            backend=None,
            options={"TASK_HANDLER_URL": "https://example.com/t/{task_id}/"},
        )

        assert broker.get_handler_url("abc") == "https://example.com/t/abc/"

    def test_builds_the_url_from_the_detected_host(self):
        class DetectingBroker(HTTPPushBroker):
            def detect_handler_host(self):
                return "https://detected.example.com"

        broker = DetectingBroker(backend=None)

        assert broker.get_handler_url("abc") == (
            "https://detected.example.com/tasks/execute/abc/"
        )

    def test_uses_a_custom_path(self):
        class DetectingBroker(HTTPPushBroker):
            def detect_handler_host(self):
                return "https://detected.example.com"

        broker = DetectingBroker(
            backend=None, options={"TASK_HANDLER_PATH": "/x/{task_id}"}
        )

        assert broker.get_handler_url("abc") == "https://detected.example.com/x/abc"

    def test_reports_an_undetectable_host(self):
        broker = HTTPPushBroker(backend=None)

        with pytest.raises(ImproperlyConfigured, match="Set TASK_HANDLER_URL"):
            broker.get_handler_url("abc")
