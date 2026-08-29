"""
Amazon SQS broker.

Sends a message holding the task id whenever a task is saved, and lets a
worker receive those messages with run_database_tasks. Everything else
about the task stays in the database.
"""

import json
import logging

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from django.core.exceptions import ImproperlyConfigured

from django_database_task.brokers import BrokerMessage, PullBroker

from .detection import detect_aws_region

logger = logging.getLogger(__name__)

#: The longest delay SQS accepts on a message, in seconds.
SQS_MAX_DELAY_SECONDS = 900

#: The most messages a single ReceiveMessage call may return.
SQS_MAX_RECEIVE_COUNT = 10

#: The longest a receive call may wait for a message, in seconds.
SQS_MAX_WAIT_SECONDS = 20


class SQSBroker(PullBroker):
    """
    Broker backed by Amazon SQS.

    Requires: pip install django-database-task[sqs]

    The SQS queue name is the task's queue_name attribute, so
    @task(queue_name="ranking") is sent to the "ranking" queue, matching
    how the Cloud Tasks broker behaves.

    Options:
        AWS_REGION              Region. Detected from AWS_REGION or
                                AWS_DEFAULT_REGION when unset.
        SQS_QUEUE_URL_TEMPLATE  Queue URL with a {queue_name} placeholder.
                                Set it to skip the GetQueueUrl call, which
                                also saves needing sqs:GetQueueUrl.
        SQS_ENDPOINT_URL        Endpoint override, for LocalStack.
        VISIBILITY_TIMEOUT      Seconds a received message stays hidden.
                                Leave unset to use the queue's own setting;
                                it has to outlast the longest task.
        MAX_DELAY_SECONDS       Largest delay to put on a message
                                (default: 900, the SQS limit).

    Deferred tasks:
        SQS cannot hold a message for longer than 15 minutes, so a task
        with a run_after further out is left in the database instead of
        being sent. Run the worker with --source both (the default when a
        broker is configured) so the database sweep picks it up when due.
    """

    def __init__(self, backend, options=None):
        super().__init__(backend, options)

        self.region = self.options.get("AWS_REGION") or detect_aws_region()
        if not self.region:
            raise ImproperlyConfigured(
                "Could not detect the AWS region. "
                "Set AWS_REGION in TASKS OPTIONS or "
                "ensure the AWS_REGION environment variable is set."
            )

        self.queue_url_template = self.options.get("SQS_QUEUE_URL_TEMPLATE")
        self.endpoint_url = self.options.get("SQS_ENDPOINT_URL")
        self.visibility_timeout = self.options.get("VISIBILITY_TIMEOUT")

        max_delay = self.options.get("MAX_DELAY_SECONDS", SQS_MAX_DELAY_SECONDS)
        if max_delay > SQS_MAX_DELAY_SECONDS:
            raise ImproperlyConfigured(
                f"MAX_DELAY_SECONDS cannot exceed {SQS_MAX_DELAY_SECONDS}, "
                f"the longest delay SQS accepts (got {max_delay})."
            )
        self.max_delay_seconds = max_delay

        self._client = None
        self._queue_urls = {}

    @property
    def client(self):
        """Lazy initialization of the SQS client."""
        if self._client is None:
            self._client = boto3.client(
                "sqs",
                region_name=self.region,
                endpoint_url=self.endpoint_url,
            )
        return self._client

    def close(self):
        self._client = None
        self._queue_urls.clear()

    def get_queue_url(self, queue_name):
        """
        Get the URL of the SQS queue a Django queue name refers to.

        Looked up once per queue and then kept, since the URL of a queue
        does not change.
        """
        queue_name = self.resolve_queue(queue_name)

        if queue_name not in self._queue_urls:
            if self.queue_url_template:
                url = self.queue_url_template.format(queue_name=queue_name)
            else:
                url = self.client.get_queue_url(QueueName=queue_name)["QueueUrl"]
            self._queue_urls[queue_name] = url

        return self._queue_urls[queue_name]

    def get_delay_seconds(self, task_result):
        """
        Work out the delay to put on the message.

        Returns None when the task is deferred further out than SQS can
        hold a message, meaning it should not be sent at all.
        """
        run_after = task_result.task.run_after
        if not run_after:
            return 0

        from django.utils import timezone

        delay = (run_after - timezone.now()).total_seconds()
        if delay <= 0:
            return 0
        if delay > self.max_delay_seconds:
            return None
        return int(delay)

    def notify(self, task_result):
        """
        Send a message naming a task that was just saved.

        A task deferred beyond what SQS can hold is left for the database
        sweep instead.
        """
        delay = self.get_delay_seconds(task_result)
        if delay is None:
            logger.debug(
                "Task %s runs after the %ss SQS delay limit; leaving it in the "
                "database for the worker to pick up when it is due",
                task_result.id,
                self.max_delay_seconds,
            )
            return None

        response = self.client.send_message(
            QueueUrl=self.get_queue_url(task_result.task.queue_name),
            MessageBody=json.dumps({"task_id": str(task_result.id)}),
            DelaySeconds=delay,
        )

        logger.debug("Sent SQS message %s", response.get("MessageId"))
        return response

    def receive(self, queue_name=None, max_messages=1, wait_seconds=20):
        """
        Long poll the queue and return the messages it hands over.

        SQS accepts at most 10 messages and 20 seconds of waiting per
        call, so larger values are clamped rather than rejected.
        """
        from django.tasks import DEFAULT_TASK_QUEUE_NAME

        request = {
            "QueueUrl": self.get_queue_url(queue_name or DEFAULT_TASK_QUEUE_NAME),
            "MaxNumberOfMessages": max(1, min(max_messages, SQS_MAX_RECEIVE_COUNT)),
            "WaitTimeSeconds": max(0, min(int(wait_seconds), SQS_MAX_WAIT_SECONDS)),
        }
        if self.visibility_timeout is not None:
            request["VisibilityTimeout"] = self.visibility_timeout

        response = self.client.receive_message(**request)

        messages = []
        for raw in response.get("Messages", []):
            task_id = self._read_task_id(raw)
            if task_id is None:
                # Nothing here can run this message; drop it rather than
                # let SQS hand it back forever.
                self._delete(request["QueueUrl"], raw.get("ReceiptHandle"))
                continue
            messages.append(
                BrokerMessage(
                    task_id,
                    handle=(request["QueueUrl"], raw["ReceiptHandle"]),
                    raw=raw,
                )
            )

        return messages

    def ack(self, message):
        """Delete the message, so SQS does not hand it over again."""
        queue_url, receipt_handle = message.handle
        self._delete(queue_url, receipt_handle)

    def nack(self, message, delay=None):
        """
        Make the message visible again, so it is delivered another time.

        Args:
            delay: Seconds to keep it hidden first. Zero, the default,
                returns it straight away.
        """
        queue_url, receipt_handle = message.handle
        self.client.change_message_visibility(
            QueueUrl=queue_url,
            ReceiptHandle=receipt_handle,
            VisibilityTimeout=max(0, min(int(delay or 0), SQS_MAX_DELAY_SECONDS)),
        )

    def _read_task_id(self, raw):
        """Read the task id out of a message body, or None if it has none."""
        try:
            body = json.loads(raw.get("Body", ""))
        except ValueError:
            logger.error(
                "Discarding SQS message %s: body is not JSON",
                raw.get("MessageId"),
            )
            return None

        task_id = body.get("task_id") if isinstance(body, dict) else None
        if not task_id:
            logger.error(
                "Discarding SQS message %s: no task_id in the body",
                raw.get("MessageId"),
            )
            return None

        return str(task_id)

    def _delete(self, queue_url, receipt_handle):
        if not receipt_handle:
            return
        try:
            self.client.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt_handle)
        except (BotoCoreError, ClientError):
            # The message reappears and is guarded against running twice
            # by the task status and the row lock.
            logger.exception("Failed to delete SQS message from %s", queue_url)
