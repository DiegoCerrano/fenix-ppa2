"""Small reusable validators: identifiers, UTC dates and bounded integers."""


import re
import time
from datetime import datetime, timedelta, timezone

from fenix.utils.errors import ValidationError

_ID = re.compile(r"[a-z0-9-]{3, 32}")


def identifier(value, field="id"):
    """Check that a value is a lowercase slug of 3 to 32 characters.

    Args:
        value: Any object comming from the outside world.
        field: Name of the field, used only in the error message.

    Returns:
        The same string, unchanged, when it is valid.

    Raises:
        ValidationError: If value is not a string made only of
        ``[a-z0-9-]`` with a lenght between 3 and 32.

    Notes:    
        ``fullmatch`` is used on purpose: ``match`` plus `$` would let
        a trailing newline slip through.
    """

    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValidationError(f"{field} must be text [a-z0-9-]{{3, 32}}")
    return value


def utc_text(value, field="date"):
    """Parse an ISO-8601 string that carries an explicit UTC offset.

    Args:
        value: Text such as ``2026-11-10T18:00:00Z``.
        field: Name of the field, used only in the error message.

    Returns:
        A timezone-aware ``datetime`` expressed in UTC.

    Raises:
        ValidationError: If value is not exist, is not ISO-8601, has no timezone,
        or its offset is not zero.
    """

    if not isinstance(value, str):
        raise ValidationError(f"{field} must be text ISO-8601")
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValidationError(
            f"{field} is not a valid ISO-8601 datetime") from error
    if moment.tzinfo is None or moment.utcoffset() != timedelta(0):
        raise ValidationError(f"{field} must contain explicit UTC (Z)")

    return moment.astimezone(timezone.utc)


def utc_now_text():
    """Return the current time as text in UTC.

    Args:
        None.

    Returns:
        A string such as a ``2026-11-10T18:00:00Z``.

    Raises:
        None.
    """

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def bounded_int(value, minimum, maximum, field="number"):
    """Check that a value is an integer inside an inclusive range.

    Args:
        value: Object to check.
        minimum: Smallest accepted value.
        maximum: Largest accepted value.
        field: Name of the field, used only in the error message.

    Returns:
        The same integer, unchanged.

    Raises:
        ValidationError: If value is not an ``int``, is a ``bool``
        (even through ``True == 1`` in Python) or falls outside the range.
    """

    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field} must be an integer.")
    if not minimum <= value <= maximum:
        raise ValidationError(
            f"{field} must be between {minimum} and {maximum}")
    return value


def ttl_seconds(created_epoch, ttl, now=None):
    """Compute how many seconds of life are left for something.

    Args:
        created_epoch: Unix time when the thing was created.
        ttl: Total life span in seconds.
        now: Current Unix time. Defaults to ``time.time()``.

    Returns:
        Remaining whole seconds, never below zero.

    Raises:
        None.

    Note:
        Fenix does not use it to operate; it is kept as a shared 
    helper practice required by the assignment.
    """

    now = time.time() if now is None else now
    return max(0, int(created_epoch + ttl - now))
