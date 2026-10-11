"""ADR 0225: what each paired terminal last declared it runs, kept across disconnects.

``terminal-tools.json`` (0600) under the runtime root maps a paired device's name to the sorted
tool and operation names of its latest ``hello``. The brain offers a device tool exactly when a
paired device in this file declared it, so a laptop that is away, or a brain that restarts before
any terminal connects, keeps the menu it had. ``unpair_device`` removes a device's row.
"""

from __future__ import annotations

import json
import threading
from typing import TYPE_CHECKING, Final

from jarvis.state.plugin_settings import write_private_json

if TYPE_CHECKING:
    from collections.abc import Collection
    from pathlib import Path

_FILE: Final = "terminal-tools.json"
# One lock for the file: a hello writes on the loop thread, an unpair runs in the CLI or a route.
_LOCK: Final = threading.Lock()


def _read(root: Path) -> dict[str, list[str]]:
    try:
        data = json.loads((root / _FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    rows = data.get("devices") if isinstance(data, dict) else None
    if not isinstance(rows, dict):
        return {}
    return {
        name: sorted({tool for tool in tools if isinstance(tool, str)})
        for name, tools in rows.items()
        if isinstance(name, str) and isinstance(tools, list)
    }


def remember(root: Path, device: str, tools: Collection[str]) -> None:
    """Keep ``tools`` as what ``device`` declares now; the file is written only if it changed."""
    declared = sorted(set(tools))
    with _LOCK:
        rows = _read(root)
        if rows.get(device) != declared:
            rows[device] = declared
            write_private_json(root / _FILE, {"devices": rows})


def forget(root: Path, device: str) -> None:
    """Drop ``device``'s declarations; a device with none is already clean."""
    with _LOCK:
        rows = _read(root)
        if rows.pop(device, None) is not None:
            write_private_json(root / _FILE, {"devices": rows})


def declared(root: Path, paired: Collection[str]) -> frozenset[str]:
    """Every name declared by a device in ``paired``; a row of any other device is dropped."""
    with _LOCK:
        rows = _read(root)
        stale = rows.keys() - set(paired)
        for name in stale:
            del rows[name]
        if stale:
            write_private_json(root / _FILE, {"devices": rows})
    return frozenset(tool for tools in rows.values() for tool in tools)
