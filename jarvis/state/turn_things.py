"""Which records of his the post-turn pass has taken up, and what it was given and said (ADR 0300).

One additive table in memory.db (``CREATE TABLE IF NOT EXISTS``, so no schema version bump):
``turn_things`` holds a row per record of his the pass knows of. ``pending`` is work owed: the loop
reads the record when her answer and its pictures' notes are in, or their wait is over. ``none``
and ``written`` end it; an ``error`` is owed again after a pause, up to ``MAX_ERROR_TRIES`` times.
What a record wrote is in the event log (``thing.*`` by the pass, ``doings.stated``); the row keeps
what the model was given and said, and the extractor that said it.

Also here, because they read the same records: :func:`read` gives one record of his with the few
messages before it, her answer to it and the notes of its pictures.

Layer rules: ``jarvis.state`` and ``jarvis.shared`` only (L2).
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Final

from jarvis.state import picture_notes
from jarvis.state.memory_db import open_memory_db

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

VERDICTS: Final[tuple[str, ...]] = ("pending", "none", "written", "error")
MAX_ERROR_TRIES: Final[int] = 3
CONTEXT_BEFORE: Final[int] = 6
"""How many messages (his and hers) before his record the model is given."""
_ANSWER_ROWS: Final[int] = 40
"""How many rows after his record are looked through for her answer."""
_HER: Final[tuple[str, ...]] = ("jarvis", "jarvis_live")
_CONVERSATION: Final[tuple[str, ...]] = ("allen", *_HER)
_SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS turn_things (
    record_id   TEXT PRIMARY KEY,
    at          TEXT,
    verdict     TEXT,
    tries       INTEGER DEFAULT 0,
    extractor   TEXT,
    input_json  TEXT,
    output_json TEXT
);
"""


@dataclass(frozen=True)
class Said:
    """One message of the conversation before his record: ``who`` is ``him`` or ``her``."""

    who: str
    at: datetime
    text: str


@dataclass(frozen=True)
class Turn:
    """One record of his as the pass reads it.

    ``words`` is what he said, without the marker line of files and the program's filler.
    ``pictures`` are the images that came with it and ``notes`` the notes that exist for them.
    ``answer`` is her words after it (up to his next record once she has spoken) and
    ``answered_at`` when she last spoke in it; both empty while she has not.
    """

    record_id: str
    at: datetime
    words: str
    pictures: tuple[str, ...]
    notes: tuple[str, ...]
    before: tuple[Said, ...]
    answer: str
    answered_at: datetime | None


def _open(path: Path) -> sqlite3.Connection:
    conn = open_memory_db(path)
    conn.executescript(_SCHEMA)
    return conn


def has_marks(path: Path) -> bool:
    """Whether the pass has taken up any record yet: false only on its first start."""
    with closing(_open(path)) as conn:
        return conn.execute("SELECT 1 FROM turn_things LIMIT 1").fetchone() is not None


def rowid_since(path: Path, since: datetime) -> int:
    """The row id of the first record at or after ``since``; one past the last when none is."""
    with closing(open_memory_db(path)) as conn:
        found = conn.execute(
            "SELECT MIN(rowid) FROM records WHERE datetime(ts) >= datetime(?)",
            (since.isoformat(),),
        ).fetchone()[0]
        if found is not None:
            return int(found)
        return int(conn.execute("SELECT COALESCE(MAX(rowid), 0) + 1 FROM records").fetchone()[0])


def queue(path: Path, now: datetime, *, from_rowid: int) -> tuple[int, int]:
    """Mark every record of his from row ``from_rowid`` on that has no row yet as pending.

    Returns how many were marked and the row id to go on from next time.
    """
    with closing(_open(path)) as conn, conn:
        before = conn.total_changes
        conn.execute(
            "INSERT OR IGNORE INTO turn_things (record_id, at, verdict, tries) "
            "SELECT id, ?, 'pending', 0 FROM records WHERE rowid >= ? AND source = 'allen' "
            "ORDER BY rowid",
            (now.isoformat(timespec="seconds"), from_rowid),
        )
        queued = conn.total_changes - before
        last = conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM records").fetchone()[0]
    return queued, int(last) + 1


def owed(path: Path, retry_before: datetime) -> list[str]:
    """The records to read, oldest first: pending ones, and errors tried before ``retry_before``."""
    with closing(_open(path)) as conn:
        rows = conn.execute(
            "SELECT t.record_id FROM turn_things t JOIN records r ON r.id = t.record_id "
            "WHERE t.verdict = 'pending' OR (t.verdict = 'error' AND t.tries < ? "
            "AND datetime(t.at) <= datetime(?)) ORDER BY r.rowid",
            (MAX_ERROR_TRIES, retry_before.isoformat()),
        )
        return [str(row[0]) for row in rows]


def mark(  # noqa: PLR0913 - the row's fields
    path: Path,
    record_id: str,
    verdict: str,
    now: datetime,
    *,
    extractor: str = "",
    input_json: str = "",
    output_json: str = "",
) -> None:
    """Say what became of a record; an error counts its tries, any other verdict ends them."""
    if verdict not in VERDICTS:
        msg = f"not a turn things verdict: {verdict!r}"
        raise ValueError(msg)
    with closing(_open(path)) as conn, conn:
        conn.execute(
            "INSERT INTO turn_things (record_id, at, verdict, tries, extractor, input_json,"
            " output_json) VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(record_id) DO UPDATE SET at = excluded.at, verdict = excluded.verdict,"
            " tries = turn_things.tries + excluded.tries, extractor = excluded.extractor,"
            " input_json = excluded.input_json, output_json = excluded.output_json",
            (
                record_id, now.isoformat(timespec="seconds"), verdict,
                1 if verdict == "error" else 0, extractor, input_json, output_json,
            ),
        )


def read(path: Path, record_id: str) -> Turn | None:
    """The record ``record_id`` as the pass reads it; ``None`` when it is not a record of his."""
    with closing(open_memory_db(path)) as conn:
        row = conn.execute(
            "SELECT rowid, ts, source, text FROM records WHERE id = ?", (record_id,),
        ).fetchone()
        if row is None or row[2] != "allen":
            return None
        rowid, ts, _, text = row
        pictures = tuple(
            str(one) for (one,) in conn.execute(
                "SELECT attachment_id FROM record_attachments WHERE record_id = ? "
                "AND kind = 'image' ORDER BY pos",
                (record_id,),
            )
        )
        earlier = conn.execute(
            "SELECT source, ts, text FROM records WHERE rowid < ? AND source IN (?, ?, ?) "
            "ORDER BY rowid DESC LIMIT ?",
            (rowid, *_CONVERSATION, CONTEXT_BEFORE),
        ).fetchall()
        later = conn.execute(
            "SELECT source, ts, text FROM records WHERE rowid > ? ORDER BY rowid LIMIT ?",
            (rowid, _ANSWER_ROWS),
        ).fetchall()
    shown = (
        Said("him" if source == "allen" else "her", datetime.fromisoformat(str(at)), body)
        for source, at, text_ in reversed(earlier)
        if (body := picture_notes.words_of(str(text_)) if source == "allen" else str(text_))
    )
    spoken = _answer(later)
    found = picture_notes.bodies(path, pictures)
    return Turn(
        record_id=record_id,
        at=datetime.fromisoformat(str(ts)),
        words=picture_notes.words_of(str(text)),
        pictures=pictures,
        notes=tuple(found[one] for one in pictures if one in found),
        before=tuple(shown),
        answer="\n".join(words for _, words in spoken),
        answered_at=spoken[-1][0] if spoken else None,
    )


def _answer(rows: list[tuple[str, str, str]]) -> list[tuple[datetime, str]]:
    """Her rows after his record: all of them up to his next record once she has spoken."""
    spoken: list[tuple[datetime, str]] = []
    for source, at, text in rows:
        if source == "allen":
            if spoken:
                break
        elif source in _HER:
            spoken.append((datetime.fromisoformat(str(at)), str(text)))
    return spoken
