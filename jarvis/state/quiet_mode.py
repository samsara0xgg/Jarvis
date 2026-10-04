"""Allen's quiet level, one small file the daemon reads at boot (ADR 0153).

``off``, ``quiet``, ``no-pop`` or ``dnd``, cumulative in that order. It never
ends by itself, so a restart comes back in the level it left.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from jarvis.state.plugin_settings import write_private_json

if TYPE_CHECKING:
    from pathlib import Path

LEVELS = ("off", "quiet", "no-pop", "dnd")


def load(path: Path) -> str:
    """The saved level; a missing, broken or unknown one is ``off``."""
    try:
        level = json.loads(path.read_text(encoding="utf-8")).get("level")
    except (OSError, ValueError, AttributeError):
        return "off"
    return level if level in LEVELS else "off"


def save(path: Path, level: str) -> None:
    """Keep ``level`` for the next boot."""
    write_private_json(path, {"level": level})
