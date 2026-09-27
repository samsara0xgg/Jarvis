"""L3 thinking mode (ADR 0061): Allen's own words switch the model's thinking on for a conversation.

A word from ``llm.think.on_words`` in what he says turns it on, that sentence included; a word
from ``llm.think.off_words``, or a pause longer than :data:`CONVERSATION_GAP_MS`, turns it off.
Nothing is stored: each turn reads the mode back from his recent words in the event log, so a
restart keeps it and no new event exists for it.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from jarvis.state.event_log import iter_events_of_types

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping

_USER_WORDS: Final = ("surface.user_intent", "utterance.received")
# The 10 minutes after which the companion stops keeping a conversation on top.
CONVERSATION_GAP_MS: Final = 10 * 60 * 1000
# ponytail: an unbroken conversation longer than this forgets its on-word; widen if one ever does.
_LOOKBACK_MS: Final = 6 * 60 * 60 * 1000


class ThinkModeConfigError(ValueError):
    """``llm.think`` names a missing preset or holds an invalid pattern."""


@dataclass(frozen=True)
class ThinkMode:
    """``llm.think``: the preset a thinking turn uses and the words that switch it."""

    preset: str
    on: re.Pattern[str]
    off: re.Pattern[str]

    def preset_for(self, conn: sqlite3.Connection, now_ms: int) -> str | None:
        """:attr:`preset` while the running conversation's newest on-word beats every off-word."""
        words = list(iter_events_of_types(conn, _USER_WORDS, since_epoch_ms=now_ms - _LOOKBACK_MS))
        later = now_ms
        for event in reversed(words):
            if later - event.ts_epoch_ms > CONVERSATION_GAP_MS:
                return None
            said = event.payload.get("transcript")
            if isinstance(said, str):
                if self.off.search(said):
                    return None
                if self.on.search(said):
                    return self.preset
            later = event.ts_epoch_ms
        return None

    def status(self, conn: sqlite3.Connection) -> dict[str, Any]:
        """ADR 0064: ``GET /inherent/think``, whether it is on now and the words that switch it."""
        return {
            "on": self.preset_for(conn, int(time.time() * 1000)) is not None,
            "on_words": self.on.pattern,
            "off_words": self.off.pattern,
        }


def load_think_mode(llm: Mapping[str, Any]) -> ThinkMode | None:
    """Parse ``llm.think``; absent means no thinking mode, a broken block fails boot."""
    block = llm.get("think")
    if block is None:
        return None
    presets = llm.get("presets") or {}
    preset = block.get("preset") if isinstance(block, dict) else None
    if not isinstance(preset, str) or preset not in presets:
        msg = f"llm.think.preset {preset!r} is not one of llm.presets"
        raise ThinkModeConfigError(msg)
    try:
        # A missing pattern never matches, so without `off_words` only the pause ends the mode.
        on, off = (
            re.compile(str(block.get(key) or "(?!)"), re.IGNORECASE)
            for key in ("on_words", "off_words")
        )
    except re.error as exc:
        msg = f"llm.think: invalid pattern: {exc}"
        raise ThinkModeConfigError(msg) from exc
    return ThinkMode(preset=preset, on=on, off=off)
