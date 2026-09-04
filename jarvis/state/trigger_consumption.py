"""Durable record of action triggers already folded by a live turn."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS decision_trigger_consumptions ("
        "event_uid TEXT PRIMARY KEY, turn_id TEXT NOT NULL)"
    )
    conn.commit()


def mark_trigger_consumed(conn: sqlite3.Connection, event_uid: str, turn_id: str) -> None:
    """Persist successful semantic consumption before releasing live ownership."""
    _ensure_schema(conn)
    conn.execute(
        "INSERT OR IGNORE INTO decision_trigger_consumptions (event_uid, turn_id) VALUES (?, ?)",
        (event_uid, turn_id),
    )
    conn.commit()


def trigger_was_consumed(conn: sqlite3.Connection, event_uid: str) -> bool:
    """Check durable consumption, including completed turns from older runtimes."""
    _ensure_schema(conn)
    return conn.execute(
        "SELECT 1 FROM decision_trigger_consumptions WHERE event_uid = ? "
        "UNION ALL SELECT 1 FROM events WHERE type = 'turn.ended' AND source_event_id = ? LIMIT 1",
        (event_uid, event_uid),
    ).fetchone() is not None
