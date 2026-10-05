"""One read transaction for the exact Event Log and operational debt packet.

The projections are folds of the append-only log, so a reader that already
holds the fold up to event N reads only the rows after N (ADR 0164). The log
stays the only fact: a :class:`DecisionFoldState` is a cache of its prefix, and
a reader that cannot prove its cache is a prefix of what it sees folds the
whole log instead.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from jarvis.state.authorization_snapshot import (
    AuthorizationFacts,
    AuthorizationSnapshot,
    read_authorization_snapshot,
)
from jarvis.state.event_log import iter_events_after
from jarvis.state.projections import (
    ProjectionFold,
    ProjectionSet,
    RefoldRequired,
    fold_projections,
)

if TYPE_CHECKING:
    import sqlite3

    from jarvis.shared import Event

LOGGER = logging.getLogger("jarvis.state.decision_snapshot")


@dataclass(frozen=True)
class DecisionFoldState:
    """Both folds up to one event: its id, and its uid to recognise the same log again.

    Immutable once built: advancing it returns a new value, so one state can be
    held by any number of threads.
    """

    cursor: int
    anchor_uid: str
    projections: ProjectionFold
    authorization: AuthorizationFacts


@dataclass(frozen=True)
class DecisionStateSnapshot:
    """One event cursor and the facts visible at that same SQLite snapshot.

    ``fold_state`` is the state at ``event_cursor`` for the caller to reuse as
    the ``prior`` of its next read. It is ``None`` when the read borrowed a
    caller's transaction (which may hold the caller's own uncommitted rows) or
    when the log could not be folded incrementally.
    """

    event_cursor: int
    projections: ProjectionSet
    authorizations: AuthorizationSnapshot
    fold_state: DecisionFoldState | None = field(default=None, compare=False, repr=False)


def _anchor_matches(conn: sqlite3.Connection, prior: DecisionFoldState, cursor: int) -> bool:
    """The row at the prior cursor is the event the prior folded last: same log, still there."""
    if prior.cursor > cursor:
        return False  # this view is older than the prior; a state may not come from the future
    if prior.cursor == 0:
        return True
    row = conn.execute("SELECT event_uid FROM events WHERE id = ?", (prior.cursor,)).fetchone()
    return row is not None and row[0] == prior.anchor_uid


def _advance(state: DecisionFoldState, rows: list[tuple[int, Event]]) -> DecisionFoldState:
    events = [event for _, event in rows]
    return DecisionFoldState(
        cursor=rows[-1][0] if rows else state.cursor,
        anchor_uid=rows[-1][1].event_uid if rows else state.anchor_uid,
        projections=state.projections.advance(events),
        authorization=state.authorization.advance(events),
    )


_EMPTY = DecisionFoldState(0, "", ProjectionFold(), AuthorizationFacts())


def fold_log_state(
    conn: sqlite3.Connection, cursor: int, prior: DecisionFoldState | None = None,
) -> DecisionFoldState | None:
    """The fold of every event up to ``cursor``, from ``prior`` when it is a prefix of this log.

    Returns ``None`` when the log cannot be folded incrementally. Reads rows of
    the connection's current read view; the caller owns the transaction.
    """
    if prior is not None and _anchor_matches(conn, prior, cursor):
        try:
            return _advance(prior, list(iter_events_after(conn, prior.cursor, cursor)))
        except RefoldRequired:
            pass
    try:
        return _advance(_EMPTY, list(iter_events_after(conn, 0, cursor)))
    except RefoldRequired:
        # Every later read pays a whole-log fold until this shape is folded incrementally.
        LOGGER.warning(
            "decision snapshot cannot fold incrementally at event %d; folding the whole log",
            cursor,
        )
        return None


def read_decision_snapshot(
    conn: sqlite3.Connection,
    prior: DecisionFoldState | None = None,
) -> DecisionStateSnapshot:
    """Borrow a caller's transaction or own a read-only one, never create tables.

    With a ``prior`` that is a prefix of the log this view sees, only the rows
    after its cursor are read; without one (startup), or when it is not a prefix
    (an older view, a different log), the whole log is folded.
    """
    owned = not conn.in_transaction
    if owned:
        conn.execute("BEGIN")
    try:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()
        cursor = int(row[0])
        state = fold_log_state(conn, cursor, prior)
        if state is None:
            # A log shaped so that only the whole-log fold orders it (ConversationFold).
            events = [event for _, event in iter_events_after(conn, 0, cursor)]
            facts = AuthorizationFacts().advance(events)
            return DecisionStateSnapshot(
                event_cursor=cursor,
                projections=fold_projections(events),
                authorizations=read_authorization_snapshot(conn, facts),
            )
        return DecisionStateSnapshot(
            event_cursor=cursor,
            projections=state.projections.projections(),
            authorizations=read_authorization_snapshot(conn, state.authorization),
            fold_state=state if owned else None,
        )
    finally:
        if owned:
            # No writes belong to this reader; rollback only ends its read view.
            conn.rollback()
