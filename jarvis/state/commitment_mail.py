"""Which mails the commitment poller has read, and what it was given and said (ADR 0274).

One additive table in memory.db (``CREATE TABLE IF NOT EXISTS``, so no schema version bump):
``commitment_mail`` holds a row per mail looked at. Only ``error`` rows are looked at again, up
to ``MAX_ERROR_TRIES`` times; the commitments themselves are events (``jarvis.state.commitments``).
"""

from __future__ import annotations

from contextlib import closing
from typing import TYPE_CHECKING, Final

from jarvis.state.memory_db import open_memory_db

if TYPE_CHECKING:
    import sqlite3
    from datetime import datetime
    from pathlib import Path

VERDICTS: Final[tuple[str, ...]] = ("filtered", "none", "noted", "interview", "error")
MAX_ERROR_TRIES: Final[int] = 3
_SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS commitment_mail (
    message_id  TEXT PRIMARY KEY,
    at          TEXT,
    verdict     TEXT,
    tries       INTEGER DEFAULT 0,
    input_json  TEXT,
    output_json TEXT
);
"""


def _open(path: Path) -> sqlite3.Connection:
    conn = open_memory_db(path)
    conn.executescript(_SCHEMA)
    return conn


def seen_ids(path: Path) -> set[str]:
    """Ids not to read again: every verdict but ``error``, and an error tried three times."""
    with closing(_open(path)) as conn:
        rows = conn.execute(
            "SELECT message_id FROM commitment_mail WHERE verdict != 'error' OR tries >= ?",
            (MAX_ERROR_TRIES,),
        )
        return {row[0] for row in rows}


def record(  # noqa: PLR0913 - the row's fields
    path: Path,
    message_id: str,
    verdict: str,
    now: datetime,
    *,
    input_json: str = "",
    output_json: str = "",
) -> None:
    """Remember a mail was read; an error counts its tries, any other verdict ends them."""
    if verdict not in VERDICTS:
        msg = f"not a commitment mail verdict: {verdict!r}"
        raise ValueError(msg)
    with closing(_open(path)) as conn, conn:
        conn.execute(
            "INSERT INTO commitment_mail (message_id, at, verdict, tries, input_json, output_json)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(message_id) DO UPDATE SET at = excluded.at, verdict = excluded.verdict,"
            " tries = commitment_mail.tries + excluded.tries, input_json = excluded.input_json,"
            " output_json = excluded.output_json",
            (
                message_id, now.isoformat(timespec="seconds"), verdict,
                1 if verdict == "error" else 0, input_json, output_json,
            ),
        )
