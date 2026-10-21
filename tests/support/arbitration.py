"""Event-log rows of one response (open, chunk, emitted), as the decision layer emits them."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from jarvis.state.event_log import emit_event

if TYPE_CHECKING:
    import sqlite3

    from jarvis.shared import Event


def _emit(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    payload: dict[str, object],
) -> list[tuple[int, Event]]:
    """Emit one response event and keep its Event Log row id."""
    event = emit_event(conn, type=event_type, payload=payload)
    row = conn.execute(
        "SELECT id FROM events WHERE event_uid = ?",
        (event.event_uid,),
    ).fetchone()
    assert row is not None
    return [(int(row[0]), event)]


def _identity(response_id: str, group_id: str, turn_id: str) -> dict[str, object]:
    return {
        "turn_id": turn_id,
        "response_id": response_id,
        "response_group_id": group_id,
        "phase": "final",
        "channel": "speech",
    }


def emit_open_chunk(
    conn: sqlite3.Connection,
    *,
    response_id: str,
    group_id: str,
    turn_id: str,
    text: str,
) -> list[tuple[int, Event]]:
    """Emit open+chunk only; the buffer's row id is the open's row id."""
    base = _identity(response_id, group_id, turn_id)
    return _emit(
        conn,
        event_type="surface.response_open",
        payload={**base, "query": "q", "kind": "text"},
    ) + _emit(
        conn,
        event_type="surface.response_chunk",
        payload={
            **base,
            "text": text,
            "sequence": 0,
            "segment_hash": hashlib.sha256(text.encode()).hexdigest(),
        },
    )


def emit_terminal(
    conn: sqlite3.Connection,
    *,
    response_id: str,
    group_id: str,
    turn_id: str,
    text: str,
) -> list[tuple[int, Event]]:
    """Emit the terminal response event that schedules the buffer."""
    return _emit(
        conn,
        event_type="surface.response_emitted",
        payload={**_identity(response_id, group_id, turn_id), "text": text},
    )


def emit_whole_response(
    conn: sqlite3.Connection,
    *,
    response_id: str,
    group_id: str,
    turn_id: str,
    text: str,
) -> list[tuple[int, Event]]:
    """Emit one complete response: open, chunk, emitted."""
    identity = {"response_id": response_id, "group_id": group_id, "turn_id": turn_id}
    return emit_open_chunk(conn, text=text, **identity) + emit_terminal(
        conn,
        text=text,
        **identity,
    )
