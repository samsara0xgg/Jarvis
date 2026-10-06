"""ADR 0170: one pairing token per terminal that reaches the brain over the private network.

``python -m jarvis pair <name>`` runs on the brain, mints a token, keeps only its SHA-256
beside the device's name in ``devices.json`` (0600) under the runtime root, and hands the
token back once. ``unpair`` deletes the row. The daemon reads the file on every check, so
a revoked token stops working without a restart.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from jarvis.state.plugin_settings import write_private_json

if TYPE_CHECKING:
    from pathlib import Path

_FILE = "devices.json"
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


class DeviceTokenError(ValueError):
    """A device name that is not usable, already paired, or not paired."""


def _digest(token: str) -> str:
    # The token is 256 random bits, so a plain SHA-256 is enough; no salt or stretching.
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _read(root: Path) -> dict[str, dict[str, str]]:
    path = root / _FILE
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("devices") if isinstance(data, dict) else None
    return rows if isinstance(rows, dict) else {}


def pair_device(root: Path, name: str) -> str:
    """Pair ``name`` and return its new token, the only time it is ever visible.

    Raises:
        DeviceTokenError: ``name`` is not 1-64 letters, digits, ``.``, ``_`` or ``-``,
            or is already paired.
    """
    if not _NAME.fullmatch(name):
        msg = "a device name is 1-64 letters, digits, '.', '_' or '-'"
        raise DeviceTokenError(msg)
    rows = _read(root)
    if name in rows:
        msg = f"{name} is already paired; unpair it first"
        raise DeviceTokenError(msg)
    token = secrets.token_urlsafe(32)
    rows[name] = {
        "sha256": _digest(token),
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    write_private_json(root / _FILE, {"devices": rows})
    return token


def unpair_device(root: Path, name: str) -> None:
    """Revoke ``name``'s token.

    Raises:
        DeviceTokenError: ``name`` is not paired.
    """
    rows = _read(root)
    if name not in rows:
        msg = f"{name} is not paired"
        raise DeviceTokenError(msg)
    del rows[name]
    write_private_json(root / _FILE, {"devices": rows})


def paired_devices(root: Path) -> list[tuple[str, str]]:
    """Each paired device as ``(name, created_at)``, oldest first; never a token or its hash."""
    rows = _read(root)
    return sorted(
        ((name, str(row.get("created_at", ""))) for name, row in rows.items()),
        key=lambda pair: (pair[1], pair[0]),
    )


def device_name_for_token(root: Path, token: str) -> str | None:
    """The paired device ``token`` belongs to, else ``None``; rows are compared in constant time."""
    try:
        rows = _read(root)
    except (OSError, ValueError):
        return None
    presented = _digest(token)
    found: str | None = None
    for name, row in rows.items():
        if secrets.compare_digest(presented, str(row.get("sha256", ""))):
            found = name
    return found


def device_token_matches(root: Path, token: str) -> bool:
    """Whether ``token`` belongs to a paired device."""
    return device_name_for_token(root, token) is not None
