"""The bus trip Allen pinned on the notch, folded from the event log (ADR 0200).

There is no pins table: the pin is the newest ``departure.pinned`` event, moved by later
``departure.updated`` events (the live refresh) and ended by ``departure.unpinned``. The tool
(``pin_departure``), the daemon's refresh and the notice routes read the same fold.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from jarvis.state.event_log import emit_event, iter_events_of_types

if TYPE_CHECKING:
    import sqlite3

TYPES: Final[tuple[str, ...]] = ("departure.pinned", "departure.updated", "departure.unpinned")
ID_PREFIX: Final[str] = "departure-"


@dataclass
class Departure:
    """One pinned trip: when to leave, the first bus, and where it boards."""

    pin_id: str
    reminder_id: str
    route: str
    board_stop: str
    leave_at_ms: int
    departs_at_ms: int
    arrive_at: str
    stop_lat: float | None
    stop_lng: float | None
    to: str

    @property
    def first_route(self) -> str:
        """The first bus's number: ``28`` for the trip ``28 → 12``."""
        return self.route.split("→")[0].strip()


def current(conn: sqlite3.Connection) -> Departure | None:
    """The pin as the log leaves it (not yet checked against the clock), or None."""
    found: Departure | None = None
    for event in iter_events_of_types(conn, TYPES):
        payload = event.payload
        if event.type == "departure.pinned":
            lat, lng = payload["stop_lat"], payload["stop_lng"]
            found = Departure(
                str(payload["pin_id"]),
                str(payload["reminder_id"]),
                str(payload["route"]),
                str(payload["board_stop"]),
                int(payload["leave_at_ms"]),
                int(payload["departs_at_ms"]),
                str(payload["arrive_at"]),
                None if lat is None else float(lat),
                None if lng is None else float(lng),
                str(payload["to"]),
            )
        elif found is None or found.pin_id != payload["pin_id"]:
            continue
        elif event.type == "departure.updated":
            found.reminder_id = str(payload["reminder_id"])
            found.leave_at_ms = int(payload["leave_at_ms"])
            found.departs_at_ms = int(payload["departs_at_ms"])
            found.arrive_at = str(payload["arrive_at"])
        else:
            found = None
    return found


def active(conn: sqlite3.Connection, now_ms: int) -> Departure | None:
    """The pin while its bus has not left; once ``departs`` has passed it is gone."""
    one = current(conn)
    return one if one is not None and now_ms < one.departs_at_ms else None


def pin(  # noqa: PLR0913 — one keyword per payload field.
    conn: sqlite3.Connection,
    *,
    reminder_id: str,
    route: str,
    board_stop: str,
    leave_at_ms: int,
    departs_at_ms: int,
    arrive_at: str,
    stop: tuple[float, float] | None,
    to: str,
    action_id: str,
    source_event_id: str | None = None,
) -> str:
    """Append ``departure.pinned``; a newer pin replaces the older. Returns the new pin id."""
    pin_id = ID_PREFIX + uuid.uuid4().hex[:8]
    emit_event(
        conn,
        type="departure.pinned",
        payload={
            "pin_id": pin_id,
            "reminder_id": reminder_id,
            "route": route,
            "board_stop": board_stop,
            "leave_at_ms": leave_at_ms,
            "departs_at_ms": departs_at_ms,
            "arrive_at": arrive_at,
            "stop_lat": None if stop is None else stop[0],
            "stop_lng": None if stop is None else stop[1],
            "to": to,
            "action_id": action_id,
        },
        source_event_id=source_event_id,
        correlation={"action_id": action_id},
    )
    return pin_id


def update(  # noqa: PLR0913 — one keyword per payload field.
    conn: sqlite3.Connection,
    pin_id: str,
    *,
    reminder_id: str,
    leave_at_ms: int,
    departs_at_ms: int,
    arrive_at: str,
) -> None:
    """Append ``departure.updated``: the live times, and the reminder now ringing for them."""
    emit_event(
        conn,
        type="departure.updated",
        payload={
            "pin_id": pin_id,
            "reminder_id": reminder_id,
            "leave_at_ms": leave_at_ms,
            "departs_at_ms": departs_at_ms,
            "arrive_at": arrive_at,
        },
    )


def unpin(conn: sqlite3.Connection, pin_id: str) -> None:
    """Append ``departure.unpinned``."""
    emit_event(conn, type="departure.unpinned", payload={"pin_id": pin_id})
