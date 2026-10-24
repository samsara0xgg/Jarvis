"""What the devices said, as the log keeps it: the ``utterance.received`` rows (ADR 0349).

The runtime's voice paths (ADR 0219, 0268, 0278) ask which sentences another device or an earlier
turn of one device recorded. Each function says what it answers and the SQL stays here, beside the
shape of the row it reads. A device is the row's ``ingestion_node``: a paired phone's name, else
``mac``. An utterance whose turn id or words are not text is left out, as it never names a turn.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3


@dataclass(frozen=True)
class Utterance:
    """One sentence a device recorded: the turn it began, the words, and when (epoch ms)."""

    turn_id: str
    transcript: str
    at_ms: int


_UTTERANCE_FIELDS = (
    "SELECT json_extract(payload_json, '$.turn_id'), "
    "json_extract(payload_json, '$.transcript'), ts_epoch_ms FROM events "
)


def _utterances(rows: sqlite3.Cursor) -> list[Utterance]:
    return [
        Utterance(turn_id, transcript, int(at_ms))
        for turn_id, transcript, at_ms in rows
        if isinstance(turn_id, str) and isinstance(transcript, str)
    ]


def transcripts_heard_elsewhere_since(
    conn: sqlite3.Connection, device: str, *, since_ms: int,
) -> list[str]:
    """The words of each utterance recorded since ``since_ms`` by a device other than ``device``.

    Whatever channel it came in on; ``since_ms`` itself counts.
    """
    return [
        transcript
        for (transcript,) in conn.execute(
            "SELECT json_extract(payload_json, '$.transcript') FROM events "
            "WHERE type = 'utterance.received' AND ts_epoch_ms >= ? AND ingestion_node != ?",
            (since_ms, device),
        )
        if isinstance(transcript, str)
    ]


def first_utterance_row(conn: sqlite3.Connection, turn_id: str) -> int | None:
    """The log row id of the first sentence the turn heard, or ``None`` if it heard none."""
    row = conn.execute(
        "SELECT id FROM events WHERE type = 'utterance.received' "
        "AND json_extract(payload_json, '$.turn_id') = ? ORDER BY id LIMIT 1",
        (turn_id,),
    ).fetchone()
    return None if row is None else int(row[0])


def utterances_before(conn: sqlite3.Connection, device: str, *, row_id: int) -> list[Utterance]:
    """The last 20 sentences ``device`` recorded in rows before ``row_id``, newest first."""
    return _utterances(conn.execute(
        _UTTERANCE_FIELDS
        + "WHERE type = 'utterance.received' AND ingestion_node = ? AND id < ? "
        "ORDER BY id DESC LIMIT 20",
        (device, row_id),
    ))


def latest_utterances(conn: sqlite3.Connection, device: str) -> list[Utterance]:
    """The last 20 sentences ``device`` recorded, newest first."""
    return _utterances(conn.execute(
        _UTTERANCE_FIELDS
        + "WHERE type = 'utterance.received' AND ingestion_node = ? ORDER BY id DESC LIMIT 20",
        (device,),
    ))
