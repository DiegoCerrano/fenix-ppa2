"""Atomic read/write of the JSON files in data/ (inventory and ledger)."""

import json
import os
from pathlib import Path

from fenix.config import settings
from fenix.utils.errors import StateError


def read_json(path, default):
    """Read a JSON file, tolerating a missing one.

    Args:
        path: File to read.
        default: Value returned when the file does not exist.

    Returns:
        The parsed JSON value, or default.

    Raises:
        StateError: If the file exists but is not valid JSON.
    """

    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise StateError(f"{path.name} está corrupto") from error


def write_json_atomic(path, data):
    """Write JSON so a crash can never leave a half-written file.

    Args:
       path: Destination file.
       data: JSON-serializable object.

    Returns:
        None.

    Raises:
        OSError: If the disk write fails.
        TypeError: If data is not JSON-serializable.

    Side Effects:
        Writes a temporary file next to the destination, forces it to disk
        (``fsync``) and then swaps it in with ``os.replace``.
    """

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def load_inventory():
    """Load the inventory of resources owned by the group.

    Args:
        None.

    Returns:
        A dict like ``{"resources": {"node-01": {...}, ...}}``. An empty
        inventory when ``data/inventory.json`` does not exist yet.

    Raises:
        StateError: If the file is corrupt.
    """

    return read_json(settings.data_dir() / "inventory.json", {"resources": {}})


def save_inventory(inventory):
    """Persist the inventory atomically.

    Args:
        inventory: Dict to store in ``data/inventory.json``.

    Returns:
        None.

    Raises:
        OSError: If the disk write fails.
    """

    write_json_atomic(settings.data_dir() / "inventory.json", inventory)


def load_ledger():
    """Load the ledger of processed events.

    Args:
        None.

    Returns:
        A dict like ``{"events": {event_id: record}}``. Empty when
        ``data/events.json`` does not exist yet.

    Raises:
        StateError: If the file is corrupt.
    """

    return read_json(settings.data_dir() / "events.json", {"events": {}})


def save_ledger(ledger):
    """Persist the ledger atomically.

    Args:
        ledger: Dict to store in ``data/events.json``.

    Returns:
        None.

    Raises:
        OSError: If the disk write fails.
    """

    write_json_atomic(settings.data_dir() / "events.json", ledger)
