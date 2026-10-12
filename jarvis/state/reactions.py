"""ADR 0256: the emoji he and she put on messages, and the lines that tell the model.

A reaction is an event ``conversation.reaction {target, by, emoji|null}``: ``target`` is the id of
a message record (his, or her answer), ``by`` is ``allen`` or ``jarvis``, ``emoji`` is null when
it is taken off. The newest event per (target, by) is the reaction now.

The model learns of one the way it learns of everything, from the history: a record appended
after the fact, never an edit of the message it is on (ADR 0044's front stays byte-stable). His
is a ``user`` line, hers an ``assistant`` line; neither is a row of the conversation window.

Layer rules: ``jarvis.state`` and ``jarvis.shared`` only (L2).
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Final

from jarvis.state.event_log import emit_event
from jarvis.state.memory_db import (
    ALLEN_REACTION_SOURCE,
    JARVIS_REACTION_SOURCE,
    append_record,
    message_record,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterable
    from pathlib import Path

    from jarvis.shared import Event

EVENT: Final = "conversation.reaction"
ALLEN: Final = "allen"
JARVIS: Final = "jarvis"
EYES: Final = "\U0001f440"
"""What the program puts on a message that arrived while another was being answered."""
EXCERPT_CHARS: Final = 60
"""How much of the message his reaction line quotes."""

Reactions = dict[str, dict[str, str]]
"""Record id -> ``{"allen": emoji, "jarvis": emoji}`` of the reactions that are on it now."""


def excerpt(words: str) -> str:
    """The first characters of a message on one line, as his reaction line quotes it."""
    return " ".join(words.split())[:EXCERPT_CHARS]


def allen_line(emoji: str, words: str, *, took_back: bool = False) -> str:
    """The history line for his reaction, or for taking it back."""
    quoted = excerpt(words)
    if took_back:
        return f"[He took back {emoji} from: «{quoted}»]"
    return f"[He reacted {emoji} to: «{quoted}»]"


def jarvis_line(emoji: str) -> str:
    """The history line for her reaction."""
    return f"[You reacted {emoji}]"


def current(conn: sqlite3.Connection, targets: Iterable[str]) -> Reactions:
    """The reactions on ``targets`` now: the newest event per (target, by), a null one dropped."""
    found: Reactions = {}
    for (payload,) in conn.execute(
        "SELECT payload_json FROM events WHERE type = ? "
        "AND json_extract(payload_json, '$.target') IN (SELECT value FROM json_each(?)) "
        "ORDER BY id",
        (EVENT, json.dumps(list(targets))),
    ):
        one = json.loads(payload)
        by_who = found.setdefault(str(one["target"]), {})
        if one["emoji"] is None:
            by_who.pop(str(one["by"]), None)
        else:
            by_who[str(one["by"])] = str(one["emoji"])
    return {target: by_who for target, by_who in found.items() if by_who}


def emit(conn: sqlite3.Connection, *, target: str, by: str, emoji: str | None) -> Event:
    """Append the event that puts ``emoji`` on ``target`` for ``by``, or takes it off."""
    return emit_event(
        conn,
        type=EVENT,
        payload={"target": target, "by": by, "emoji": emoji},
        actor="user" if by == ALLEN else "jarvis_runtime",
    )


def allen_reacts(
    conn: sqlite3.Connection, memory_db: Path, *, target: str, emoji: str | None,
) -> bool:
    """He puts ``emoji`` on a message, or takes his off (``None``); whether anything changed.

    Appends the line the model reads, then the event. Never starts a turn.

    Raises:
        LookupError: ``target`` is not a message record of his or hers.
    """
    message = message_record(memory_db, target)
    if message is None:
        msg = "that is not a message to react to"
        raise LookupError(msg)
    before = current(conn, [target]).get(target, {}).get(ALLEN)
    if emoji == before:
        return False
    shown = before if emoji is None else emoji
    append_record(
        memory_db,
        record_id=f"reaction:{uuid.uuid4().hex}",
        source=ALLEN_REACTION_SOURCE,
        text=allen_line(str(shown), message[1], took_back=emoji is None),
    )
    emit(conn, target=target, by=ALLEN, emoji=emoji)
    return True


def jarvis_reacts(
    conn: sqlite3.Connection, memory_db: Path | None, *, target: str, emoji: str, record_id: str,
) -> None:
    """She puts ``emoji`` on his message; ``record_id`` makes a retried turn write one line."""
    if memory_db is not None:
        append_record(
            memory_db, record_id=record_id, source=JARVIS_REACTION_SOURCE, text=jarvis_line(emoji),
        )
    emit(conn, target=target, by=JARVIS, emoji=emoji)
