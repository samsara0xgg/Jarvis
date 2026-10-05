"""Which of Allen's turns overlap: one still being answered, words said since.

ADR 0107. A turn whose tool is slow can still be
running when Allen says something else. The newer turn reads the older one
here so it answers only the new words; the older turn reads the words said
since its own so its late answer opens by pointing back at its question.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    import sqlite3

_INPUT_TYPES: Final[tuple[str, str]] = ("utterance.received", "surface.user_intent")
# A turn is over once it ended, failed, or had its run cancelled (ADR 0074
# folds a cancelled turn's words into the next one). A completed run is not
# enough: a commentary run completes while its turn goes on.
_TURN_CLOSED_TYPES: Final[tuple[str, str, str]] = (
    "turn.ended",
    "turn.failed",
    "response.cancelled",
)


@dataclass(frozen=True)
class TurnInFlight:
    """An earlier turn of Allen's words that has no answer yet."""

    turn_id: str
    words: str
    asked_ms: int


def turns_in_flight(
    conn: sqlite3.Connection,
    *,
    trigger_event_uid: str,
    since_ms: int,
) -> tuple[TurnInFlight, ...]:
    """Turns started from Allen's words before this trigger, not yet over.

    Only turns started at or after ``since_ms``: a turn stuck past that is
    left to the supervisor, not named to the model.
    """
    rows = conn.execute(
        f"""
        SELECT json_extract(s.payload_json, '$.turn_id'),
               json_extract(t.payload_json, '$.transcript'),
               t.ts_epoch_ms
        FROM events s JOIN events t ON t.event_uid = s.source_event_id
        WHERE s.type = 'turn.started' AND s.ts_epoch_ms >= ?
          AND t.type IN ({", ".join("?" * len(_INPUT_TYPES))})
          AND t.id < (SELECT id FROM events WHERE event_uid = ?)
          AND NOT EXISTS (
            SELECT 1 FROM events e
            WHERE e.type IN ({", ".join("?" * len(_TURN_CLOSED_TYPES))})
              AND e.ts_epoch_ms >= s.ts_epoch_ms
              AND json_extract(e.payload_json, '$.turn_id')
                  = json_extract(s.payload_json, '$.turn_id')
          )
        ORDER BY t.id
        """,  # noqa: S608 - only placeholders are interpolated
        (since_ms, *_INPUT_TYPES, trigger_event_uid, *_TURN_CLOSED_TYPES),
    ).fetchall()
    return tuple(
        TurnInFlight(turn_id=str(turn_id), words=words.strip(), asked_ms=int(asked_ms))
        for turn_id, words, asked_ms in rows
        if isinstance(words, str) and words.strip()
    )


def any_turn_in_flight(conn: sqlite3.Connection, *, since_ms: int) -> bool:
    """Whether a turn started from Allen's words at or after ``since_ms`` is not yet over."""
    return (
        conn.execute(
            f"""
            SELECT 1 FROM events s JOIN events t ON t.event_uid = s.source_event_id
            WHERE s.type = 'turn.started' AND s.ts_epoch_ms >= ?
              AND t.type IN ({", ".join("?" * len(_INPUT_TYPES))})
              AND NOT EXISTS (
                SELECT 1 FROM events e
                WHERE e.type IN ({", ".join("?" * len(_TURN_CLOSED_TYPES))})
                  AND e.ts_epoch_ms >= s.ts_epoch_ms
                  AND json_extract(e.payload_json, '$.turn_id')
                      = json_extract(s.payload_json, '$.turn_id')
              )
            LIMIT 1
            """,  # noqa: S608 - only placeholders are interpolated
            (since_ms, *_INPUT_TYPES, *_TURN_CLOSED_TYPES),
        ).fetchone()
        is not None
    )


# What a turn and its sound leave on the log: input, the turn's own rows, the answer,
# and the playback rows (checkpoints keep coming for as long as it is speaking).
_ACTIVITY_TYPES: Final[tuple[str, ...]] = (
    *_INPUT_TYPES,
    "turn.started",
    *_TURN_CLOSED_TYPES,
    "response.started",
    "response.completed",
    "surface.playback_started",
    "surface.playback_checkpoint",
    "surface.playback_completed",
    "surface.playback_interrupted",
    "surface.playback_failed",
)


def turn_activity_since(conn: sqlite3.Connection, *, since_ms: int) -> bool:
    """Whether Allen's input, a turn, its answer or its playback wrote a row since ``since_ms``."""
    return (
        conn.execute(
            f"""
            SELECT 1 FROM events
            WHERE type IN ({", ".join("?" * len(_ACTIVITY_TYPES))}) AND ts_epoch_ms >= ?
            LIMIT 1
            """,  # noqa: S608 - only placeholders are interpolated
            (*_ACTIVITY_TYPES, since_ms),
        ).fetchone()
        is not None
    )


def words_since(conn: sqlite3.Connection, *, trigger_event_uid: str) -> tuple[str, ...]:
    """What Allen said or typed after this trigger, oldest first."""
    rows = conn.execute(
        f"""
        SELECT json_extract(payload_json, '$.transcript') FROM events
        WHERE type IN ({", ".join("?" * len(_INPUT_TYPES))})
          AND id > (SELECT id FROM events WHERE event_uid = ?)
        ORDER BY id
        """,  # noqa: S608 - only placeholders are interpolated
        (*_INPUT_TYPES, trigger_event_uid),
    ).fetchall()
    return tuple(words.strip() for (words,) in rows if isinstance(words, str) and words.strip())
