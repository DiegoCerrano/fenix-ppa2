"""HTTP receiver: authenticate, validate and store in SQS. Never repairs."""

import json
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from fenix.config import settings
from fenix.services import events, sqs
from fenix.utils import crypto, logging_json, state
from fenix.utils.errors import (BadBodyError, ConfigError,
                                QueueUnavailableError, SignatureError,
                                StateError, ValidationError)
from fenix.utils.logging_json import get_logger

MAX_BODY = 16 * 1024
log = get_logger("receiver")


@asynccontextmanager
async def lifespan(app):
    """Start and stop the logging system together with the server.

    Args:
        app: The FastAPI application.

    Returns:
        An async context manager used by FastAPI.

    Raises:
        OSError: If the log file cannot be opened.
    """
    logging_json.setup_logging("receiver")
    yield
    logging_json.shutdown_logging()


app = FastAPI(title="Fénix receptor", lifespan=lifespan)


def _decode(raw_body):
    """Decode raw bytes into a JSON value.

    Args:
        raw_body: The request body as bytes.

    Returns:
        Whatever the JSON contains.

    Raises:
        BadBodyError: If the bytes are not UTF-8 or not valid JSON (the
            receiver answers HTTP 400).
    """
    try:
        return json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise BadBodyError("el cuerpo no es UTF-8/JSON válido") from error


def handle_webhook(raw_body, signature):
    """Run the whole webhook logic (no FastAPI needed, easy to test).

    Args:
        raw_body: Request body as raw bytes, exactly as received.
        signature: Value of ``X-Webhook-Signature`` or None.

    Returns:
        A tuple ``(http_status, payload_dict)``: 202 with ``event_id`` and
        ``message_id``; 400, 401, 422 or 503 with an error payload; 500 if
        the server secret is not configured.

    Raises:
        None. Every domain error is turned into an HTTP status.

    Side Effects:
        Publishes to SQS only after signature, JSON and contract checks
        pass, and logs the status together with ``elapsed_ms``.

    Notes:
        Order of checks: signature, JSON, contract, SQS. The size limit
        (413) is applied earlier, by the endpoint, while reading.
    """
    started = time.perf_counter()
    ctx = {"action": "webhook"}
    try:
        crypto.verify(settings.webhook_secret(), signature, raw_body)
        event = _decode(raw_body)
        clean = events.validate(event, state.load_inventory())
        ctx.update(event_id=clean["event_id"],
                   resource_id=clean["resource_id"])
        message_id = sqs.publish(clean)
        code, payload = 202, {"event_id": clean["event_id"],
                              "message_id": message_id}
    except SignatureError as error:
        code, payload = 401, {"error": "unauthorized"}
        ctx["reason"] = str(error)
    except ConfigError:
        code, payload = 500, {"error": "server_misconfigured"}
    except BadBodyError as error:
        code, payload = 400, {"error": "bad_request", "detail": str(error)}
    except ValidationError as error:
        code, payload = 422, {"error": "invalid_event", "detail": str(error)}
    except (QueueUnavailableError, StateError):
        code, payload = 503, {"error": "queue_unavailable"}
    ctx.update(status=code, elapsed_ms=round(
        (time.perf_counter() - started) * 1000, 1))
    level = log.info if code == 202 else log.warning
    level(f"webhook respondió {code}", extra=ctx)
    return code, payload


@app.get("/health")
async def health():
    """Report that the receiver process is alive.

    Args:
        None.

    Returns:
        ``{"status": "ok"}``.

    Raises:
        None.
    """
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(request: Request):
    """Receive an alert, enforcing the 16 KiB body limit.

    Args:
        request: The incoming FastAPI request.

    Returns:
        A JSON response: 413 if the body exceeds 16 KiB, otherwise the
        status produced by ``handle_webhook``.

    Raises:
        None.

    Notes:
        The body is read as raw bytes (the HMAC covers those exact bytes).
        Boto3 is blocking, so the work runs in a thread pool to avoid
        freezing the event loop.
    """
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY:
        return JSONResponse({"error": "payload_too_large"}, status_code=413)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY:
            return JSONResponse({"error": "payload_too_large"},
                                status_code=413)
    code, payload = await run_in_threadpool(
        handle_webhook, bytes(body),
        request.headers.get("X-Webhook-Signature"))
    return JSONResponse(payload, status_code=code)
