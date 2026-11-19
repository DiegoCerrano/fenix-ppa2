"""Convergent repair (``repair``) and staged pipeline (``handle``)."""

import os
import time

from botocore.exceptions import BotoCoreError, ClientError

from fenix.config import aws, settings
from fenix.services import cloud_support as cloud
from fenix.services import events, notifications
from fenix.utils import logging_json, state
from fenix.utils.errors import (ConflictError, PermanentError,
                                SimulatedFailure, TransientError)
from fenix.utils.logging_json import get_logger
from fenix.utils.validation import utc_now_text

log = get_logger("remediation")


def _wait_running(ec2, instance_id, tries=8):
    """Wait briefly (bounded) for an instance to be running.

    Args:
        ec2: A Boto3 EC2 client.
        instance_id: Instance to watch.
        tries: Maximum number of one-second checks.

    Returns:
        The last state observed.

    Raises:
        ClientError: If EC2 fails with an unexpected error.

    Notes:
        Short on purpose: the worker must not block for minutes.
    """
    current = None
    for _ in range(tries):
        current = cloud.instance_state(ec2, instance_id)
        if current == "running":
            return current
        time.sleep(1)
    return current


def _repair_instance(resource, ec2):
    """Bring a stopped instance back to ``running``.

    Args:
        resource: Inventory entry of the instance (needs ``id``).
        ec2: A Boto3 EC2 client.

    Returns:
        A text describing what was done.

    Raises:
        TransientError: If the instance is still stopping, did not reach
            running in time, or a Boto3 call failed.
        PermanentError: If the instance is in a state that cannot be
            repaired (for example terminated or missing).

    Side Effects:
        Calls ``start_instances`` only when the real state is ``stopped``.
    """
    try:
        current = cloud.instance_state(ec2, resource["id"])
        if current == "running":
            return "ya estaba en running, no hice nada"
        if current == "stopped":
            ec2.start_instances(InstanceIds=[resource["id"]])
            current = _wait_running(ec2, resource["id"])
        elif current == "pending":
            current = _wait_running(ec2, resource["id"])
        elif current == "stopping":
            raise TransientError("el nodo todavía se está deteniendo")
        else:
            raise PermanentError(f"estado no reparable: {current}")
    except (ClientError, BotoCoreError) as error:
        raise TransientError(
            f"fallo de Boto3 ({type(error).__name__})") from error
    if current != "running":
        raise TransientError("el nodo todavía no llegó a running")
    return "start_instances: stopped -> running verificado"


def _repair_bucket(resource, s3):
    """Restore the mandatory tags of a bucket, keeping foreign tags.

    Args:
        resource: Inventory entry (needs ``id`` and ``expected_tags``).
        s3: A Boto3 S3 client.

    Returns:
        A text describing what was done.

    Raises:
        TransientError: If a Boto3 call failed or the tags did not end up
            as expected after writing.

    Side Effects:
        Reads, merges, writes and re-reads the tag set. Expected values win
        over current ones; unknown tags are preserved.
    """
    expected = resource["expected_tags"]
    try:
        current = cloud.get_bucket_tags(s3, resource["id"])
        merged = {**current, **expected}
        if merged == current:
            return "las etiquetas ya estaban completas, no hice nada"
        cloud.put_tags(s3, resource["id"], merged)
        verified = cloud.get_bucket_tags(s3, resource["id"])
    except (ClientError, BotoCoreError) as error:
        raise TransientError(
            f"fallo de Boto3 ({type(error).__name__})") from error
    if any(verified.get(key) != value for key, value in expected.items()):
        raise TransientError("las etiquetas no quedaron como se esperaba")
    restored = sorted(k for k, v in expected.items() if current.get(k) != v)
    return f"etiquetas restauradas: {restored}"


def repair(resource, ec2=None, s3=None):
    """Drive a resource to its desired state (convergent repair).

    Args:
        resource: Inventory entry with ``type`` (``ec2`` or ``s3``).
        ec2: Optional EC2 client (tests).
        s3: Optional S3 client (tests).

    Returns:
        A text describing what was done (or that nothing was needed).

    Raises:
        TransientError: Retryable failure (see the private helpers).
        PermanentError: Unknown resource type or unrepairable state.

    Notes:
        It first looks at the real state and acts only if needed, so
        repeating it is harmless. ``start`` converges; ``reboot`` would
        repeat its effect every time.
    """
    if resource["type"] == "ec2":
        return _repair_instance(resource, ec2 or aws.get_client("ec2"))
    if resource["type"] == "s3":
        return _repair_bucket(resource, s3 or aws.get_client("s3"))
    raise PermanentError(f"tipo de recurso desconocido: {resource['type']}")


def _save(ledger, record):
    """Stamp a record with the current time and persist the ledger.

    Args:
        ledger: The whole ledger dict.
        record: The event record that just changed (updated in place).

    Returns:
        None.

    Raises:
        OSError: If the disk write fails.
    """
    record["updated_at"] = utc_now_text()
    state.save_ledger(ledger)


def _maybe_crash(ctx):
    """Kill the process right after repairing (lab scenario F12 only).

    Args:
        ctx: Logging context of the event.

    Returns:
        None (it does not return when the crash is enabled).

    Raises:
        None.

    Side Effects:
        If ``FENIX_CRASH_AFTER_REPAIR=1``: logs a CRITICAL line, flushes
        the logs and exits the process abruptly with code 137.
    """
    if settings.env("FENIX_CRASH_AFTER_REPAIR") == "1":
        log.critical("corte simulado después de reparar", extra=ctx)
        logging_json.shutdown_logging()
        os._exit(137)


def handle(event, receive_count=1, message_id=None):
    """Process a validated event in persisted stages.

    Args:
        event: A validated event dict (see ``events.validate``).
        receive_count: SQS delivery number, used only for logging.
        message_id: SQS ``MessageId``, used only for logging.

    Returns:
        ``"done"`` after finishing, or ``"already_done"`` if a previous
        run had already completed this ``event_id``.

    Raises:
        ConflictError: If the same ``event_id`` has different content.
        SimulatedFailure: While ``attempts <= simulate_failures``.
        TransientError: Retryable repair or notification failure.
        PermanentError: Unrepairable resource or missing inventory entry.
        StateError: If a data file is corrupt.

    Side Effects:
        Updates ``data/events.json`` after every stage (attempt, repair,
        notification) BEFORE moving on, so a restart resumes where it
        stopped without repeating verified actions.

    Notes:
        An external API call and a JSON write are not one transaction. If
        the process dies in between, the next run looks at the real state
        again; a convergent repair makes that safe.
    """
    event_id, alias = event["event_id"], event["resource_id"]
    ctx = {"event_id": event_id, "message_id": message_id,
           "resource_id": alias, "attempt": receive_count}
    ledger = state.load_ledger()
    record = ledger["events"].get(event_id)
    fingerprint = events.fingerprint(event)
    if record is None:
        record = {"fingerprint": fingerprint, "event": event, "attempts": 0,
                  "repair_done": False, "repair_result": None,
                  "notified": False, "status": "received",
                  "first_seen": utc_now_text()}
        ledger["events"][event_id] = record
    elif record["fingerprint"] != fingerprint:
        log.error("mismo event_id con contenido distinto",
                  extra={**ctx, "action": "dedupe", "status": "conflict"})
        raise ConflictError(f"{event_id} ya existe con otro contenido")
    if record["status"] == "done":
        log.info("evento ya completado, solo confirmo",
                 extra={**ctx, "action": "dedupe", "status": "already_done"})
        return "already_done"
    record["attempts"] += 1
    _save(ledger, record)
    if record["attempts"] <= event["simulate_failures"]:
        raise SimulatedFailure(
            f"falla simulada {record['attempts']}/{event['simulate_failures']}")
    resource = state.load_inventory()["resources"].get(alias)
    if resource is None:
        raise PermanentError(f"{alias} ya no está en el inventario")
    if not record["repair_done"]:
        result = repair(resource)
        record.update(repair_done=True, repair_result=result,
                      status="repaired")
        _save(ledger, record)
        log.info(result, extra={**ctx, "action": "repair", "status": "ok"})
        _maybe_crash(ctx)
    if not record["notified"]:
        text = (f"[Fénix] {event['event_type']} en {alias}: "
                f"{record['repair_result']} (evento {event_id})")
        notifications.notify(text)
        record.update(notified=True, status="done")
        _save(ledger, record)
    return "done"
