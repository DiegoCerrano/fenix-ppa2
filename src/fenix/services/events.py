"""Validation of the event contract against the inventory."""

import hashlib
import json

from fenix.utils.errors import ValidationError
from fenix.utils.validation import bounded_int, identifier, utc_text


FIELDS = {"schema_version", "event_id", "event_type",
          "resource_id", "occurred_at", "simulate_failures"}

EVENT_TYPES = {"instance.stopped": "ec2", "bucket.tags_missing": "s3"}


def validate(event, inventory):
    """Validate an event and return a clean copy of it.

    Args: 
        event: Whatever was decoded from JSON (expected: a dict).
        inventory: Dict with a ``resources`` mapping alias -> resource.

    Returns:
        A shallow copy of the event when it satisfies the contract.

    Raises:
        ValidationError: If the event is not an object, has missing or extra
        keys, a wrong ``schema_version``, a malformed id, an unknown
        ``event_type``, a ``resource_id`` that is not in the inventory,
        an ``event_type`` that does not match the resource type, a
        date without explicit UTC, or ``simulate_failures`` outside
        0..5 (a ``bool`` is rejected too).

    Notes:
        It runs twice: in the HTTP receiver and again in the worker,
        because a message can be injected straight into SQS.
    """

    if not isinstance(event, dict):
        raise ValidationError("The event must be a JSON object")
    missing = FIELDS - set(event)
    extra = set(event) - FIELDS
    if missing:
        raise ValidationError(f"faltan claves: {sorted(missing)}")
    if extra:
        raise ValidationError(f"claves no permitidas: {sorted(extra)}")
    bounded_int(event["schema_version"], 1, 1, "schema_version")
    identifier(event["event_id"], "event_id")
    identifier(event["resource_id"], "resource_id")
    kind = event["event_type"]
    if not isinstance(kind, str) or kind not in EVENT_TYPES:
        raise ValidationError("event_type desconocido.")
    resource = inventory.get("resources", {}).get(event["resource_id"])
    if resource is None:
        raise ValidationError("resource_id no esta en el inventario.")
    if resource.get("type") != EVENT_TYPES[kind]:
        raise ValidationError("event_type no corresponde a ese recurso.")
    utc_text(event["occurred_at"], "occurred_at")
    bounded_int(event["simulate_failures"], 0, 5, "simulate_failures")
    return dict(event)


def fingerprint(event):
    """Compute a stable hash of an event's content.

    Args:
        event: A validated event dict.

    Returns:
        A SHA-256 hex digest of the canonical JSON of the event.

    Raises:
        TypeError: If the event contains non-JSON values.

    Notes:
        Used to detect the same ``event_id`` arriving with different data.
    """

    canonical = json.dumps(event, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()
