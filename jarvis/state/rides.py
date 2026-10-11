"""The bus ride Allen is on, folded from the event log (ADR 0224).

There is no rides table: a ride is the ``ride.started`` event, changed by ``ride.alight_set`` and
``ride.warned`` and closed by ``ride.ended``. At most one ride is active; starting another closes
the older (``replaced``). The boarding watch (``ride.watched``) and the questions the brain asked
him (``ride.candidate_offered`` and its answer) are events too, so a restart forgets nothing and a
trip is asked about once. Every writer passes the time it means, so a clock that is not the wall
clock (a test's) keeps the log consistent. Nothing here calls the network.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from jarvis.state.event_log import emit_event, iter_events_of_types

if TYPE_CHECKING:
    import sqlite3

WATCHED: Final = "ride.watched"
OFFERED: Final = "ride.candidate_offered"
ANSWERED: Final = "ride.candidate_answered"
STARTED: Final = "ride.started"
ALIGHT_SET: Final = "ride.alight_set"
WARNED: Final = "ride.warned"
ENDED: Final = "ride.ended"
TYPES: Final = (WATCHED, OFFERED, ANSWERED, STARTED, ALIGHT_SET, WARNED, ENDED)

SOURCES: Final = ("tapped", "said", "pin")
"""How a ride began: he tapped the boarding question, said so, or said so and the bus was found
from his pinned trip."""
END_REASONS: Final = ("left_vehicle", "passed_stop", "said", "schedule_end", "replaced")

_MINUTE_MS: Final = 60_000
LOOKBACK_MS: Final = 24 * 60 * _MINUTE_MS
"""A ride never runs longer than this, so older events cannot belong to an active one."""
ASKED_KEEP_MS: Final = 3 * 60 * _MINUTE_MS
"""A trip asked about is not asked about again within this long (a trip id repeats daily)."""


@dataclass
class Ride:
    """One ride: where it is, how it began and, once over, how it ended."""

    ride_id: str
    route: str
    source: str
    started_at_ms: int
    trip_id: str | None = None
    vehicle_id: str | None = None
    board_stop: str | None = None
    board_stop_id: str | None = None
    alight_stop: str | None = None
    alight_stop_id: str | None = None
    alight_seq: int | None = None
    destination: str | None = None
    pin_tid: str | None = None
    service_date: str | None = None
    scheduled_end_ms: int | None = None
    device: str | None = None
    warned: bool = False
    ended_at_ms: int | None = None
    end_reason: str | None = None

    @property
    def active(self) -> bool:
        """Whether he is still on it."""
        return self.ended_at_ms is None


_START_FIELDS: Final = frozenset(Ride.__dataclass_fields__) - {
    "started_at_ms", "warned", "ended_at_ms", "end_reason",
}
"""What ``ride.started`` carries; the time is the event's own."""


@dataclass(frozen=True)
class Candidate:
    """A bus the brain asked him about: the trip, and where his phone first moved with it."""

    candidate_id: str
    trip_id: str
    vehicle_id: str
    route: str
    offered_at_ms: int
    first_lat: float | None
    first_lng: float | None
    device: str | None
    answer: str | None = None


@dataclass(frozen=True)
class Watch:
    """A boarding watch he asked for in words: any route, until ``until_ms``."""

    watch_id: str
    destination: str
    until_ms: int
    since_ms: int


def new_id(prefix: str) -> str:
    """A fresh id such as ``ride-3fa91c02``."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _emit(conn: sqlite3.Connection, kind: str, payload: dict[str, Any], at_ms: int) -> None:
    emit_event(conn, type=kind, payload=payload, ts_epoch_ms=at_ms)


def fold(conn: sqlite3.Connection, since_ms: int) -> list[Ride]:
    """The rides started at or after ``since_ms`` (and the events after them), in start order."""
    found: dict[str, Ride] = {}
    for event in iter_events_of_types(conn, (STARTED, ALIGHT_SET, WARNED, ENDED),
                                      since_epoch_ms=since_ms):
        p = event.payload
        if event.type == STARTED:
            found[p["ride_id"]] = Ride(
                started_at_ms=event.ts_epoch_ms,
                **{k: v for k, v in p.items() if k in _START_FIELDS},
            )
            continue
        one = found.get(p["ride_id"])
        if one is None:
            continue
        if event.type == ALIGHT_SET:
            one.alight_stop = p["alight_stop"]
            one.alight_stop_id = p.get("alight_stop_id")
            one.alight_seq = p.get("alight_seq")
        elif event.type == WARNED:
            one.warned = True
        else:
            one.ended_at_ms, one.end_reason = event.ts_epoch_ms, str(p["reason"])
    return list(found.values())


def active(conn: sqlite3.Connection, now_ms: int) -> Ride | None:
    """The ride he is on, or None."""
    mine = [r for r in fold(conn, now_ms - LOOKBACK_MS) if r.active]
    return mine[-1] if mine else None


def candidates(conn: sqlite3.Connection, since_ms: int) -> list[Candidate]:
    """The questions asked since ``since_ms``, each with his answer when there is one."""
    found: dict[str, Candidate] = {}
    for event in iter_events_of_types(conn, (OFFERED, ANSWERED), since_epoch_ms=since_ms):
        p = event.payload
        if event.type == OFFERED:
            found[p["candidate_id"]] = Candidate(
                p["candidate_id"], p["trip_id"], p["vehicle_id"], p["route"], event.ts_epoch_ms,
                p.get("first_lat"), p.get("first_lng"), p.get("device"),
            )
        elif (one := found.get(p["candidate_id"])) is not None:
            found[p["candidate_id"]] = Candidate(
                one.candidate_id, one.trip_id, one.vehicle_id, one.route, one.offered_at_ms,
                one.first_lat, one.first_lng, one.device, str(p["answer"]),
            )
    return list(found.values())


def watch(conn: sqlite3.Connection, now_ms: int) -> Watch | None:
    """The newest watch he asked for that has not run out and no ride has begun since."""
    found: Watch | None = None
    since = now_ms - LOOKBACK_MS
    for event in iter_events_of_types(conn, (WATCHED, STARTED), since_epoch_ms=since):
        if event.type == STARTED:
            found = None
        else:
            p = event.payload
            found = Watch(p["watch_id"], p["destination"], int(p["until_ms"]), event.ts_epoch_ms)
    return found if found is not None and found.until_ms > now_ms else None


def write_watch(conn: sqlite3.Connection, *, destination: str, until_ms: int, at_ms: int) -> str:
    """Append ``ride.watched``: look for him boarding until ``until_ms``. Returns the watch id."""
    watch_id = new_id("watch")
    _emit(conn, WATCHED, {
        "watch_id": watch_id, "destination": destination, "until_ms": until_ms,
    }, at_ms)
    return watch_id


def write_offer(  # noqa: PLR0913 - one keyword per payload field.
    conn: sqlite3.Connection, *, trip_id: str, vehicle_id: str, route: str,
    first: tuple[float, float] | None, device: str | None, at_ms: int,
) -> str:
    """Append ``ride.candidate_offered`` before the question is pushed. Returns its id."""
    candidate_id = new_id("cand")
    _emit(conn, OFFERED, {
        "candidate_id": candidate_id, "trip_id": trip_id, "vehicle_id": vehicle_id,
        "route": route, "first_lat": None if first is None else first[0],
        "first_lng": None if first is None else first[1], "device": device,
    }, at_ms)
    return candidate_id


def write_answer(conn: sqlite3.Connection, candidate_id: str, answer: str, at_ms: int) -> None:
    """Append ``ride.candidate_answered``: ``yes`` (he boarded) or ``no``."""
    _emit(conn, ANSWERED, {"candidate_id": candidate_id, "answer": answer}, at_ms)


def write_start(conn: sqlite3.Connection, ride: Ride) -> None:
    """Append ``ride.started`` for ``ride`` (its ``started_at_ms`` is the event's time).

    Any ride still active is closed first with ``replaced``.
    """
    for older in fold(conn, ride.started_at_ms - LOOKBACK_MS):
        if older.active:
            write_end(conn, older.ride_id, "replaced", ride.started_at_ms)
    _emit(conn, STARTED, {k: getattr(ride, k) for k in _START_FIELDS}, ride.started_at_ms)


def write_alight(  # noqa: PLR0913 - one keyword per payload field.
    conn: sqlite3.Connection, ride_id: str, *, stop: str, stop_id: str | None, seq: int | None,
    at_ms: int,
) -> None:
    """Append ``ride.alight_set``: the stop he gets off at."""
    _emit(conn, ALIGHT_SET, {
        "ride_id": ride_id, "alight_stop": stop, "alight_stop_id": stop_id, "alight_seq": seq,
    }, at_ms)


def write_warned(conn: sqlite3.Connection, ride_id: str, at_ms: int) -> None:
    """Append ``ride.warned``: the one-stop warning went out, so it never goes twice."""
    _emit(conn, WARNED, {"ride_id": ride_id}, at_ms)


def write_end(conn: sqlite3.Connection, ride_id: str, reason: str, at_ms: int) -> None:
    """Append ``ride.ended`` with one of :data:`END_REASONS`."""
    _emit(conn, ENDED, {"ride_id": ride_id, "reason": reason}, at_ms)
