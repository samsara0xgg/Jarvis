"""The bus trips Allen pinned on the notch, folded from the event log (ADR 0200, 0204).

There is no pins table: the pin is the newest ``departure.pinned`` event, changed by later
``departure.trip_added``, ``departure.trip_removed`` and ``departure.updated`` events (a trip's
live times) and ended by ``departure.unpinned``. A pin holds up to ``MAX_TRIPS`` trips, so two lines
a few minutes apart can both stay; a pin written before that holds one trip in its own fields and
is folded as a set of one. The tool (``pin_departure``), the cards, the daemon's refresh and the
notice routes read the same fold.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Final

from jarvis.state.event_log import emit_event, iter_events_of_types

if TYPE_CHECKING:
    import sqlite3

TYPES: Final[tuple[str, ...]] = (
    "departure.pinned",
    "departure.trip_added",
    "departure.trip_removed",
    "departure.updated",
    "departure.unpinned",
)
ID_PREFIX: Final[str] = "departure-"
MAX_TRIPS: Final[int] = 3


@dataclass
class Trip:
    """One bus on a pin: when to leave, the first bus, and where it boards."""

    tid: str
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


def new_tid() -> str:
    """A fresh id for a trip that does not come from an offered row."""
    return uuid.uuid4().hex[:6]


@dataclass
class Departure:
    """The pin: its trips, the ring (one reminder, for the trip ``ring_tid``) and its offer."""

    pin_id: str
    reminder_id: str
    ring_tid: str
    trips: list[Trip] = field(default_factory=list)
    offer_id: str | None = None

    def catchable(self, now_ms: int) -> list[Trip]:
        """The trips whose bus has not left (leave time plus the walk), earliest leave first."""
        return sorted(
            (t for t in self.trips if now_ms < t.departs_at_ms),
            key=lambda t: (t.leave_at_ms, t.departs_at_ms),
        )

    def trip(self, tid: str) -> Trip | None:
        """The trip with that id, or None."""
        return next((t for t in self.trips if t.tid == tid), None)


def _trip(raw: dict[str, Any], tid: str) -> Trip:
    lat, lng = raw["stop_lat"], raw["stop_lng"]
    return Trip(
        str(raw.get("tid", tid)),
        str(raw["route"]),
        str(raw["board_stop"]),
        int(raw["leave_at_ms"]),
        int(raw["departs_at_ms"]),
        str(raw["arrive_at"]),
        None if lat is None else float(lat),
        None if lng is None else float(lng),
        str(raw["to"]),
    )


def current(conn: sqlite3.Connection) -> Departure | None:
    """The pin as the log leaves it (not yet checked against the clock), or None."""
    found: Departure | None = None
    for event in iter_events_of_types(conn, TYPES):
        payload = event.payload
        if event.type == "departure.pinned":
            # A pin from before ADR 0204 has its one trip's fields at the top.
            trips = [_trip(raw, "t0") for raw in payload.get("trips") or [payload]]
            offer = payload.get("offer_id")
            found = Departure(
                str(payload["pin_id"]),
                str(payload["reminder_id"]),
                str(payload.get("ring", trips[0].tid)),
                trips,
                None if offer is None else str(offer),
            )
            continue
        if found is None or found.pin_id != payload["pin_id"]:
            continue
        if event.type == "departure.unpinned":
            found = None
            continue
        found.reminder_id = str(payload["reminder_id"])
        found.ring_tid = str(payload.get("ring", found.ring_tid))
        if event.type == "departure.trip_added":
            found.trips.append(_trip(payload["trip"], new_tid()))
        elif event.type == "departure.trip_removed":
            found.trips = [t for t in found.trips if t.tid != payload["tid"]]
            if not found.trips:
                found = None
        elif one := found.trip(str(payload.get("tid", found.trips[0].tid))):
            one.leave_at_ms = int(payload["leave_at_ms"])
            one.departs_at_ms = int(payload["departs_at_ms"])
            one.arrive_at = str(payload["arrive_at"])
    return found


def active(conn: sqlite3.Connection, now_ms: int) -> Departure | None:
    """The pin while any of its buses has not left; once the last has gone it is gone."""
    one = current(conn)
    return one if one is not None and one.catchable(now_ms) else None


def pin(  # noqa: PLR0913 — one keyword per payload field.
    conn: sqlite3.Connection,
    *,
    reminder_id: str,
    ring: str,
    trips: list[Trip],
    offer_id: str | None,
    action_id: str,
    source_event_id: str | None = None,
) -> str:
    """Append ``departure.pinned``: a new set that replaces the older. Returns the new pin id."""
    pin_id = ID_PREFIX + uuid.uuid4().hex[:8]
    emit_event(
        conn,
        type="departure.pinned",
        payload={
            "pin_id": pin_id,
            "reminder_id": reminder_id,
            "ring": ring,
            "trips": [asdict(t) for t in trips],
            "offer_id": offer_id,
            "action_id": action_id,
        },
        source_event_id=source_event_id,
        correlation={"action_id": action_id},
    )
    return pin_id


def add(conn: sqlite3.Connection, pin_id: str, trip: Trip, *, reminder_id: str, ring: str) -> None:
    """Append ``departure.trip_added``: one more trip on the set, and the ring now."""
    emit_event(
        conn,
        type="departure.trip_added",
        payload={"pin_id": pin_id, "trip": asdict(trip), "reminder_id": reminder_id, "ring": ring},
    )


def remove(conn: sqlite3.Connection, pin_id: str, tid: str, *, reminder_id: str, ring: str) -> None:
    """Append ``departure.trip_removed``: one trip off the set, and the ring now."""
    emit_event(
        conn,
        type="departure.trip_removed",
        payload={"pin_id": pin_id, "tid": tid, "reminder_id": reminder_id, "ring": ring},
    )


def update(
    conn: sqlite3.Connection, pin_id: str, trip: Trip, *, reminder_id: str, ring: str,
) -> None:
    """Append ``departure.updated``: this trip's live times, and the ring now."""
    emit_event(
        conn,
        type="departure.updated",
        payload={
            "pin_id": pin_id,
            "tid": trip.tid,
            "reminder_id": reminder_id,
            "ring": ring,
            "leave_at_ms": trip.leave_at_ms,
            "departs_at_ms": trip.departs_at_ms,
            "arrive_at": trip.arrive_at,
        },
    )


def unpin(conn: sqlite3.Connection, pin_id: str) -> None:
    """Append ``departure.unpinned``: the whole set."""
    emit_event(conn, type="departure.unpinned", payload={"pin_id": pin_id})
