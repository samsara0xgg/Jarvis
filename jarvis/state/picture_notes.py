"""ADR 0223: the note in words that a picture he sent leaves in memory.db.

A note is an ordinary record, appended after its turn, so history, recall, the day summary, the
core-memory review and the ledger read it with no special case. It is linked to its picture by the
record id (:func:`note_id`), and its first line names the picture so the model can look at it again
(``look_at_picture``). Erasing the pictures erases the notes (:func:`delete_all`).

Also here, because they read the same records: what he said last in words, for the language an
attachment-only turn is answered in, and what he said next, for the note's correction window.

Layer rules: ``jarvis.state`` and ``jarvis.shared`` only (L2).
"""

from __future__ import annotations

import json
import re
from contextlib import closing
from typing import TYPE_CHECKING, Final

from jarvis.shared import lang
from jarvis.state.attachments import short_id
from jarvis.state.memory_db import (
    NOTE_ID_PREFIX,
    PICTURE_NOTE_SOURCE,
    append_record,
    open_memory_db,
)

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from jarvis.state.attachments import AttachmentRef

SOURCE: Final = PICTURE_NOTE_SOURCE
"""The record source; the conversation window leaves it out, the prompt history keeps it."""
NOTE_MAX_CHARS: Final = 700
"""A note is short: what the picture shows, its text, and what he said about it."""

_ID_PREFIX: Final = NOTE_ID_PREFIX
_HEX_PREFIX: Final = re.compile(r"[0-9a-f]{8,32}")
_HEADER_OPEN: Final = "[Picture note"
_ATTACHED: Final = "[attached:"
_LAST_WORDS_SCAN: Final = 50
"""How many of his earlier records are looked through for one that has words."""


def note_id(attachment_id: str) -> str:
    """The record id of a picture's note: one per picture, so a retry writes nothing twice."""
    return f"{_ID_PREFIX}{attachment_id}"


def append(path: Path, ref: AttachmentRef, body: str) -> None:
    """Append the note of ``ref``; the first one written stays."""
    text = f"{_HEADER_OPEN} · {ref.kind} {ref.name} · id {short_id(ref.id)}]\n{body.strip()}"
    append_record(path, record_id=note_id(ref.id), source=SOURCE, text=text)


def bodies(path: Path, attachment_ids: Iterable[str]) -> dict[str, str]:
    """The note text (without its header line) of each picture that has one."""
    ids = {note_id(one): one for one in attachment_ids}
    if not ids or not path.is_file():
        return {}
    with closing(open_memory_db(path)) as conn:
        rows = conn.execute(
            "SELECT id, text FROM records WHERE source = ? "
            "AND id IN (SELECT value FROM json_each(?))",
            (SOURCE, json.dumps(list(ids))),
        ).fetchall()
    return {ids[str(rid)]: str(text).partition("\n")[2] for rid, text in rows}


def body_by_prefix(path: Path, prefix: str) -> str | None:
    """The note of the one picture whose id starts with ``prefix`` (8+ hex digits), if any."""
    prefix = prefix.strip().lower()
    if not path.is_file() or _HEX_PREFIX.fullmatch(prefix) is None:
        return None
    with closing(open_memory_db(path)) as conn:
        rows = conn.execute(
            "SELECT text FROM records WHERE source = ? AND id LIKE ?",
            (SOURCE, f"{_ID_PREFIX}{prefix}%"),
        ).fetchall()
    return str(rows[0][0]).partition("\n")[2] if len(rows) == 1 else None


DELETE_SQL: Final = (
    "DELETE FROM records WHERE source = ? AND id NOT IN (SELECT upto_record_id FROM summaries)"
)
"""The one DELETE memory.db allows (``tests/canary/test_no_projection_writes.py`` pins this string).

A note a rolling summary is anchored on stays: history refuses to render past a missing anchor.
No such note exists under ``session.context: day_summaries``, which writes no rolling summary.
"""


def delete_all(path: Path) -> int:
    """Delete every note (the pictures they describe are being erased); how many went."""
    if not path.is_file():
        return 0
    with closing(open_memory_db(path)) as conn, conn:
        return conn.execute(DELETE_SQL, (SOURCE,)).rowcount


def _words_of(text: str) -> str:
    """A record's text without the attachment marker lines and the program's filler sentences."""
    kept = [
        line for line in text.splitlines()
        if line.strip() and not line.startswith(_ATTACHED) and not lang.is_filler(line)
    ]
    return "\n".join(kept).strip()


def answer_after(path: Path, record_id: str) -> str:
    """Her answer to the turn of ``record_id``: her records up to his next turn, else ``""``."""
    if not path.is_file():
        return ""
    with closing(open_memory_db(path)) as conn:
        rows = conn.execute(
            "SELECT source, text FROM records WHERE rowid > "
            "(SELECT rowid FROM records WHERE id = ?) ORDER BY rowid LIMIT 20",
            (record_id,),
        ).fetchall()
    said: list[str] = []
    for source, text in rows:
        if source == "allen":
            break
        if source in ("jarvis", "jarvis_live"):
            said.append(str(text))
    return "\n".join(said)


def last_words(path: Path, before_record_id: str | None) -> str:
    """The words of his latest earlier turn that had any, else ``""``.

    ``None`` looks through all his records, newest first.

    An attachment-only turn is answered in the language of these, never of the filler sentence
    the program wrote in its place (ADR 0223).
    """
    if not path.is_file():
        return ""
    with closing(open_memory_db(path)) as conn:
        rows = conn.execute(
            "SELECT text FROM records WHERE source = 'allen' AND rowid < "
            "COALESCE((SELECT rowid FROM records WHERE id = ?), "
            "CASE WHEN ? IS NULL THEN 9223372036854775807 END) ORDER BY rowid DESC LIMIT ?",
            (before_record_id, before_record_id, _LAST_WORDS_SCAN),
        ).fetchall()
    return next((words for (text,) in rows if (words := _words_of(str(text)))), "")


def next_words(path: Path, after_record_id: str) -> str | None:
    """The words of his first turn after the record, or ``None`` when he has not said one yet.

    ``""`` means a turn came and had no words (another picture alone).
    """
    if not path.is_file():
        return None
    with closing(open_memory_db(path)) as conn:
        row = conn.execute(
            "SELECT text FROM records WHERE source = 'allen' AND rowid > "
            "(SELECT rowid FROM records WHERE id = ?) ORDER BY rowid LIMIT 1",
            (after_record_id,),
        ).fetchone()
    return None if row is None else _words_of(str(row[0]))
