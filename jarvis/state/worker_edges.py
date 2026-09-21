"""L2 ``worker_edges`` — which Codex threads this daemon opened, and for whom (ADR 0019).

Topology only: ``parent`` is the ``action_id`` of the ``spawn_worker`` call,
``child`` the app-server thread id, ``status`` ``open`` until ``close_worker``
or daemon shutdown marks it ``closed``. Bounded operational state in the
Event Log file, like the outbox and the receipts: not a projection of events,
and the only writer is this module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterable

_SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS worker_edges (
    parent TEXT NOT NULL,
    child  TEXT PRIMARY KEY,
    status TEXT NOT NULL
)
"""


def open_edge(conn: sqlite3.Connection, *, parent: str, child: str) -> None:
    """Record that ``parent`` opened thread ``child``."""
    conn.execute(_SCHEMA)
    conn.execute("INSERT INTO worker_edges VALUES (?, ?, 'open')", (parent, child))
    conn.commit()


def close_edges(conn: sqlite3.Connection, children: Iterable[str]) -> None:
    """Mark the given threads closed; unknown ids are ignored."""
    conn.execute(_SCHEMA)
    conn.executemany(
        "UPDATE worker_edges SET status = 'closed' WHERE child = ?",
        [(child,) for child in children],
    )
    conn.commit()
