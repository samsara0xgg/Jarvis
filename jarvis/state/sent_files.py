"""The rows of the log that name files he sent (ADR 0349; ADR 0211, 0223, 0255).

A phone attaches files to a turn, or shares them from another app. The row carries the stored
files' ids in its payload ``attachments`` (:mod:`jarvis.state.attachments`); the bytes stay in
their files. The runtime links the records of those turns to the files, and writes a note for
each picture; both look the rows up here, so the SQL stays beside the shape of the row.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from jarvis.state.event_log import EVENT_COLUMNS, row_to_event

if TYPE_CHECKING:
    import sqlite3

    from jarvis.shared import Event

_TURN_ROWS: Final = ("surface.user_intent", "utterance.received")
"""The rows that open one of his turns: typed words, or words heard."""
_ROWS: Final = (*_TURN_ROWS, "user.shared")
"""The rows that can name files he sent: the opening row of a turn, or a saved share."""


def turn_rows_naming_files_since(conn: sqlite3.Connection, *, since_ms: int) -> list[Event]:
    """The opening rows of his turns at or after ``since_ms`` that name files, oldest first."""
    marks = ",".join("?" * len(_TURN_ROWS))
    return [
        row_to_event(row)
        for row in conn.execute(
            f"SELECT {EVENT_COLUMNS} FROM events "  # noqa: S608 - the columns are a constant, marks is "?, ?".
            f"WHERE type IN ({marks}) AND ts_epoch_ms >= ? "
            "AND json_extract(payload_json, '$.attachments') IS NOT NULL ORDER BY id",
            (*_TURN_ROWS, since_ms),
        )
    ]


def rows_naming_files_between(
    conn: sqlite3.Connection, *, since_ms: int, until_ms: int,
) -> list[Event]:
    """The rows naming files that he sent, or saved from another app, in ``[since_ms, until_ms]``.

    That is the opening rows of his turns and the ``user.shared`` rows, oldest first.
    """
    marks = ",".join("?" * len(_ROWS))
    return [
        row_to_event(row)
        for row in conn.execute(
            f"SELECT {EVENT_COLUMNS} FROM events "  # noqa: S608 - the columns are a constant, marks is "?, ?, ?".
            f"WHERE type IN ({marks}) AND ts_epoch_ms >= ? AND ts_epoch_ms <= ? "
            "AND json_extract(payload_json, '$.attachments') IS NOT NULL ORDER BY id",
            (*_ROWS, since_ms, until_ms),
        )
    ]
