"""Worker: consume SQS, process with ``handle`` and ACK at the very end."""

import json
import time

from fenix.services import events, remediation, sqs
from fenix.utils import state
from fenix.utils.errors import (ConflictError, FenixError, PermanentError,
                                QueueUnavailableError, TransientError,
                                ValidationError)
from fenix.utils.logging_json import get_logger

log = get_logger("worker")


def parse_body(body):
    """Decode a message body into a Python object.

    Args:
        body: The ``Body`` text of an SQS message.

    Returns:
        Whatever the JSON contains (dict, list, ...). Contract validation
        happens later in ``events.validate``.

    Raises:
        ValidationError: If the body is not valid JSON.
    """
    try:
        return json.loads(body)
    except (json.JSONDecodeError, RecursionError) as error:
        raise ValidationError("el cuerpo no es JSON válido") from error


def _fail(public, receipt, error, kind, floor=0, detail=True):
    """Log a failure and schedule the retry. It NEVER acknowledges.

    Args:
        public: Logging context (message id, attempt, event id...).
        receipt: ``ReceiptHandle`` of the current receipt.
        error: The exception that caused the failure.
        kind: ``"transient"``, ``"permanent"`` or ``"unexpected"``.
        floor: Minimum backoff in seconds (for example ``Retry-After``).
        detail: If False, the error text is hidden (unknown exceptions).

    Returns:
        None.

    Raises:
        None.

    Side Effects:
        Changes the message visibility through ``sqs.retry_later``. If that
        fails, it only logs: the message stays un-acknowledged and will
        come back by itself.
    """
    level = log.warning if kind == "transient" else log.error
    reason = str(error) if detail else "error inesperado"
    level(f"{type(error).__name__}: {reason}",
          extra={**public, "action": "process", "status": "failed",
                 "kind": kind, "error_type": type(error).__name__})
    try:
        delay = sqs.retry_later(receipt, public["attempt"], floor)
    except QueueUnavailableError as problem:
        log.error(str(problem), extra={**public, "action": "backoff",
                                       "status": "not_scheduled"})
        return
    last = public["attempt"] >= sqs.MAX_RECEIVE_COUNT
    note = " (último intento: SQS lo derivará a la DLQ)" if last else ""
    log.info("reintento programado" + note,
             extra={**public, "action": "backoff", "status": "scheduled",
                    "delay": delay})


def process_message(message):
    """Process one SQS message from start to finish.

    Args:
        message: A message dict as returned by ``receive_message``.

    Returns:
        ``"done"`` or ``"already_done"`` on success, ``"failed"`` when the
        message was kept for a retry, ``"ack_failed"`` if the work was done
        but SQS could not delete the message.

    Raises:
        NotImplementedError: Re-raised on purpose, so an unfinished
            template is visible instead of silently ignored.

    Side Effects:
        Calls ``sqs.ack`` ONLY after ``remediation.handle`` returned
        without errors. Business failures never stop the service.
    """
    receipt = message["ReceiptHandle"]
    attrs = message.get("Attributes", {})
    public = {"message_id": message.get("MessageId"),
              "attempt": int(attrs.get("ApproximateReceiveCount", 1))}
    try:
        data = parse_body(message["Body"])
        event = events.validate(data, state.load_inventory())
        public["event_id"] = event["event_id"]
        public["resource_id"] = event["resource_id"]
        outcome = remediation.handle(
            event, public["attempt"], public["message_id"])
    except (ValidationError, ConflictError, PermanentError) as error:
        _fail(public, receipt, error, "permanent")
        return "failed"
    except TransientError as error:
        _fail(public, receipt, error, "transient", floor=error.retry_after)
        return "failed"
    except NotImplementedError:
        raise  # unfinished template: show it, never hide it
    except FenixError as error:
        _fail(public, receipt, error, "transient")
        return "failed"
    except Exception as error:  # a weird message must not kill the service
        _fail(public, receipt, error, "unexpected", detail=False)
        return "failed"
    try:
        sqs.ack(receipt)
    except QueueUnavailableError as problem:
        log.error(str(problem), extra={**public, "action": "ack",
                                       "status": "failed"})
        return "ack_failed"
    log.info("mensaje confirmado", extra={**public, "action": "ack",
                                          "status": "acknowledged"})
    return outcome


def run(once=False):
    """Main worker loop.

    Args:
        once: If True, poll a single time and return (``--once`` mode).

    Returns:
        None.

    Raises:
        NotImplementedError: Propagated from ``process_message``.
        KeyboardInterrupt: When the user presses Ctrl+C (handled by the
            CLI, which stops the logging in a ``finally`` block).

    Side Effects:
        Long-polls SQS and processes messages. If polling fails, it logs,
        waits 2 seconds and tries again (or returns in ``once`` mode).
    """
    log.info("worker iniciado", extra={"action": "start", "status": "ok"})
    while True:
        try:
            messages = sqs.receive()
        except QueueUnavailableError as problem:
            log.error(str(problem), extra={"action": "receive",
                                           "status": "failed"})
            if once:
                return
            time.sleep(2)
            continue
        if not messages:
            log.debug("sin mensajes disponibles",
                      extra={"action": "receive", "status": "empty"})
        for message in messages:
            process_message(message)
        if once:
            return
