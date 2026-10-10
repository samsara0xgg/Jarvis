"""ADR 0219: a device's voice acts only on its own turns, and words heard twice make one turn.

Two readers of the event log that the runtime binds to the voice paths of every device:

* :func:`device_turns` answers "does this turn belong to device X", from the row that opened
  the turn (:func:`jarvis.state.event_log.turn_origin`, ADR 0212/0217). The voice-driven
  cancels (barge-in, supersede, exit) take it as ``only_turns``.
* :func:`heard_elsewhere` answers "did another device just record these words", the check the
  shared word judge runs first (:mod:`jarvis.surface.word_judge`, verdict ``elsewhere``).
"""

from __future__ import annotations

import contextlib
import difflib
import time
import unicodedata
from typing import TYPE_CHECKING, Final

from jarvis.state.event_log import MAC_NODE, open_runtime_event_log, turn_origin
from jarvis.surface import voice_asr

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable
    from pathlib import Path

ELSEWHERE_WINDOW_S: Final = 8.0
"""How long another device's utterance still counts as the same sentence (ADR 0219)."""
ELSEWHERE_SIMILARITY: Final = 0.6
"""``difflib`` ratio at which two recognizers' transcripts are the same words (ADR 0219)."""
_MIN_CHARS: Final = 2
_OPEN_DEADLINE_S: Final = 0.25
"""Bounds the capture worker's open of the log, as ``recent_speech`` does."""


def turn_is_device_turn(
    conn: sqlite3.Connection, turn_id: str, device: str, *, unowned: bool = False,
) -> bool:
    """Whether the row that opened ``turn_id`` was written under ``device``.

    ``unowned`` is the answer for a turn with no opening row (background and reconciliation
    turns): the host's own voice treats them as its own, a remote device's never touches them.
    """
    node = turn_origin(conn, turn_id)[1]
    return unowned if node is None else node == device


def device_turns(
    event_log_path: Path, device: str, *, unowned: bool = False,
) -> Callable[[str], bool]:
    """``only_turns`` for ``device``: the turns it opened. Opens the log once per turn asked."""

    def _is_device_turn(turn_id: str) -> bool:
        with contextlib.closing(open_runtime_event_log(event_log_path)) as conn:
            return turn_is_device_turn(conn, turn_id, device, unowned=unowned)

    return _is_device_turn


def host_turns(event_log_path: Path) -> Callable[[str], bool]:
    """``only_turns`` for the host's own voice: the Mac's turns and those with no opening row."""
    return device_turns(event_log_path, MAC_NODE, unowned=True)


def _normalize(text: str) -> str:
    """Casefolded letters and digits of ``text`` after its wake phrase, nothing else."""
    return "".join(
        char for char in voice_asr.strip_wake_lead(text).casefold()
        if unicodedata.category(char)[0] in {"L", "N"}
    )


def same_words(left: str, right: str) -> bool:
    """Whether two recognizers' transcripts are one sentence (ADR 0219)."""
    a, b = _normalize(left), _normalize(right)
    if min(len(a), len(b)) < _MIN_CHARS:
        return False
    return a in b or b in a or difflib.SequenceMatcher(None, a, b).ratio() >= ELSEWHERE_SIMILARITY


def heard_elsewhere(event_log_path: Path, device: str, text: str) -> bool:
    """Whether a device other than ``device`` recorded these words as a voice utterance lately.

    The first device to record a sentence wins; the later one is absorbed. Any
    ``utterance.received`` of the last :data:`ELSEWHERE_WINDOW_S` seconds under another
    ``ingestion_node`` counts, whatever its channel.
    """
    since_ms = int((time.time() - ELSEWHERE_WINDOW_S) * 1000)
    with contextlib.closing(
        open_runtime_event_log(event_log_path, deadline=time.monotonic() + _OPEN_DEADLINE_S),
    ) as conn:
        rows = conn.execute(
            "SELECT json_extract(payload_json, '$.transcript') FROM events "
            "WHERE type = 'utterance.received' AND ts_epoch_ms >= ? AND ingestion_node != ?",
            (since_ms, device),
        ).fetchall()
    return any(isinstance(row[0], str) and same_words(text, row[0]) for row in rows)
