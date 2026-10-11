"""ADR 0227: the brain-side settings each paired terminal last reported, kept across disconnects.

``terminal-settings.json`` (0600) under the runtime root maps a paired device's name to its row:
``reported`` (the brain-owned settings its latest ``hello`` found in the device's own files),
``asked`` (the part of those the brain differed on when the card was last raised) and ``open``
(the card still waits). The card is raised again only when ``asked`` would change, so a
reconnect with the same files says nothing. ``unpair_device`` removes a device's row.
"""

from __future__ import annotations

import json
import threading
from typing import TYPE_CHECKING, Any, Final

from jarvis.state.plugin_settings import write_private_json

if TYPE_CHECKING:
    from pathlib import Path

_FILE: Final = "terminal-settings.json"
# One lock for the file: a hello writes on the loop thread, an answer on a worker, an unpair
# in the CLI.
_LOCK: Final = threading.Lock()


def _read(root: Path) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads((root / _FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    rows = data.get("devices") if isinstance(data, dict) else None
    if not isinstance(rows, dict):
        return {}
    return {name: row for name, row in rows.items() if isinstance(row, dict)}


def rows(root: Path) -> dict[str, dict[str, Any]]:
    """Every device's row."""
    with _LOCK:
        return _read(root)


def keep(root: Path, device: str, row: dict[str, Any]) -> None:
    """Keep ``row`` for ``device``; the file is written only if it changed."""
    with _LOCK:
        found = _read(root)
        if found.get(device) != row:
            found[device] = row
            write_private_json(root / _FILE, {"devices": found})


def forget(root: Path, device: str) -> None:
    """Drop ``device``'s row; a device with none is already clean."""
    with _LOCK:
        found = _read(root)
        if found.pop(device, None) is not None:
            write_private_json(root / _FILE, {"devices": found})
