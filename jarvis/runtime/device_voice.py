"""ADR 0219: a device's voice acts only on its own turns, and words heard twice make one turn.

Two readers of the event log that the runtime binds to the voice paths of every device:

* :func:`device_turns` answers "does this turn belong to device X", from the row that opened
  the turn (:func:`jarvis.state.event_log.turn_origin`, ADR 0212/0217). The voice-driven
  cancels (barge-in, supersede, exit) take it as ``only_turns``.
* :func:`heard_elsewhere` answers "did another device just record these words", the check the
  shared word judge runs first (:mod:`jarvis.surface.word_judge`, verdict ``elsewhere``).
* :func:`repeat_of` answers "is this utterance the same words as one this device's turn is
  still answering" (ADR 0268), asked where the turn starts.
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
    from collections.abc import Callable, Collection
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


_FILLERS: Final = "嗯哦噢喔唔呃额啊哈"
"""Listening sounds at the edge of a sentence that are not part of what it says."""
_REPEAT_ENDED = (
    "SELECT 1 FROM events WHERE type IN ('response.cancelled', 'response.failed', 'turn.failed') "
    "AND json_extract(payload_json, '$.turn_id') = ? LIMIT 1"
)


def repeats(words: str, earlier: str) -> bool:
    """Whether ``words`` say no more than ``earlier`` did: the same, or a part of it.

    Both are normalized as :func:`same_words` does and stripped of a listening sound at either
    edge. A longer sentence that merely begins with ``earlier`` is a continuation, not a repeat.
    """
    a, b = _normalize(words).strip(_FILLERS), _normalize(earlier).strip(_FILLERS)
    return len(a) >= _MIN_CHARS and a in b


def repeat_of(  # noqa: PLR0913 - the turn, its words, and the three facts only the runtime knows
    event_log_path: Path,
    turn_id: str,
    words: str,
    *,
    window_s: float,
    in_flight: Collection[str],
    usable: Callable[[str], bool],
) -> str | None:
    """The turn of the same device that ``turn_id``'s words repeat, if it is still answering.

    That is the latest earlier voice turn under the same ``ingestion_node`` whose words
    :func:`repeats` accepts, and that either began within ``window_s`` or is in ``in_flight``,
    was not cancelled or failed, and which ``usable`` still lets answer.
    """
    since_ms = int((time.time() - window_s) * 1000)
    with contextlib.closing(open_runtime_event_log(event_log_path)) as conn:
        node = turn_origin(conn, turn_id)[1]
        own = conn.execute(
            "SELECT id FROM events WHERE type = 'utterance.received' "
            "AND json_extract(payload_json, '$.turn_id') = ? ORDER BY id LIMIT 1",
            (turn_id,),
        ).fetchone()
        if node is None or own is None:
            return None
        rows = conn.execute(
            "SELECT json_extract(payload_json, '$.turn_id'), "
            "json_extract(payload_json, '$.transcript'), ts_epoch_ms FROM events "
            "WHERE type = 'utterance.received' AND ingestion_node = ? AND id < ? "
            "ORDER BY id DESC LIMIT 20",
            (node, own[0]),
        ).fetchall()
        for earlier, text, ts_ms in rows:
            if (
                isinstance(earlier, str) and isinstance(text, str) and earlier != turn_id
                and (ts_ms >= since_ms or earlier in in_flight)
                and repeats(words, text) and usable(earlier)
                and conn.execute(_REPEAT_ENDED, (earlier,)).fetchone() is None
            ):
                return earlier
    return None
