"""Her voice volume and speed, set by Allen's words (ADR 0174).

``current`` is what the next synthesized answer uses; ``default`` is what ``current``
returns to when a conversation ends, and the only part kept in a small file across
restarts. Read by the TTS provider at each task start, written by the ``set_voice`` tool,
so both sides share one thread-safe object injected at wiring time.
"""

from __future__ import annotations

import json
import threading
from typing import TYPE_CHECKING, Final

from jarvis.state.plugin_settings import write_private_json

if TYPE_CHECKING:
    from pathlib import Path

FACTORY_PERCENT: Final[int] = 100
FACTORY_SPEED: Final[float] = 1.0
MIN_PERCENT: Final[int] = 30
MAX_PERCENT: Final[int] = 300
MIN_SPEED: Final[float] = 0.6
MAX_SPEED: Final[float] = 1.8


def _clamp(percent: object, speed: object) -> tuple[int, float]:
    """A stored pair inside the bounds; anything unreadable is the factory voice."""
    if (
        isinstance(percent, bool) or not isinstance(percent, int | float)
        or isinstance(speed, bool) or not isinstance(speed, int | float)
    ):
        return FACTORY_PERCENT, FACTORY_SPEED
    return (
        int(min(MAX_PERCENT, max(MIN_PERCENT, percent))),
        round(min(MAX_SPEED, max(MIN_SPEED, speed)), 2),
    )


class VoiceSettings:
    """The current and default (percent, speed), under one lock."""

    def __init__(self, path: Path | None = None) -> None:
        """Load the saved default (a missing or broken file is the factory voice)."""
        self._path = path
        self._lock = threading.Lock()
        self._default = (FACTORY_PERCENT, FACTORY_SPEED)
        if path is not None:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self._default = _clamp(data.get("percent"), data.get("speed"))
            except (OSError, ValueError, AttributeError):
                pass
        self._current = self._default

    def snapshot(self) -> tuple[int, float]:
        """``(percent, speed)`` now."""
        with self._lock:
            return self._current

    def default(self) -> tuple[int, float]:
        """``(percent, speed)`` a conversation's end returns to."""
        with self._lock:
            return self._default

    def set_current(self, percent: int, speed: float) -> None:
        """Use this from the next answer on; not kept past the conversation."""
        with self._lock:
            self._current = _clamp(percent, speed)

    def remember(self) -> None:
        """Make the current pair the default and keep it for the next boot."""
        with self._lock:
            self._default = self._current
            self._save()

    def reset(self) -> None:
        """Back to the factory voice, and forget any remembered default."""
        with self._lock:
            self._current = self._default = (FACTORY_PERCENT, FACTORY_SPEED)
            self._save()

    def end_conversation(self) -> None:
        """The conversation is over: the current pair returns to the default."""
        with self._lock:
            self._current = self._default

    def _save(self) -> None:
        if self._path is not None:
            percent, speed = self._default
            write_private_json(self._path, {"percent": percent, "speed": speed})
