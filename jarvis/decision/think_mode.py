"""L3 thinking mode (ADR 0108): Allen's own words make one turn think.

A word from ``llm.think.on_words`` in the sentence that opens a turn gives that turn
``llm.think.preset``; every other turn uses the default preset. Nothing is stored and nothing
carries over from one turn to the next, so there is no mode to switch off or to lose in a restart.
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

    from jarvis.shared import Event

_USER_WORDS: Final = ("surface.user_intent", "utterance.received")
_TURN_END: Final = ("response.completed", "response.cancelled", "response.failed")
# A turn whose response has not ended after this long is lost: a crash writes no end event.
# It also bounds how far back the log is read.
_LOST_TURN_MS: Final = 10 * 60 * 1000


class ThinkModeConfigError(ValueError):
    """``llm.think`` names a missing preset or holds an invalid pattern."""


@dataclass(frozen=True)
class ThinkMode:
    """``llm.think``: the preset a thinking turn uses and the words that choose it."""

    preset: str
    on: re.Pattern[str]

    def preset_for(self, conn: sqlite3.Connection, turn: Event) -> str | None:
        """:attr:`preset` when the sentence ``turn`` answers holds an on-word, else ``None``."""
        if turn.type not in _USER_WORDS:
            return None
        events = _recent(conn, turn.ts_epoch_ms)
        return self.preset if self._holds_on_word(events, turn) else None

    def status(self, conn: sqlite3.Connection) -> dict[str, Any]:
        """ADR 0108: ``GET /inherent/think``, the turn that is thinking now, or ``None``."""
        turn_id = self._thinking_turn(_recent(conn, int(time.time() * 1000)))
        return {"on": turn_id is not None, "on_words": self.on.pattern, "turn_id": turn_id}

    def _holds_on_word(self, events: list[Event], turn: Event) -> bool:
        """An on-word in ``turn``'s words, or in an utterance ADR 0074 folded into it.

        A sentence Allen paused in arrives as two utterances; the first one's answer is
        cancelled as ``superseded`` and the second turn answers both.
        """
        superseded = {
            event.payload.get("turn_id")
            for event in events
            if event.type == "response.cancelled" and event.payload.get("reason") == "superseded"
        }
        words = [event for event in events if event.type in _USER_WORDS]
        last = next((i for i, event in enumerate(words) if event.event_uid == turn.event_uid), None)
        if last is None:  # not in the window: only its own words
            words, last = [turn], 0
        first = last
        while first > 0 and words[first - 1].payload.get("turn_id") in superseded:
            first -= 1
        return any(
            isinstance(said := event.payload.get("transcript"), str) and self.on.search(said)
            for event in words[first : last + 1]
        )

    def _thinking_turn(self, events: list[Event]) -> str | None:
        """The newest turn's id when its sentence holds an on-word and a final run is still open."""
        words = [event for event in events if event.type in _USER_WORDS]
        if not words or not self._holds_on_word(events, words[-1]):
            return None
        turn_id = words[-1].payload.get("turn_id")
        # A commentary run completes while its turn goes on, and a failed run may be followed
        # by a correction run: the turn is over once every final run it opened has ended.
        mine = [event for event in events if event.payload.get("turn_id") == turn_id]
        runs = {
            event.payload.get("response_id")
            for event in mine
            if event.type == "response.started" and event.payload.get("phase") == "final"
        }
        ended = {event.payload.get("response_id") for event in mine if event.type in _TURN_END}
        return None if runs and runs <= ended else str(turn_id)


def _recent(conn: sqlite3.Connection, at_ms: int) -> list[Event]:
    """Allen's words and every response's start and end in the ten minutes before ``at_ms``."""
    return list(
        iter_events_of_types(
            conn,
            (*_USER_WORDS, "response.started", *_TURN_END),
            since_epoch_ms=at_ms - _LOST_TURN_MS,
        )
    )


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
        # A missing pattern never matches, so without `on_words` no turn thinks.
        on = re.compile(str(block.get("on_words") or "(?!)"), re.IGNORECASE)
    except re.error as exc:
        msg = f"llm.think: invalid pattern: {exc}"
        raise ThinkModeConfigError(msg) from exc
    return ThinkMode(preset=preset, on=on)
