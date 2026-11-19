"""HMAC-SHA256 signature of the webhook: ``t=<epoch>,v1=<64 hex>``."""

import hashlib
import hmac
import re
import time

from fenix.utils.errors import SignatureError

WINDOW_SECONDS = 300
_HEX64 = re.compile(r"[0-9a-f]{64}")


def _digest(secret, timestamp, body):
    """Compute the hex HMAC-SHA256 of ``timestamp + "." + body``.

    Args:
        secret: Shared secret as text.
        timestamp: Unix time (int) used in the signed message.
        body: Raw request body as bytes.

    Returns:
        A 64 character lowercase hex string.

    Raises:
        None.
    """

    message = str(timestamp).encode() + b"." + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def sign(secret, body, timestamp=None):
    """Build the value of the ``X-Webhook-Signature`` header.

    Args:
        secret: Shared secret as text.
        body: Raw body bytes, exactly as they will be sent.
        timestamp: Unix time to sign with. Defaults to now.

    Returns:
        A string like ``t=1762867867,v1=<64 hex>``.

    Raises:
        None.
    """

    timestamp = int(time.time()) if timestamp is None else int(timestamp)
    return f"t={timestamp},v1={_digest(secret, timestamp, body)}"


def _parse(header):
    """Split the header into timestamp and signature with strict rules.

    Args:
        header: Raw header value (or None).

    Returns:
        A tuple ``(timestamp_int, signature_hex)``.

    Raises:
        SignatureError: If the header is absent, does not have exactly the
        keys ``t`` and ``v1`` once each, ``t`` is not made of digits,
        or ``v1`` is not 64 lowercase hex characters.
    """

    if not isinstance(header, str) or not header:
        raise SignatureError("firma ausente")
    parts = header.split(",")
    if len(parts) != 2:
        raise SignatureError("firma con formato ambiguo")
    found = {}
    for part in parts:
        key, sep, value = part.partition("=")
        if not sep or key in found or key not in ("t", "v1"):
            raise SignatureError("firma mal formada o duplicada")
        found[key] = value
    if set(found) != {"t", "v1"}:
        raise SignatureError("faltan t o v1")
    stamp = found["t"]
    if not (stamp.isascii() and stamp.isdigit()):
        raise SignatureError("t no es un epoch")
    if not _HEX64.fullmatch(found["v1"]):
        raise SignatureError("v1 no son 64 hex en minúscula")
    return int(stamp), found["v1"]


def verify(secret, header, body, now=None):
    """Validate the signature and its time window.

    Args:
        secret: Shared secret as text.
        header: Value of ``X-Webhook-Signature`` (or None).
        body: Raw request body bytes.
        now: Current Unix time. Defaults to ``time.time()``.

    Returns:
        None when the signature is valid.

    Raises:
        SignatureError: If the header is malformed, the timestamp
        is more than 300 seconds away from now, or the digest does not match.

    Notes:
        ``hmac.compare_digest`` compares in constant time. The 300 s window
        does not stop duplicates inside five minutes: the ledger does.
    """

    timestamp, provided = _parse(header)
    now = time.time() if now is None else now
    if abs(now - timestamp) > WINDOW_SECONDS:
        raise SignatureError("timestamp fuera de la ventana de 300 s")
    expected = _digest(secret, timestamp, body)
    if not hmac.compare_digest(expected, provided):
        raise SignatureError("la firma no coincide")
