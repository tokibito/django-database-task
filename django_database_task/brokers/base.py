"""
Base classes for task brokers.

A broker notifies an external service that a task has been saved, so the
service can trigger its execution. The database stays the source of truth:
a broker only ever carries a task id, never the task's arguments or state.

Brokers come in two shapes:

- A push broker (Cloud Tasks, EventBridge Pipes) calls an HTTP endpoint of
  the Django application. See HTTPPushBroker.
- A pull broker (SQS) hands messages to a worker that polls it. See
  PullBroker.

A backend picks its broker with the broker_class attribute or the BROKER
option, and passes its whole OPTIONS dict to it, so each broker decides
which options it reads.
"""

from django.core.exceptions import ImproperlyConfigured


class BrokerMessage:
    """
    One message received from a pull broker.

    Attributes:
        task_id: Id of the task the message refers to.
        handle: Broker specific value the broker needs to acknowledge the
            message (an SQS receipt handle, for example).
        raw: The original message, for logging and debugging.
    """

    def __init__(self, task_id, handle=None, raw=None):
        self.task_id = task_id
        self.handle = handle
        self.raw = raw

    def __repr__(self):
        return f"<{type(self).__name__} task_id={self.task_id!r}>"


class TaskBroker:
    """
    Base class for brokers.

    Args:
        backend: The DatabaseTaskBackend the broker belongs to.
        options: The backend's OPTIONS dict.
    """

    def __init__(self, backend, options=None):
        self.backend = backend
        self.options = options or {}

    def notify(self, task_result):
        """
        Tell the external service about a task that was saved.

        Only the task id is sent. Raising is fine: the backend logs the
        failure and leaves the task in the database, where the worker or
        the HTTP endpoints can still pick it up.
        """
        raise NotImplementedError(f"{type(self).__name__} must implement notify().")

    def enqueue(self, task_result):
        """
        Notify the external service.

        .. deprecated:: 0.5
            Renamed to notify(). The backend still calls an enqueue()
            a broker overrides, with a DeprecationWarning; that stops
            working in 0.6.
        """
        return self.notify(task_result)

    # Marks the implementation above as this library's, so a broker that
    # overrides enqueue() can be told apart from one that inherits it.
    enqueue._is_library_notify = True

    def resolve_queue(self, queue_name):
        """
        Translate a Django queue name into the broker's queue identifier.

        The default is to use the name as it is.
        """
        return queue_name

    def get_auth_handlers(self, endpoint=None):
        """
        Get the handlers that authenticate requests coming from the broker.

        Returns:
            list of callables
        """
        return []

    def close(self):
        """Release whatever the broker holds open. Called by workers."""


class HTTPPushBroker(TaskBroker):
    """
    Base class for brokers that make the service call an HTTP endpoint.

    Options:
        TASK_HANDLER_URL   Full URL template, with a {task_id} placeholder.
        TASK_HANDLER_PATH  Path appended to the detected host when
                           TASK_HANDLER_URL is not set.
    """

    default_handler_path = "/tasks/execute/{task_id}/"

    def __init__(self, backend, options=None):
        super().__init__(backend, options)
        self.task_handler_url = self.options.get("TASK_HANDLER_URL")
        self.task_handler_path = self.options.get(
            "TASK_HANDLER_PATH", self.default_handler_path
        )

    def get_handler_url(self, task_id):
        """
        Get the full URL of the endpoint that executes the task.

        If TASK_HANDLER_URL is set, it is used directly. Otherwise the host
        is detected from the environment and TASK_HANDLER_PATH appended.
        """
        if self.task_handler_url:
            return self.task_handler_url.format(task_id=task_id)

        host = self.detect_handler_host()
        if not host:
            raise ImproperlyConfigured(
                "Could not detect task handler host from environment. "
                "Set TASK_HANDLER_URL explicitly in TASKS OPTIONS."
            )

        return f"{host}{self.task_handler_path.format(task_id=task_id)}"

    def detect_handler_host(self):
        """
        Detect the host the service should call, from the environment.

        Subclasses override this for the platform they run on. Returning
        None means TASK_HANDLER_URL has to be configured.
        """
        return None


class PullBroker(TaskBroker):
    """
    Base class for brokers a worker polls for messages.

    A worker calls receive() in a loop, executes each task, then ack()s the
    message. A message that is never acknowledged is delivered again, so
    the worker can crash without losing the task.
    """

    def receive(self, queue_name=None, max_messages=1, wait_seconds=20):
        """
        Wait for messages and return them.

        Args:
            queue_name: Django queue to read from, or None for the
                broker's default. Pass it through resolve_queue().
            max_messages: How many messages to return at most.
            wait_seconds: How long to wait for a message before giving up.
                Zero polls without waiting.

        Returns:
            list of BrokerMessage
        """
        raise NotImplementedError(f"{type(self).__name__} must implement receive().")

    def ack(self, message):
        """Tell the broker the message is dealt with, so it is not resent."""
        raise NotImplementedError(f"{type(self).__name__} must implement ack().")

    def nack(self, message, delay=None):
        """
        Give the message back to the broker so it is delivered again.

        Args:
            message: The message to return.
            delay: Seconds to wait before the message becomes visible
                again, if the broker supports it.
        """
