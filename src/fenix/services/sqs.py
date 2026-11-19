"""SQS queues: creation with DLQ, publish, receive, ACK and backoff."""

import json

from botocore.exceptions import BotoCoreError, ClientError

from fenix.config import aws, settings
from fenix.utils import state
from fenix.utils.errors import QueueUnavailableError

MAX_RECEIVE_COUNT = 3
VISIBILITY_SECONDS = 45
_urls = {}


def _fail(error, what):
    """Wrap a Boto3 error into a QueueUnavailableError.

    Args:
        error: The original ``ClientError`` or ``BotoCoreError``.
        what: Short name of the operation that failed.

    Returns:
        A ``QueueUnavailableError`` (the caller raises it).

    Raises:
        None.

    Notes:
        Only the exception type name is kept, never its message.
    """
    return QueueUnavailableError(f"SQS: falló {what} ({type(error).__name__})")


def ensure_queues(client=None):
    """Create (or reuse) the DLQ and the main queue with a redrive policy.

    Args:
        client: Optional SQS client (tests). Defaults to a LocalStack one.

    Returns:
        A dict with ``queue_url``, ``dlq_url`` and ``dlq_arn``.

    Raises:
        QueueUnavailableError: If SQS rejects or does not answer a call.

    Side Effects:
        Creates queues if missing, sets ``RedrivePolicy`` with
        ``maxReceiveCount=3`` and ``VisibilityTimeout=45``, and writes
        ``data/queues.json``. It is safe to run any number of times.
    """
    sqs = client or aws.get_client("sqs")
    main_name, dlq_name = settings.queue_names()
    try:
        dlq_url = sqs.create_queue(QueueName=dlq_name)["QueueUrl"]
        attrs = sqs.get_queue_attributes(
            QueueUrl=dlq_url, AttributeNames=["QueueArn"])["Attributes"]
        redrive = json.dumps({
            "deadLetterTargetArn": attrs["QueueArn"],
            "maxReceiveCount": str(MAX_RECEIVE_COUNT),
        })
        main_url = sqs.create_queue(QueueName=main_name)["QueueUrl"]
        sqs.set_queue_attributes(QueueUrl=main_url, Attributes={
            "RedrivePolicy": redrive,
            "VisibilityTimeout": str(VISIBILITY_SECONDS),
        })
    except (ClientError, BotoCoreError) as error:
        raise _fail(error, "ensure_queues") from error
    _urls.clear()
    info = {"queue_url": main_url, "dlq_url": dlq_url,
            "dlq_arn": attrs["QueueArn"]}
    state.write_json_atomic(settings.data_dir() / "queues.json", info)
    return info


def queue_url(kind="main", client=None):
    """Return the URL of a queue, asking SQS once and caching the answer.

    Args:
        kind: ``"main"`` or ``"dlq"``.
        client: Optional SQS client (tests).

    Returns:
        The queue URL exactly as the SDK reports it.

    Raises:
        QueueUnavailableError: If SQS fails or the queue does not exist
            (run ``init-queues`` first).
    """
    if kind in _urls:
        return _urls[kind]
    sqs = client or aws.get_client("sqs")
    main_name, dlq_name = settings.queue_names()
    name = main_name if kind == "main" else dlq_name
    try:
        _urls[kind] = sqs.get_queue_url(QueueName=name)["QueueUrl"]
    except (ClientError, BotoCoreError) as error:
        raise _fail(error, "get_queue_url (¿corriste init-queues?)") from error
    return _urls[kind]


def publish(event, client=None):
    """Store an already validated event in the main queue.

    Args:
        event: A validated event dict.
        client: Optional SQS client (tests).

    Returns:
        The SQS ``MessageId`` of the new message.

    Raises:
        QueueUnavailableError: If SQS does not confirm ``send_message``.
    """
    sqs = client or aws.get_client("sqs")
    body = json.dumps(event, sort_keys=True, separators=(",", ":"))
    return send_raw(body, sqs)


def send_raw(body, client=None):
    """Send plain text to the main queue, without any validation.

    Args:
        body: Text to use as the message body (can be garbage).
        client: Optional SQS client (tests).

    Returns:
        The SQS ``MessageId``.

    Raises:
        QueueUnavailableError: If SQS does not confirm ``send_message``.

    Notes:
        Used to simulate a poison message injected behind the receiver.
    """
    sqs = client or aws.get_client("sqs")
    try:
        url = queue_url("main", sqs)
        response = sqs.send_message(QueueUrl=url, MessageBody=body)
    except (ClientError, BotoCoreError) as error:
        _urls.clear()
        raise _fail(error, "send_message") from error
    return response["MessageId"]


def receive(client=None):
    """Poll the main queue once (long polling).

    Args:
        client: Optional SQS client (tests).

    Returns:
        A list with at most one message. An empty list means the queue had
        nothing available; it is not an error.

    Raises:
        QueueUnavailableError: If the transport or SQS fails.

    Side Effects:
        The received message becomes invisible for 45 seconds. It is NOT
        deleted.
    """
    sqs = client or aws.get_client("sqs")
    try:
        response = sqs.receive_message(
            QueueUrl=queue_url("main", sqs),
            MaxNumberOfMessages=1,
            WaitTimeSeconds=10,
            VisibilityTimeout=VISIBILITY_SECONDS,
            AttributeNames=["ApproximateReceiveCount"],
        )
    except (ClientError, BotoCoreError) as error:
        raise _fail(error, "receive_message") from error
    return response.get("Messages", [])


def ack(receipt_handle, client=None):
    """Delete a message: the work is confirmed.

    Args:
        receipt_handle: The ``ReceiptHandle`` of the latest receipt.
        client: Optional SQS client (tests).

    Returns:
        None.

    Raises:
        QueueUnavailableError: If SQS fails to delete the message.

    Notes:
        Call it only after every required step has been completed.
    """
    sqs = client or aws.get_client("sqs")
    try:
        sqs.delete_message(QueueUrl=queue_url("main", sqs),
                           ReceiptHandle=receipt_handle)
    except (ClientError, BotoCoreError) as error:
        raise _fail(error, "delete_message") from error


def retry_later(receipt_handle, receive_count, floor=0, client=None):
    """Schedule a retry by changing the message visibility (backoff).

    Args:
        receipt_handle: The ``ReceiptHandle`` of the latest receipt.
        receive_count: How many times SQS has delivered the message.
        floor: Minimum delay in seconds (for example ``Retry-After``).
            It is capped at 30.
        client: Optional SQS client (tests).

    Returns:
        The delay applied, ``min(30, max(2 ** receive_count, floor))``.

    Raises:
        QueueUnavailableError: If SQS fails to change the visibility.

    Notes:
        The original message is kept. Re-sending it as a new message would
        reset its receive count and it would never reach the DLQ.
    """
    delay = min(30, max(2 ** int(receive_count), min(int(floor), 30)))
    sqs = client or aws.get_client("sqs")
    try:
        sqs.change_message_visibility(
            QueueUrl=queue_url("main", sqs),
            ReceiptHandle=receipt_handle,
            VisibilityTimeout=delay,
        )
    except (ClientError, BotoCoreError) as error:
        raise _fail(error, "change_message_visibility") from error
    return delay


def peek_dlq(client=None):
    """Look at one message of the DLQ without deleting or hiding it.

    Args:
        client: Optional SQS client (tests).

    Returns:
        A list with at most one message.

    Raises:
        QueueUnavailableError: If SQS fails.

    Notes:
        It uses ``VisibilityTimeout=0`` so the message stays visible.
    """
    sqs = client or aws.get_client("sqs")
    try:
        response = sqs.receive_message(
            QueueUrl=queue_url("dlq", sqs),
            MaxNumberOfMessages=1,
            WaitTimeSeconds=2,
            VisibilityTimeout=0,
            AttributeNames=["ApproximateReceiveCount"],
        )
    except (ClientError, BotoCoreError) as error:
        raise _fail(error, "peek_dlq") from error
    return response.get("Messages", [])


def stats(client=None):
    """Read the approximate message counters of both queues.

    Args:
        client: Optional SQS client (tests).

    Returns:
        A dict ``{"main": {...}, "dlq": {...}}`` with the attributes
        ``ApproximateNumberOfMessages`` and
        ``ApproximateNumberOfMessagesNotVisible`` (strings).

    Raises:
        QueueUnavailableError: If SQS fails.

    Notes:
        The counters are approximate: never use them as the only evidence.
    """
    sqs = client or aws.get_client("sqs")
    names = ["ApproximateNumberOfMessages",
             "ApproximateNumberOfMessagesNotVisible"]
    result = {}
    try:
        for kind in ("main", "dlq"):
            attrs = sqs.get_queue_attributes(
                QueueUrl=queue_url(kind, sqs), AttributeNames=names)
            result[kind] = attrs["Attributes"]
    except (ClientError, BotoCoreError) as error:
        raise _fail(error, "get_queue_attributes") from error
    return result


def delete_queues(client=None):
    """Delete both queues.

    Args:
        client: Optional SQS client (tests).

    Returns:
        None.

    Raises:
        QueueUnavailableError: If SQS fails.

    Side Effects:
        Destroys the queues and every message in them. Document the DLQ
        first.
    """
    sqs = client or aws.get_client("sqs")
    try:
        for kind in ("main", "dlq"):
            sqs.delete_queue(QueueUrl=queue_url(kind, sqs))
    except (ClientError, BotoCoreError) as error:
        raise _fail(error, "delete_queue") from error
    _urls.clear()
