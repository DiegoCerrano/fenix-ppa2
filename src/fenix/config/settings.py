"""Central configuration: read and validate environment variables.

This module has no third-party dependencies on purpose: Boto3 clients
are built in ``fenix.config.aws`` from the values validated here.
"""

import os
from pathlib import Path
from urllib.parse import urlparse

from fenix.utils.errors import ConfigError, ValidationError
from fenix.utils.validation import identifier


_LOOPBACK = {"localhost", "127.0.0.1", "::1"}


def load_env(path=".env"):
    """Load ``KEYVALUE`` lines from a dotenv file into ``os.environ``.

    Args:
        path: Path of the file. Missing files are silently ignored.

    Returns:
        None.

    Raises:
        None.

    Side Effects:
        Overwrintes existing environment variables with file values,
    so the project always wins over the shell. 
    """

    file = Path(path)
    if not file.is_file():
        return
    for line in file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ[key.strip()] = value.strip().strip('"').strip("'")


def env(name, default=""):
    """Read an environment variable.

    Args:
        name: Variable Name.
        default: Value returned when the variable is not set.

    Returns:
        The variable value or default.

    Raises:
        None.
    """

    return os.environ.get(name, default)


def group_id():
    """Return the group identifier used in name and tags.

    Args:
        None.

    Returns:
        The ``GROUP_ID`` value (default ``grupo01``)

    Raises:
        ConfigError: If the value is not a valid slug (see ``identifier``).
    """

    try:
        return identifier(env("GROUP_ID", "grupo01"), "GROUP_ID")
    except ValidationError as error:
        raise ConfigError(str(error)) from error


def data_dir():
    """Return de folder that holds inventory and ledger files.

    Args:
        None.

    Returns:
        A ``Path`` (``data/`` or ``FENIX_DATA_DIR``.)

    Raises:
        OSError: If the folder cannot be created.

    Side Effects:
        Create the folder when it does not exist.
    """

    folder = Path(env("FENIX_DATA_DIR", "data"))
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def log_dir():
    """Return the folder that holds the log files.

    Args:
        None.

    Returns:
        A ``Path`` (``logs/`` or ``FENIX_LOG_DIR``)

    Side Effects:
        Creates the folder when it does not exist.
    """

    folder = Path(env("FENIX_LOG_DIR", "logs"))
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def log_level():
    """Return the minimum log level written to files.

    Args:
        None.

    Returns:
        An upper-case level name, ``DEBUG`` by default.

    Raises:
        None.
    """

    return env("FENIX_LOG_LEVEL", "DEBUG").upper()


def log_max_bytes():
    """Return the file size that triggers log rotatios.

    Args:
        None.

    Returns:
        Size in bytes (default 1000000).

    Raises:
        ValueError: If ``FENIX_LOG_MAX_BYTES`` is not an integer.
    """

    return int(env("FENIX_LOG_MAX_BYTES", "1000000"))


def queue_names():
    """Returns the names of the main queue and of its dead-letter-queue.

    Args:
        None.

    Returns:
        A tuple ``(main_name, dlq_name)`` derived from the group id.

    Raises:
        ConfigError: If ``GROUP_ID`` is invalid.
    """

    group = group_id()
    return f"fenix-events-{group}", f"fenix-dlq-{group}"


def webhook_secret():
    """Return the HMAC secret shared with the webhook sender.

    Args:
        None.

    Returns:
        The ``WEBHOOK_SECRET`` value.

    Raises:
        ConfigError: If the is missing or shorter than 16 characters.
    """

    secret = env("WEBHOOK_SECRET")
    if len(secret) < 16:
        raise ConfigError(
            "WEBHOOK_SECRET is missing or shorter than 16 characteres")
    return secret


def endpoint_url():
    """Return the Localstack URL after checking it is a loopback address.

    Args:
        None.

    Returns:
        The ``AWS_ENDPOINT_URL`` value (default ``http://localhost:4566``)

    Raises:
        ConfigError: If the is not loopback or the port is not 4566.
            This protects against operating on real AWS by mistake.  
    """

    url = env("AWS_ENDPOINT_URL", "http://localhost:4566")
    parsed = urlparse(url)
    if parsed.hostname not in _LOOPBACK or parsed.port != 4566:
        raise ConfigError("AWS_ENDPOINT_URL must point at loopback:4566")
    return url


def aws_credentials():
    """Return the LocalStack credentials after checking they are fake.

    Args:
        None.

    Returns:
        A tuple ``(access_key_id, secret_access_key)``, ``("test", "test")``
        by default.

    Raises:
        ConfigError: If the access key looks like a real AWS key
            (starts with AKIA or ASIA).
    """

    key = env("AWS_ACCESS_KEY_ID", "test")
    secret = env("AWS_SECRET_ACCESS_KEY", "test")
    if key.startswith(("AKIA", "ASIA")):
        raise ConfigError("It looks like a real AWS key: use the fake ones")
    return key, secret


def region():
    """Return the AWS region used by every client.

    Args:
        None.

    Returns:
        The ``AWS_DEFAULT_REGION`` value (default ``us-east-1``).

    Raises:
        None.
    """

    return env("AWS_DEFAULT_REGION", "us-east-1")


load_env()
