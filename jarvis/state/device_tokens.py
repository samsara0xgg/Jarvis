"""ADR 0170: one pairing token per terminal that reaches the brain over the private network.

``python -m jarvis pair <name>`` runs on the brain, mints a token, keeps only its SHA-256
beside the device's name in ``devices.json`` (0600) under the runtime root, and hands the
token back once. ``unpair`` deletes the row. The daemon reads the file on every check, so
a revoked token stops working without a restart.

ADR 0196: a device without a shell pairs by claiming a one-time code instead. The brain mints the
code for a name (:class:`PairingCodes`), keeps only its SHA-256 and only in memory, and a claim
inside ten minutes pairs that name exactly as ``pair`` does.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from jarvis.state.plugin_settings import write_private_json

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_FILE = "devices.json"
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
PAIRING_CODE_TTL_S: Final = 600


class DeviceTokenError(ValueError):
    """A device name that is not usable, already paired, or not paired."""


class DeviceNameTakenError(DeviceTokenError):
    """The device name is already paired."""


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


def _check_name(name: str) -> None:
    if not _NAME.fullmatch(name):
        msg = "a device name is 1-64 letters, digits, '.', '_' or '-'"
        raise DeviceTokenError(msg)


def _check_unpaired(rows: dict[str, dict[str, str]], name: str) -> None:
    if name in rows:
        msg = f"{name} is already paired; unpair it first"
        raise DeviceNameTakenError(msg)


def pair_device(root: Path, name: str) -> str:
    """Pair ``name`` and return its new token, the only time it is ever visible.

    Raises:
        DeviceTokenError: ``name`` is not 1-64 letters, digits, ``.``, ``_`` or ``-``,
            or is already paired.
    """
    _check_name(name)
    rows = _read(root)
    _check_unpaired(rows, name)
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


class PairingCodes:
    """ADR 0196: the one pairing code that is outstanding, held in this process's memory only.

    ``mint`` makes a code for a device name and voids any earlier one; ``claim`` trades a code
    for a new device token through :func:`pair_device`. Only the code's SHA-256 is kept, and a
    restart loses it. Safe to call from several threads.
    """

    def __init__(self, root: Path, *, clock: Callable[[], float] = time.time) -> None:
        """Hold no code yet.

        Args:
            root: The runtime root whose ``devices.json`` a claim pairs into.
            clock: Epoch seconds; injectable for tests.
        """
        self._root = root
        self._clock = clock
        self._lock = threading.Lock()
        self._held: tuple[str, str, float] | None = None  # (code digest, device name, expiry)

    def mint(self, name: str) -> tuple[str, int]:
        """Mint a code that pairs ``name`` once and return it with its expiry in epoch ms.

        Raises:
            DeviceNameTakenError: ``name`` is already paired.
            DeviceTokenError: ``name`` is not 1-64 letters, digits, ``.``, ``_`` or ``-``.
        """
        _check_name(name)
        _check_unpaired(_read(self._root), name)
        code = secrets.token_urlsafe(32)
        expires = self._clock() + PAIRING_CODE_TTL_S
        with self._lock:
            self._held = (_digest(code), name, expires)
        return code, int(expires * 1000)

    def claim(self, code: str) -> tuple[str, str] | None:
        """Consume ``code`` and return ``(device name, new token)``, else ``None``.

        A wrong, used or expired code, and a name paired by other means since the mint, all give
        the same ``None``; the last two also void the code.
        """
        try:
            presented = _digest(code)
        except UnicodeEncodeError:  # a lone surrogate in the JSON string is no code
            return None
        with self._lock:
            held = self._held
            if held is None or not secrets.compare_digest(presented, held[0]):
                return None
            self._held = None
            if self._clock() >= held[2]:
                return None
            try:
                return held[1], pair_device(self._root, held[1])
            except DeviceTokenError:
                return None
