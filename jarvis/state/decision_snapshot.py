"""One read transaction for the exact Event Log and operational debt packet."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from jarvis.state.authorization_snapshot import AuthorizationSnapshot, read_authorization_snapshot
from jarvis.state.event_log import iter_events
from jarvis.state.projections import ProjectionSet, fold_projections

if TYPE_CHECKING:
    import sqlite3


@dataclass(frozen=True)
class DecisionStateSnapshot:
    """One event cursor and the facts visible at that same SQLite snapshot."""

    event_cursor: int
    projections: ProjectionSet
    authorizations: AuthorizationSnapshot


def read_decision_snapshot(conn: sqlite3.Connection) -> DecisionStateSnapshot:
    """Borrow a caller's transaction or own a read-only one, never create tables."""
    owned = not conn.in_transaction
    if owned:
        conn.execute("BEGIN")
    try:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()
        cursor = int(row[0])
        events = tuple(iter_events(conn))
        authorizations = read_authorization_snapshot(conn, events)
        return DecisionStateSnapshot(
            event_cursor=cursor,
            projections=fold_projections(events),
            authorizations=authorizations,
        )
    finally:
        if owned:
            # No writes belong to this reader; rollback only ends its read view.
            conn.rollback()
