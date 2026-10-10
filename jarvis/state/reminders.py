"""Owner-set reminders, folded from the event log (ADR 0179).

There is no reminders table: a reminder is a ``reminder.scheduled`` event, and what became of
it is whichever of ``reminder.cancelled``, ``reminder.fired`` and ``reminder.acknowledged``
followed. ``set_reminder`` (L4) and the daemon's tick (runtime) read the same fold.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from jarvis.state.event_log import emit_event, iter_events_of_types

if TYPE_CHECKING:
    import sqlite3
    from datetime import datetime

TYPES: Final[tuple[str, ...]] = (
    "reminder.scheduled",
    "reminder.cancelled",
    "reminder.fired",
    "reminder.acknowledged",
)
ID_PREFIX: Final[str] = "reminder-"


@dataclass
class Reminder:
    """One reminder and what has happened to it so far."""

    reminder_id: str
    due_at_ms: int
    due_at_local: str
    text: str
    cancelled: bool = False
    fired_at_ms: int | None = None
    late_ms: int = 0
    delivered: str | None = None
    acknowledged: bool = False

    @property
    def pending(self) -> bool:
        """Scheduled, not cancelled, not yet fired."""
        return not self.cancelled and self.fired_at_ms is None


def fold(conn: sqlite3.Connection, *, until_ms: int | None = None) -> dict[str, Reminder]:
    """Every reminder ever scheduled, by id, in the order it was scheduled.

    ``until_ms`` folds only the events up to that instant: the reminders as they stood then.
    """
    found: dict[str, Reminder] = {}
    for event in iter_events_of_types(conn, TYPES):
        if until_ms is not None and event.ts_epoch_ms > until_ms:
            continue
        payload = event.payload
        rid = str(payload["reminder_id"])
        if event.type == "reminder.scheduled":
            found[rid] = Reminder(
                rid,
                int(payload["due_at_epoch_ms"]),
                str(payload["due_at_local"]),
                str(payload["text"]),
            )
        elif (one := found.get(rid)) is None:
            continue
        elif event.type == "reminder.cancelled":
            one.cancelled = True
        elif event.type == "reminder.fired":
            one.fired_at_ms, one.late_ms = int(payload["fired_at"]), int(payload["late_ms"])
            one.delivered = str(payload["delivered"])
        else:
            one.acknowledged = True
    return found


def pending(conn: sqlite3.Connection) -> list[Reminder]:
    """The reminders still to fire, soonest first."""
    return sorted((r for r in fold(conn).values() if r.pending), key=lambda r: r.due_at_ms)


def schedule(
    conn: sqlite3.Connection,
    *,
    due: datetime,
    text: str,
    action_id: str,
    source_event_id: str | None = None,
) -> str:
    """Append one ``reminder.scheduled`` event for ``due`` (aware) and return the new id."""
    reminder_id = ID_PREFIX + uuid.uuid4().hex[:8]
    emit_event(
        conn,
        type="reminder.scheduled",
        payload={
            "reminder_id": reminder_id,
            "due_at_epoch_ms": int(due.timestamp() * 1000),
            "due_at_local": due.isoformat(timespec="seconds"),
            "text": text,
            "action_id": action_id,
        },
        source_event_id=source_event_id,
        correlation={"action_id": action_id},
    )
    return reminder_id


def cancel(
    conn: sqlite3.Connection,
    reminder_id: str,
    *,
    action_id: str,
    source_event_id: str | None = None,
) -> None:
    """Append ``reminder.cancelled`` for a reminder the caller knows is still pending."""
    emit_event(
        conn,
        type="reminder.cancelled",
        payload={"reminder_id": reminder_id, "action_id": action_id},
        source_event_id=source_event_id,
        correlation={"action_id": action_id},
    )
