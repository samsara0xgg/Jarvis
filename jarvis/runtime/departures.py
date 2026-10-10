"""The pinned bus trips: what the notch is served and the live refresh behind them (ADR 0200, 0204).

The state is the event log (``jarvis.state.departures``): a set of up to three trips. While a bus
has not left, the reminder tick calls :meth:`Departures.refresh`, which every ``REFRESH_S`` asks BC
Transit's live feed (``jarvis.runtime.bus_live``) when each trip's bus really leaves. If one moved,
``departure.updated`` is written for it. Every tick also checks the one reminder still rings for the
earliest catchable trip, and moves it on when that trip is missed. A bus the feed does not show, or
a feed that does not answer, leaves its times as they were and marks that trip ``stale``; a failed
refresh never drops the pin.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final
from zoneinfo import ZoneInfo

from jarvis.execution.tools import ToolError
from jarvis.execution.transit_tool import (
    TransitOffers,
    add_trip,
    pin_offered,
    pin_trips,
    ring,
)
from jarvis.state import departures as folded
from jarvis.state import reminders
from jarvis.state.event_log import open_runtime_event_log

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable
    from pathlib import Path

    from jarvis.runtime.bus_live import BusLive

LOGGER = logging.getLogger(__name__)

# The feed itself moves about every 30 s. The reminder tick is 15 s, so this is every second tick;
# the margin keeps a tick that comes a little early from skipping a whole round.
REFRESH_S: Final[float] = 30.0
_EARLY_S: Final[float] = 5.0
_ZONE: Final = ZoneInfo("America/Vancouver")
_MS_PER_MIN: Final[int] = 60_000
_GRACE_MS: Final[int] = 60_000  # the tool's grace for a leave time just past


def _clock(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, _ZONE).strftime("%H:%M")


def _shifted(trip: folded.Trip, departs_ms: int, shift_ms: int) -> str:
    """The trip's ``arrive_at`` moved by ``shift_ms``, on the day of the new departure."""
    arrive = datetime.combine(
        datetime.fromtimestamp(departs_ms / 1000, _ZONE).date(),
        datetime.strptime(trip.arrive_at, "%H:%M").time(),  # noqa: DTZ007 - a clock only.
        tzinfo=_ZONE,
    ) + timedelta(milliseconds=shift_ms)
    return f"{arrive:%H:%M}"


@dataclass
class _Live:
    """What the refresh knows of one trip of the served pin."""

    trip_id: str | None = None  # the BC Transit trip matched last time
    stale: bool = False
    delay_min: int = 0
    checked_ms: int | None = None


@dataclass
class _Undo:
    """What the last removal took: the whole set, or one trip of it."""

    pin_id: str
    trips: list[folded.Trip]
    whole: bool = False
    offer_id: str | None = None


class Departures:
    """The pin's served view, its edits and its live refresh; built once at boot."""

    def __init__(
        self, event_log: Path, live: BusLive | None = None, offers: TransitOffers | None = None,
    ) -> None:
        """``live`` is BC Transit's feed; without it the pin keeps Google's times.

        ``offers`` is what ``transit`` left for the notch's card (ADR 0203).
        """
        self._path = event_log
        self.live = live
        self.offers = offers if offers is not None else TransitOffers()
        self._undo: _Undo | None = None
        self.now: Callable[[], datetime] = lambda: datetime.now(UTC)
        self._pin: str | None = None
        self._last = 0.0
        self._live: dict[str, _Live] = {}

    def _now_ms(self) -> int:
        return int(self.now().timestamp() * 1000)

    # --- what the surface reads -----------------------------------------------------

    def view(self) -> dict[str, Any] | None:
        """The pin while a bus has not left, as ``GET /inherent/notices`` serves it.

        The top-level fields are the current trip (the earliest still catchable); ``trips`` are
        all the catchable ones with their own live status, ``more`` the others.
        """
        with contextlib.closing(open_runtime_event_log(self._path)) as conn:
            one = folded.active(conn, self._now_ms())
        if one is None:
            return None
        trips = [self._trip_view(one, t) for t in one.catchable(self._now_ms())]
        return {**trips[0], "id": one.pin_id, "more": len(trips) - 1, "trips": trips}

    def _trip_view(self, one: folded.Departure, trip: folded.Trip) -> dict[str, Any]:
        mine = self._live.get(trip.tid) if one.pin_id == self._pin else None
        known = mine or _Live()
        return {
            "id": trip.tid,
            "route": trip.route,
            "board_stop": trip.board_stop,
            "leave_at_ms": trip.leave_at_ms,
            "departs_at_ms": trip.departs_at_ms,
            "leave_at": _clock(trip.leave_at_ms),
            "departs": _clock(trip.departs_at_ms),
            "arrive_at": trip.arrive_at,
            "to": trip.to,
            "stale": known.stale,
            "delay_min": known.delay_min,
            "checked_at_ms": known.checked_ms,
        }

    def offer(self) -> dict[str, Any] | None:
        """The latest ``transit`` answer's options while the card may show them."""
        return self.offers.view()

    def unpin(self, pin_id: str) -> None:
        """``POST /inherent/notices/{id}``: take the whole set off and cancel its reminder."""
        with contextlib.closing(open_runtime_event_log(self._path)) as conn:
            one = folded.current(conn)
            if one is None or one.pin_id != pin_id:
                return
            self._unpin(conn, one)

    def _unpin(self, conn: sqlite3.Connection, one: folded.Departure) -> None:
        self._undo = _Undo(one.pin_id, one.trips, whole=True, offer_id=one.offer_id)
        ringing = reminders.fold(conn).get(one.reminder_id)
        if ringing is not None and ringing.pending:
            reminders.cancel(conn, one.reminder_id, action_id=one.pin_id)
        folded.unpin(conn, one.pin_id)

    # --- what the notch's cards ask for (ADR 0203, 0204) --------------------------------

    def act(self, body: dict[str, Any]) -> dict[str, Any]:
        """``POST /inherent/departure``: one of the cards' actions."""
        match body["action"]:
            case "pin" | "add":
                return self.pin_offer(body["offer_id"], body["index"], add=body["action"] == "add")
            case "remove":
                return self.remove(body["trip_id"])
            case "next":
                return self.next_bus()
            case _:
                return self.undo(body["pin_id"])

    def pin_offer(self, offer_id: str, index: int, *, add: bool = False) -> dict[str, Any]:
        """Pin one option of the offer, as the tool would; LookupError when it cannot be.

        ``add`` puts it on the set this offer already started (a new set when it did not).
        """
        with contextlib.closing(open_runtime_event_log(self._path)) as conn:
            made = pin_offered(conn, self.offers, offer_id, index, add=add)
        return {"departure": self.view(), "reason": made.get("reason")}

    def remove(self, trip_id: str) -> dict[str, Any]:
        """Take one trip off the set (the last, the pin); LookupError if it is not pinned."""
        with contextlib.closing(open_runtime_event_log(self._path)) as conn:
            now_ms = self._now_ms()
            one = folded.active(conn, now_ms)
            trip = None if one is None else one.trip(trip_id)
            if one is None or trip is None:
                msg = "that trip is not pinned"
                raise LookupError(msg)
            rest = [t for t in one.catchable(now_ms) if t.tid != trip_id]
            if not rest:
                self._unpin(conn, one)
            else:
                self._undo = _Undo(one.pin_id, [trip], whole=False, offer_id=one.offer_id)
                reminder_id, ring_tid = ring(
                    conn, [t for t in one.trips if t.tid != trip_id], now_ms=now_ms,
                    reminder_id=one.reminder_id, ring_tid=one.ring_tid, action_id=one.pin_id,
                )
                folded.remove(conn, one.pin_id, trip_id, reminder_id=reminder_id, ring=ring_tid)
        return {"departure": self.view(), "reason": None}

    def next_bus(self) -> dict[str, Any]:
        """Move the current trip to the next later bus of its route at its stop, or say none."""
        with contextlib.closing(open_runtime_event_log(self._path)) as conn:
            now_ms = self._now_ms()
            one = folded.active(conn, now_ms)
            if one is None:
                msg = "nothing is pinned"
                raise LookupError(msg)
            trip = one.catchable(now_ms)[0]
            found = self._later(one, trip)
            reason: str | None = "no_later"
            if found is not None:
                trip.leave_at_ms, trip.departs_at_ms, trip.arrive_at = found
                self._live[trip.tid] = _Live()  # the bus matched before is not this one
                self._record(conn, one, trip, now_ms)
                reason = None
        return {"departure": self.view(), "reason": reason}

    def undo(self, pin_id: str) -> dict[str, Any]:
        """Bring back what the last removal took, unless its time to leave has passed."""
        gone = self._undo
        if gone is None or gone.pin_id != pin_id:
            msg = "nothing to bring back"
            raise LookupError(msg)
        with contextlib.closing(open_runtime_event_log(self._path)) as conn:
            now_ms = self._now_ms()
            back = [t for t in gone.trips if t.leave_at_ms >= now_ms - _GRACE_MS]
            one = folded.active(conn, now_ms)
            reason: str | None = "past"
            try:
                if back and (gone.whole or one is None):
                    pin_trips(conn, back, offer_id=gone.offer_id, action_id=pin_id)
                    reason = None
                elif back and one is not None:
                    for trip in back:
                        add_trip(conn, one, trip, action_id=pin_id)
                        one = folded.current(conn)
                        assert one is not None  # noqa: S101 - just added to it.
                    reason = None
            except ToolError as exc:
                reason = "full" if exc.code == "full" else "past"
        if reason is None:
            self._undo = None
        return {"departure": self.view(), "reason": reason}

    def _later(
        self, one: folded.Departure, trip: folded.Trip,
    ) -> tuple[int, int, str] | None:
        """``(leave ms, departs ms, arrive_at)`` of the next bus: last answer or feed.

        Past the pinned buses of that route and stop, so it never doubles one already there.
        """
        line = max(
            t.departs_at_ms
            for t in one.trips
            if (t.first_route, t.board_stop) == (trip.first_route, trip.board_stop)
        )
        after = datetime.fromtimestamp(line / 1000, _ZONE)
        if later := self.offers.later(trip.first_route, trip.board_stop, after):
            return int(later[0].timestamp() * 1000), int(later[1].timestamp() * 1000), later[2]
        if self.live is None or trip.stop_lat is None or trip.stop_lng is None:
            return None
        mine = self._live.get(trip.tid) if one.pin_id == self._pin else None
        try:
            found = self.live.next_departure(
                trip.first_route, (trip.stop_lat, trip.stop_lng), line,
                None if mine is None else mine.trip_id,
            )
        except Exception:  # noqa: BLE001 - network, zip or feed trouble all mean no answer.
            LOGGER.warning("departures: BC Transit live data unavailable", exc_info=True)
            return None
        if found is None:
            return None
        departs = round(found[1] / _MS_PER_MIN) * _MS_PER_MIN
        shift = departs - trip.departs_at_ms
        return trip.leave_at_ms + shift, departs, _shifted(trip, departs, shift)

    def _record(
        self, conn: sqlite3.Connection, one: folded.Departure, trip: folded.Trip, now_ms: int,
    ) -> None:
        """Write ``trip``'s new times, the ring moved to match them, as one ``updated``."""
        reminder_id, ring_tid = ring(
            conn, one.trips, now_ms=now_ms, reminder_id=one.reminder_id, ring_tid=one.ring_tid,
            action_id=one.pin_id,
        )
        folded.update(conn, one.pin_id, trip, reminder_id=reminder_id, ring=ring_tid)

    # --- the live refresh -----------------------------------------------------------

    def refresh(self) -> None:
        """One refresh if the pin is due for one; a failure is logged and never raised."""
        try:
            self._refresh()
        except Exception:
            LOGGER.exception("departures: refresh failed; the pin keeps its times")
            for mine in self._live.values():
                mine.stale = True

    def _refresh(self) -> None:
        now = self.now()
        now_ms = int(now.timestamp() * 1000)
        with contextlib.closing(open_runtime_event_log(self._path)) as conn:
            one = folded.active(conn, now_ms)
            if one is None:
                self._pin = None
                return
            moved: list[folded.Trip] = []
            if one.pin_id != self._pin:
                # A new pin starts with Google's times, fresh from the trip lookup.
                self._pin, self._last, self._live = one.pin_id, now.timestamp(), {}
            elif now.timestamp() - self._last >= REFRESH_S - _EARLY_S:
                self._last = now.timestamp()
                moved = self._poll(one, now_ms)
            reminder_id, ring_tid = ring(
                conn, one.trips, now_ms=now_ms, reminder_id=one.reminder_id,
                ring_tid=one.ring_tid, action_id=one.pin_id,
            )
            # The ring moves with a trip's new time, or alone when the earliest trip was missed.
            ringing = one.trip(ring_tid)
            changed = (reminder_id, ring_tid) != (one.reminder_id, one.ring_tid)
            if not moved and changed and ringing:
                moved = [ringing]
            for trip in moved:
                folded.update(conn, one.pin_id, trip, reminder_id=reminder_id, ring=ring_tid)

    def _poll(self, one: folded.Departure, now_ms: int) -> list[folded.Trip]:
        """Ask the feed about every catchable trip; the ones it moved, already shifted in place."""
        moved = []
        # ponytail: one feed download per trip (three at most); share a parsed feed if it matters.
        for trip in one.catchable(now_ms):
            mine = self._live.setdefault(trip.tid, _Live())
            found = None
            if self.live is not None and trip.stop_lat is not None and trip.stop_lng is not None:
                try:
                    found = self.live.departure(
                        trip.first_route,
                        (trip.stop_lat, trip.stop_lng),
                        trip.departs_at_ms,
                        mine.trip_id,
                    )
                except Exception:  # noqa: BLE001 - network, zip or feed trouble all mean stale.
                    LOGGER.warning("departures: BC Transit live data unavailable", exc_info=True)
            mine.stale = found is None
            if found is None:
                continue
            mine.trip_id, mine.checked_ms = found[0], now_ms
            # The minute it will leave; seconds would move the pin on every wobble of the feed.
            departs = round(found[1] / _MS_PER_MIN) * _MS_PER_MIN
            shift = departs - trip.departs_at_ms
            if shift:
                mine.delay_min += shift // _MS_PER_MIN
                trip.arrive_at = _shifted(trip, departs, shift)
                trip.leave_at_ms += shift
                trip.departs_at_ms = departs
                moved.append(trip)
                LOGGER.info(
                    "departure %s trip %s moved by %d min", one.pin_id, trip.tid,
                    shift // _MS_PER_MIN,
                )
        return moved
