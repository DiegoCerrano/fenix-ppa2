"""JSON logs : in-memory queue + writter thread + gzip rotation.
"""

import gzip
import json
import logging
import logging.handlers
import os
import queue
import re
import shutil
from datetime import datetime, timezone

from fenix.config import settings

BASE_FIELDS = ("event_id", "message_id", "resource_id",
               "attempt", "action", "status")

_STANDARD = set(logging.LogRecord("x", 0, "x", 0, "x", (), None).__dict__)
_STANDARD |= {"message", "asctime", "taskName"}
_SECRETS = [
    re.compile(r"https://hooks\.slack\.com/\S+"),
    re.compile(r"xox[a-z]-[A-Za-z0-9-]+"),
    re.compile(r"v1=[0-9a-f]{64}"),
]

_listener = None

logging.getLogger("fenix").addHandler(logging.NullHandler())


def sanitize(text):
    """Hide private ULRs, tokens and full signature inside a text.

    Args:
        text: Any log text.

    Returns:
        The text with Slack webhook URLs, Slack tokens and ``v1=`` HMAC signatures
        replaced by ``[hidden]``.

    Raises:
        None.
    """

    for pattern in _SECRETS:
        text = pattern.sub("[Hidden]", text)
        return text


class JsonFormatter(logging.Formatter):
    """Format every record as one JSON line with
    fixed context fields."""

    def format(self, record):
        """Turn a log inteo a JSON line.

        Args:
            record: The ``logging.LogRecord`` to format.

        Returns:
            A JSON string. Context fields that do not apply are ``null``;
            any extra attribute passed through ``extra=`` is included.

        Raises:
            None.
        """

        moment = datetime.fromtimestamp(record.created, timezone.utc)
        entry = {
            "timestamp": moment.isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": sanitize(record.getMessage()),
        }

        for field in BASE_FIELDS:
            entry[field] = getattr(record, field, None)
        for key, value in record.__dict__.items():
            if key in _STANDARD or key in BASE_FIELDS or key.startswith("_"):
                continue
            entry[key] = sanitize(value) if isinstance(value, str) else value
        return json.dumps(entry, ensure_ascii=False, default=str)


class _ConsoleFormatter(logging.Formatter):
    """Short human readable format for the terminal."""

    def format(self, record):
        """Format a record as ``HH:MM:SS LEVEL message [context]``.

        Args:
            record: The ``logging.LogRecord`` to format.

        Returns:
            A single line of text.

        Raises:
            None.
        """

        hour = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        extras = [f"{k}={getattr(record, k)}" for k in BASE_FIELDS
                  if getattr(record, k, None) is not None]
        text = f"{hour} {record.levelname:<8} {sanitize(record.getMessage())}"
        return f"{text} [{' '.join(extras)}]" if extras else text


class _NoTraceback(logging.Formatter):
    """Stop QueueHandler from gluing tracebacks into the message."""

    def formatException(self, exc_info):
        """Return an empty traceback text.

        Args:
            exc_info: The exception tuple (ignored on purpose).

        Returns:
            An empty string, so nothing sensitive leaks into the log.

        Raises:
            None.
        """

        return ""


def _rotator(source, dest):
    """Compress a rotated log file to gzip and delete the original.

    Args:
        source: Path of the plain-text file being rotated.
        dest: Path of the `` .gz`` file to create.

    Returns:
        None.

    Raises:
        OSError: If reading, writing or deleting a file fails.
    """

    with open(source, "rb") as plain, gzip.open(dest, "wb") as packed:
        shutil.copyfileobj(plain, packed)
    os.remove(source)


def _namer(name):
    """Append ``.gz`` to the name of a rotated file.

    Args:
        name: Default rotated file name (for example ``worker.log.1``).

    Returns:
        The name with a ``.gz`` suffix.

    Raises:
        None.
    """

    return name + ".gz"


def build_file_handler(path, max_bytes=None, backups=5):
    """Create a rotating JSON file handler with gzip history.

    Args:
        path: Log file to write.
        max_bytes: Size that triggers rotation. Defaults to the
        ``FENIX_LOG_MAX_BYTES`` setting, use a small value in tests.
    backups: How many compressed history files to keep.

    Returns:
        A configured ``RotatingFileHandler``.

    Raises:
        OSError: If the file cannot be opened.
        ValueError: If ``FENIX_LOG_MAX_BYTES`` is not an integer.    
    """

    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=max_bytes or settings.log_max_bytes(),
        backupCount=backups, encoding="utf-8")
    handler.rotator = _rotator
    handler.namer = _namer
    handler.setFormatter(JsonFormatter())
    return handler


def setup_logging(process):
    """Start asynchronous logging for the current process.

    Args:
        process: Short process name; becomes ``logs/<process>.log``.

    Returns:
        The running ``QueueListener``.

    Raises:
        OSError: If the log folder or file cannot be created.

    Side Effects:
        Attaches a ``QueueHandler`` to the ``fenix`` logger and starts a
        writer thread. Calling it twice returns the same listener.

    Notes:
        This ``queue.Queue`` lives in memory inside one process. It has
        nothing to do with the SQS broker.
    """

    global _listener, _producer
    if _listener is not None:
        return _listener

    root = logging.getLogger("fenix")
    root.setLevel(settings.log_level())
    root.propagate = False
    log_queue = queue.Queue(-1)
    producer = logging.handlers.QueueHandler(log_queue)
    producer.setFormatter(_NoTraceback())
    root.addHandler(producer)
    to_file = build_file_handler(settings.log_dir() / f"{process}.log")
    to_screen = logging.StreamHandler()
    to_screen.setLevel(logging.INFO)
    to_screen.setFormatter(_ConsoleFormatter())
    _listener = logging.handlers.QueueListener(
        log_queue, to_file, to_screen, respect_handler_level=True)
    _listener.start()
    _producer = producer
    return _listener


def shutdown_logging():
    """Flush pending records and stop the writer thread.

    Args:
        None.

    Returns:
        None.

    Raises:
        None.

    Side Effects:
        Drains the in-memory queue, closes the file handlers and detaches
        the ``QueueHandler``. Meant to be called from a ``finally`` block.
    """

    global _listener, _producer

    if _listener is None:
        return
    _listener.stop()
    root = logging.getLogger("fenix")
    root.removeHandler(_producer)
    for handler in _listener.handlers:
        handler.close()
    _listener = None
    _producer = None


def get_logger(name):
    """Return a child logger of ``fenix``.

    Args:
        name: Short module name (for example ``worker``).

    Returns:
        The logger named ``fenix.<name>``.

    Raises:
        None.
    """

    return logging.getLogger(f"fenix.{name}")
