"""Notifications: console (development) or a Slack incoming webhook."""

import requests

from fenix.config import settings
from fenix.utils.errors import (ConfigError, NotifyPermanentError,
                                NotifyTransientError)
from fenix.utils.logging_json import get_logger

log = get_logger("notify")
TIMEOUT = (2, 5)  # (connect, read) seconds


def _retry_after(response):
    """Read the ``Retry-After`` header, capped to the exercise scope.

    Args:
        response: A ``requests.Response``.

    Returns:
        Seconds to wait, between 0 and 30 (0 if absent or invalid).

    Raises:
        None.
    """
    try:
        return min(30, max(0, int(response.headers.get("Retry-After", 0))))
    except ValueError:
        return 0


def _post(url, **kwargs):
    """POST with bounded timeouts, never leaking the original exception.

    Args:
        url: Destination URL (private: never log it).
        **kwargs: Extra arguments for ``requests.post`` (for example
            ``json=``).

    Returns:
        The ``requests.Response``.

    Raises:
        NotifyTransientError: On any network error or timeout. The
            original exception is dropped (``from None``) because it may
            carry the private URL.
    """
    try:
        return requests.post(url, timeout=TIMEOUT, **kwargs)
    except requests.RequestException as error:
        raise NotifyTransientError(
            f"error de red al avisar ({type(error).__name__})") from None


def _check_status(response):
    """Classify an HTTP status as success, transient or permanent.
    Args:
        response: A ``requests.Response``.

    Returns:
        None when the status is 2xx.

    Raises:
        NotifyTransientError: For 429 (with ``retry_after``) and 5xx.
        NotifyPermanentError: For 400/401/403/404/410 (configuration
            problems) and any other unexpected status.
    """
    code = response.status_code
    if code == 429:
        raise NotifyTransientError("proveedor limitó (429)",
                                   retry_after=_retry_after(response))
    if code >= 500:
        raise NotifyTransientError(f"proveedor caído (HTTP {code})")
    if code in (400, 401, 403, 404, 410):
        raise NotifyPermanentError(
            f"configuración rechazada por el proveedor (HTTP {code})")
    if not 200 <= code < 300:
        raise NotifyPermanentError(f"respuesta inesperada (HTTP {code})")


def _console(text):
    """Print the notification to the terminal.

    Args:
        text: Message to show.

    Returns:
        None.

    Raises:
        None.
    """
    print(f"AVISO> {text}", flush=True)


def _slack(text):
    """Send the notification to a Slack incoming webhook.

    Args:
        text: Message to send.

    Returns:
        None.

    Raises:
        NotifyPermanentError: If ``SLACK_WEBHOOK_URL`` is missing, Slack
            rejects the configuration, or the body is not ``ok``.
        NotifyTransientError: On timeout, network error, 429 or 5xx.

    Notes:
        Slack answers with the plain text ``ok``, not with JSON, so
        ``response.json()`` must not be used.
    """
    url = settings.env("SLACK_WEBHOOK_URL")
    if not url:
        raise NotifyPermanentError("falta SLACK_WEBHOOK_URL")
    response = _post(url, json={"text": text})
    _check_status(response)
    if response.text.strip() != "ok":
        raise NotifyPermanentError("Slack no respondió 'ok'")


def notify(text):
    """Send a notification through the configured provider.

    Args:
        text: Message to send (truncated to 500 characters).

    Returns:
        None.

    Raises:
        ConfigError: If ``NOTIFY_PROVIDER`` is not ``console`` or ``slack``.
        NotifyTransientError: Retryable provider or network failure.
        NotifyPermanentError: Configuration problem with the provider.

    Side Effects:
        Prints to the terminal or sends an HTTP request to Slack.
    """
    provider = settings.env("NOTIFY_PROVIDER", "console").lower()
    text = text[:500]
    if provider == "console":
        _console(text)
    elif provider == "slack":
        _slack(text)
    else:
        raise ConfigError(f"NOTIFY_PROVIDER desconocido: {provider}")
    log.info("aviso enviado", extra={"action": "notify", "status": "sent",
                                     "provider": provider})
