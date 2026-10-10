"""ADR 0210: the Apple push tokens a paired device registered, one row per device.

``push-tokens.json`` (0600) beside ``devices.json`` under the runtime root holds, per paired
device name, the APNs device token, the Live Activity push token when one is running, and which
APNs endpoint (``sandbox`` or ``production``) both belong to. A token is a bearer secret for
pushing to that phone, so no function here logs one and no row is ever listed with it.

:func:`parse_registration` is the strict check of ``POST /inherent/device/push``'s body; the
route (L5) and the writer (the daemon) share it. ``unpair_device`` removes a device's row.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, Literal

from jarvis.state.plugin_settings import write_private_json

if TYPE_CHECKING:
    from pathlib import Path

_FILE: Final = "push-tokens.json"
ENVIRONMENTS: Final = ("sandbox", "production")
Environment = Literal["sandbox", "production"]
Kind = Literal["device", "live_activity"]
_TOKEN: Final = re.compile(r"[0-9a-fA-F]{32,512}")
_KEYS: Final = frozenset({"device_token", "environment", "live_activity_token"})
# One lock for the file: the route writes on the loop thread, a 410 drops on a worker thread.
_LOCK: Final = threading.Lock()


class RegistrationError(ValueError):
    """A registration body that is not what the route accepts; the text is safe to send back."""


@dataclass(frozen=True)
class Registration:
    """What one device registered; ``live_activity_token`` is ``None`` while no activity runs."""

    device_token: str
    environment: Environment
    live_activity_token: str | None = None


def parse_registration(body: object) -> tuple[Registration, bool]:
    """The registration in ``body`` and whether it carries ``live_activity_token`` at all.

    ``device_token`` and ``environment`` are required. ``live_activity_token`` absent leaves the
    stored one as it is, ``null`` clears it, a string sets it. Tokens are hex; they are lowered.

    Raises:
        RegistrationError: not an object, a missing, unknown or mistyped key, or a token that is
            not 32 to 512 hex characters. The message never repeats a value.
    """
    if not isinstance(body, dict):
        msg = "send an object"
        raise RegistrationError(msg)
    if body.keys() - _KEYS:
        msg = "unknown key"
        raise RegistrationError(msg)
    environment = body.get("environment")
    if environment not in ENVIRONMENTS:
        msg = 'environment is "sandbox" or "production"'
        raise RegistrationError(msg)
    device = _token(body.get("device_token"), "device_token")
    carried = "live_activity_token" in body
    live = body.get("live_activity_token")
    return Registration(
        device,
        environment,
        None if live is None else _token(live, "live_activity_token"),
    ), carried


def _token(value: object, name: str) -> str:
    if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
        msg = f"{name} is 32 to 512 hex characters"
        raise RegistrationError(msg)
    return value.lower()


def _read(root: Path) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads((root / _FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    rows = data.get("devices") if isinstance(data, dict) else None
    return rows if isinstance(rows, dict) else {}


def _row(raw: object) -> Registration | None:
    if not isinstance(raw, dict):
        return None
    device, environment, live = (
        raw.get("device_token"), raw.get("environment"), raw.get("live_activity_token"),
    )
    if not (isinstance(device, str) and environment in ENVIRONMENTS):
        return None
    return Registration(device, environment, live if isinstance(live, str) else None)


def register(root: Path, name: str, new: Registration, *, live_carried: bool = True) -> None:
    """Keep ``new`` as ``name``'s registration; unless ``live_carried``, the live token stays."""
    with _LOCK:
        rows = _read(root)
        kept = _row(rows.get(name))
        live = new.live_activity_token
        if not live_carried and kept is not None and kept.environment == new.environment:
            live = kept.live_activity_token
        rows[name] = {
            "device_token": new.device_token,
            "environment": new.environment,
            "live_activity_token": live,
            "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        write_private_json(root / _FILE, {"devices": rows})


def register_body(root: Path, name: str, body: object) -> None:
    """``POST /inherent/device/push`` for ``name``: check ``body`` and keep what it registers.

    Raises:
        RegistrationError: see :func:`parse_registration`.
    """
    new, live_carried = parse_registration(body)
    register(root, name, new, live_carried=live_carried)


def registrations(root: Path) -> dict[str, Registration]:
    """Every device's registration by device name; a row that is malformed is skipped."""
    with _LOCK:
        rows = _read(root)
    return {name: one for name, raw in rows.items() if (one := _row(raw)) is not None}


def drop_token(root: Path, name: str, kind: Kind) -> None:
    """Forget one token of ``name`` (APNs said it is no longer registered); the device stays."""
    with _LOCK:
        rows = _read(root)
        raw = rows.get(name)
        if not isinstance(raw, dict):
            return
        if kind == "live_activity":
            raw["live_activity_token"] = None
        else:
            del rows[name]
        write_private_json(root / _FILE, {"devices": rows})


def remove_device(root: Path, name: str) -> None:
    """Forget everything ``name`` registered; a device with nothing is already clean."""
    with _LOCK:
        rows = _read(root)
        if rows.pop(name, None) is not None:
            write_private_json(root / _FILE, {"devices": rows})
