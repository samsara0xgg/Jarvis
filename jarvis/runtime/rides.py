"""A followed bus ride (ADR 0224): the watch for boarding, the ride, and what is told of it.

The state is the event log (``jarvis.state.rides``); this module is the clock and the feeds. Every
``TICK_S`` the loop looks at the log and does one of three things, and nothing at all when none
applies, so BC Transit's VehiclePositions feed (about 13 KB) is fetched only while there is a
reason:

- **A ride is active**: read the bus's own position, work out the next stop, the stops left and the
  arrival at his stop (:mod:`jarvis.state.ride_progress`), warn one stop before, tell the phone's
  Live Activity, and end the ride by the first of four rules (his fixes twice more than 150 m from
  the bus; the trip passed his stop and his motion is walking or still; he said so; fifteen minutes
  past the trip's scheduled end).
- **A boarding watch is on** (a pinned trip within ten minutes of leaving, or the model's
  ``bus(ride="watch")`` after he said he is heading out): when two consecutive phone fixes are
  within 60 m of one bus and move with it, ask him once, by notification, whether he boarded.
- **Neither**: forget the vehicle history and return.

He starts a ride by tapping the notification's button (:meth:`Rides.act`) or by saying so
(``bus(ride="start")``); it is never inferred. When the feed fails the ride falls back to the
timetable and says the bus has no live position. Everything runs off the event loop; the one
thing read on it is :meth:`line`, from a snapshot the ticks keep.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final
from zoneinfo import ZoneInfo

from jarvis.decision.turn_guards import RIDE_LINE
from jarvis.execution.bus_tool import RideControl, sources_said
from jarvis.execution.tools import ToolError
from jarvis.shared import lang
from jarvis.shared.geo import metres
from jarvis.state import departures as pins
from jarvis.state import rides as folded
from jarvis.state import stop_finder
from jarvis.state.event_log import open_runtime_event_log
from jarvis.state.phone_location import fixes_since, motion_now
from jarvis.state.ride_progress import (
    Fix,
    Progress,
    Sample,
    StopTime,
    Vehicle,
    left_at,
    nearest_stop,
    next_index,
    position_at,
    progress,
    rides_with,
    stop_named,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable
    from pathlib import Path

    from jarvis.execution.transit_tool import TransitOffers
    from jarvis.runtime.bus_live import BusLive
    from jarvis.runtime.push import Push
    from jarvis.shared.here import HereReading

LOGGER = logging.getLogger(__name__)

TICK_S: Final = 15.0
_MIN_MS: Final = 60_000
PIN_LEAD_MS: Final = 10 * _MIN_MS
"""A pinned trip is watched from this long before its bus leaves ..."""
PIN_AFTER_MS: Final = 15 * _MIN_MS
"""... until this long after, since he is only seen moving with the bus once it has left."""
WATCH_MS: Final = 45 * _MIN_MS
"""How long ``bus(ride="watch")`` looks for him."""
FIX_WINDOW_MS: Final = 4 * _MIN_MS
FIX_FRESH_MS: Final = 2 * _MIN_MS
"""The newest of the two fixes that show him moving with a bus is no older than this."""
PAIR_GAP_MS: Final = 2 * _MIN_MS
"""The two fixes are no farther apart than this (the phone sends one every 30 s on the move)."""
SAID_QUIET_MS: Final = 5 * _MIN_MS
"""After he says he boarded, even a ride that did not start gets no notification for this long."""
SAID_RADIUS_M: Final = 300.0
"""The bus nearest his last fix counts when it is no farther; a fix is up to a minute old."""
AMBIGUOUS_GAP_M: Final = 100.0
"""A second bus closer than this behind the nearest one makes the match ambiguous."""
SAME_MOMENT_MS: Final = 30_000
"""His fix and a bus are compared at one moment: their reports are no farther apart than this."""
FIX_LOOKBACK_MS: Final = 5 * _MIN_MS
"""How far back his fixes are searched for the one nearest a bus's report."""
END_GRACE_MS: Final = 15 * _MIN_MS
"""The ride ends this long after the trip's scheduled end."""
FALLBACK_RIDE_MS: Final = 2 * 60 * _MIN_MS
"""A ride whose timetable is unknown ends this long after it began."""
PREDICT_EVERY_MS: Final = _MIN_MS
"""TripUpdates (about 360 KB) are read this often while riding, for the arrival at his stop."""
START_WINDOW_MS: Final = 5 * _MIN_MS
"""A Live Activity is started only this soon after the ride began."""
PASSED_BACK_MS: Final = _MIN_MS
"""Walking or still counts if it began this long before the bus was seen past his stop."""
STOPS_RETRY_MS: Final = 5 * _MIN_MS
HIST_KEEP: Final = 120
"""Positions kept per vehicle: thirty minutes at one a tick."""
PIN_WINDOW_MS: Final = 30 * _MIN_MS
"""A pinned trip whose bus leaves within this of now is the one a said ride matches."""
PRIVATE_QUIET: Final = frozenset({"no-pop", "dnd"})
"""Quiet levels at which the boarding question is held; the one-stop warning is not."""

CREDIT: Final = "BC Transit"
"""Credited wherever ride data from BC Transit's feeds is shown (its licence asks for it)."""
RIDE_LINE_PREFIX: Final = RIDE_LINE
RIDE_LINE_CHARS: Final = 520

_ZONE: Final = ZoneInfo("America/Vancouver")
_STILL: Final = frozenset({"walking", "running", "stationary"})


def _clock(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, _ZONE).strftime("%H:%M")


type Place = Callable[[], tuple[float, float] | None]
"""Where he said he is going, read when it is needed (by the tool's place reader, ADR 0346)."""


@dataclass(frozen=True)
class _Watching:
    """Why the boarding watch is on: which routes it looks at (None: any) and where he is going."""

    routes: frozenset[str] | None
    destination: str | None


@dataclass
class _Activity:
    """What the ride's Live Activity was last told."""

    ride_id: str
    started: bool = False
    wire: str = ""


class Rides:
    """The ride follower; built once at boot, driven by :meth:`run`, safe from any thread."""

    def __init__(
        self,
        event_log: Path,
        live: BusLive | None = None,
        *,
        offers: TransitOffers | None = None,
    ) -> None:
        """``live`` is BC Transit's feeds (without it a ride is only what he declares).

        ``offers`` is what the last ``bus`` plan said about where his bus lets him off; it only
        helps find his stop.
        """
        self._path = event_log
        self.live = live
        self._offers = offers
        self.now: Callable[[], datetime] = lambda: datetime.now(UTC)
        # Wired by the daemon (ADR 0210); until then nothing is pushed.
        self.push: Push | None = None
        self._lock = threading.RLock()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rides")
        # Runs a tick soon after a ride starts, so the Live Activity does not wait for the loop.
        self.defer: Callable[[Callable[[], None]], object] = self._pool.submit
        self._hist: dict[str, deque[Sample]] = {}
        self._feed_down = False
        self._said_ms = 0
        self._preds: dict[str, int] = {}
        self._pred_ms = 0
        self._stops: dict[tuple[str, str | None], list[StopTime]] = {}
        self._stops_failed_ms = 0
        self._passed_ms: int | None = None
        self._activity: _Activity | None = None
        self._asked_stop: set[str] = set()
        self._seen: dict[str, Vehicle] = {}
        # What :meth:`line` reads on the event loop: the ride and where it is, replaced whole.
        self._snap: tuple[folded.Ride, Progress | None] | None = None

    def _now_ms(self) -> int:
        return int(self.now().timestamp() * 1000)

    def _open(self) -> sqlite3.Connection:
        return open_runtime_event_log(self._path)

    # --- the loop ---------------------------------------------------------------------

    async def run(self) -> None:
        """Tick every ``TICK_S``; a failed tick is logged, never ends the loop."""
        LOGGER.info("rides started (every %.0f s)", TICK_S)
        try:
            while True:
                await asyncio.to_thread(self.tick)
                await asyncio.sleep(TICK_S)
        except asyncio.CancelledError:
            self._pool.shutdown(wait=False)
            LOGGER.info("rides cancelled")
            raise

    def tick(self) -> None:
        """One look at the log and, if there is a ride or a watch, at the feed."""
        try:
            with self._lock, contextlib.closing(self._open()) as conn:
                now = self._now_ms()
                ride = folded.active(conn, now)
                if ride is not None:
                    self._follow(conn, ride, now)
                    return
                self._snap = None
                self._activity = None
                watching = self._watching(conn, now)
                if watching is None:
                    self._idle()
                else:
                    self._look(conn, watching, now)
        except Exception:
            LOGGER.exception("rides: tick failed; trying again next time")

    def _idle(self) -> None:
        self._hist.clear()
        self._seen.clear()
        self._preds, self._pred_ms, self._passed_ms = {}, 0, None

    # --- watching for him to board -----------------------------------------------------

    def _watching(self, conn: sqlite3.Connection, now: int) -> _Watching | None:
        said = folded.watch(conn, now)
        if said is not None:
            return _Watching(None, said.destination)
        pin = pins.current(conn)
        routes = frozenset(
            t.first_route for t in (pin.trips if pin else ())
            if t.departs_at_ms - PIN_LEAD_MS <= now <= t.departs_at_ms + PIN_AFTER_MS
        )
        return _Watching(routes, None) if routes else None

    def _poll(self) -> dict[str, Vehicle] | None:
        """Every bus now, with its position added to the history; None when there is no feed."""
        if self.live is None:
            return None
        try:
            found = self.live.vehicles()
        except Exception:  # noqa: BLE001 - network or feed trouble means no vehicle data this round.
            if not self._feed_down:
                LOGGER.warning("rides: BC Transit vehicle positions unavailable", exc_info=True)
            self._feed_down = True
            return None
        self._feed_down = False
        for one in found:
            kept = self._hist.setdefault(one.vehicle_id, deque(maxlen=HIST_KEEP))
            if not kept or one.at_ms > kept[-1][0]:
                kept.append((one.at_ms, one.lat, one.lng))
        self._seen = {one.vehicle_id: one for one in found}
        return self._seen

    def _look(self, conn: sqlite3.Connection, watching: _Watching, now: int) -> None:
        """Ask him whether he boarded the bus his phone is moving with, once per trip."""
        vehicles = self._poll()
        if vehicles is None or now - self._said_ms < SAID_QUIET_MS:
            return
        fixes = [f for f in fixes_since(conn, now - FIX_WINDOW_MS) if f.at_ms <= now]
        if len(fixes) < 2:  # noqa: PLR2004 - "two consecutive fixes" is the rule.
            return
        first, second = fixes[-2], fixes[-1]
        if second.at_ms - first.at_ms > PAIR_GAP_MS or now - second.at_ms > FIX_FRESH_MS:
            return
        since = now - folded.ASKED_KEEP_MS
        asked = {c.trip_id for c in folded.candidates(conn, since)}
        asked |= {r.trip_id for r in folded.fold(conn, since) if r.trip_id}  # a ride he just had
        best: tuple[float, Vehicle] | None = None
        for one in vehicles.values():
            if one.trip_id in asked or (
                watching.routes is not None and one.route not in watching.routes
            ):
                continue
            gap = rides_with(first, second, list(self._hist.get(one.vehicle_id, ())))
            if gap is not None and (best is None or gap < best[0]):
                best = (gap, one)
        if best is not None:
            self._ask(conn, best[1], first, now)

    def _ask(self, conn: sqlite3.Connection, bus: Vehicle, first: Fix, now: int) -> None:
        """Write the question, then push it: a failed push can never ask twice."""
        candidate = folded.write_offer(
            conn, trip_id=bus.trip_id, vehicle_id=bus.vehicle_id, route=bus.route,
            first=(first.lat, first.lng), device=first.device, at_ms=now,
        )
        LOGGER.info("rides: asked whether he boarded route %s (%s)", bus.route, candidate)
        level = "off" if self.push is None else self.push.quiet()
        if self.push is None or level in PRIVATE_QUIET:
            return
        name = self.live.stop_name(bus.stop_id) if self.live and bus.stop_id else None
        body = (
            lang.t("ride.ask_body", route=bus.route, stop=name) if name
            else lang.t("ride.ask_body_plain", route=bus.route)
        )
        self.push.send(
            lang.t("ride.ask_title", route=bus.route), body, category="ride_board",
            thread="ride", data={"ride": {"kind": "board", "candidate_id": candidate,
                                          "route": bus.route}},
            device=first.device, sound=level != "quiet",
        )

    # --- starting ---------------------------------------------------------------------

    def control(self) -> RideControl:
        """The ``bus`` tool's callables."""
        return RideControl(self.watch, self.start, self.set_stop, self.end, self.status)

    def watch(self, destination: str) -> dict[str, Any]:
        """``bus(ride="watch")``: look for him boarding, any route, for ``WATCH_MS``."""
        now = self._now_ms()
        with self._lock, contextlib.closing(self._open()) as conn:
            if folded.active(conn, now) is not None:
                return {"watching": False, "note": "he is already on a bus ride"}
            watch_id, event = folded.write_watch(
                conn, destination=destination, until_ms=now + WATCH_MS, at_ms=now,
            )
        self.defer(self.tick)
        return {"watching": True, "minutes": WATCH_MS // _MIN_MS, "watch_id": watch_id,
                "event_uid": event,
                "note": "if his phone moves with a bus he is asked on his phone whether he boarded"}

    def start(
        self, route: str | None, destination: str | None, vehicle_id: str | None,
        here: HereReading | None, place: Place = lambda: None,
    ) -> dict[str, Any]:
        """``bus(ride="start")``: he boarded; find the bus and begin, or return the candidates.

        ``here`` is where Here puts him now (ADR 0322); ``None`` when it has no position to give.
        ``place`` reads where he said he is going, when that is needed to tell two buses apart or
        to find his stop (ADR 0344).
        """
        now = self._now_ms()
        with self._lock, contextlib.closing(self._open()) as conn:
            self._said_ms = now
            current = folded.active(conn, now)
            if current is not None:
                return {"started": False, "already_riding": True, **self._describe(current)}
            fix = None if here is None else Fix(
                now - here.age_s * 1000, here.lat, here.lng, here.accuracy_m, here.device,
            )
            pin = self._pin_for(conn, route, now)
            wanted = route or (pin.first_route if pin else None)
            target = self._target(pin, wanted, destination, place)
            vehicles = self._poll()
            picked = self._pick(conn, vehicles, fix, wanted, vehicle_id, target, now)
            if isinstance(picked, dict):
                return picked
            source, trip_id = "said", picked.trip_id if picked else None
            if picked is None and pin is not None and not vehicle_id:
                source, trip_id = "pin", self._pin_trip(pin)
                picked = next((v for v in (vehicles or {}).values() if v.trip_id == trip_id), None)
            if picked is None and trip_id is None and (vehicles is not None or wanted is None):
                msg = (
                    f"no route {wanted} bus is near his last position" if wanted and vehicles
                    else "no bus is near his last position" if vehicles
                    else "no live bus data here; ask him which route he is on"
                )
                raise ToolError(msg + "; ask him which bus and where he boarded", code="no_vehicle")
            ride, event = self._begin(
                conn, source=source, route=picked.route if picked else (wanted or "?"), now=now,
                vehicle=picked, trip_id=trip_id, destination=destination, pin=pin,
                near=(fix.lat, fix.lng) if fix else None, device=fix.device if fix else None,
                target=target,
            )
            result = {"started": True, "source": source, "event_uid": event,
                      **self._describe(ride)}
        self.defer(self.tick)
        return result

    def _target(
        self, pin: pins.Trip | None, route: str | None, destination: str | None, place: Place,
    ) -> tuple[tuple[float, float], str | None] | None:
        """Where his stop is: the pin's drop-off, the last plan's, else the place he said.

        Returns the point and, when Google named his stop, its name.
        """
        if pin is not None and pin.alight_lat is not None and pin.alight_lng is not None:
            return (pin.alight_lat, pin.alight_lng), pin.alight_stop
        if not destination:
            return None
        hit = self._offers.alight_for(route or "", destination) if self._offers and route else None
        if hit is not None:
            return (hit[1], hit[2]), hit[0]
        try:
            point = place()
        except ToolError:
            return None
        return None if point is None else (point, None)

    def _pick(  # noqa: PLR0913 - the feeds, his fix, what he said and the stop he is going to.
        self, conn: sqlite3.Connection, vehicles: dict[str, Vehicle] | None, fix: Fix | None,
        wanted: str | None, vehicle_id: str | None,
        target: tuple[tuple[float, float], str | None] | None, now: int,
    ) -> Vehicle | dict[str, Any] | None:
        """The bus he said he is on (ADR 0344).

        The one named; else, of the buses near him, a trip whose remaining stops include his stop
        wins, then the nearest. His fix and each bus are compared at one moment. None when there is
        no feed or fix to match by; a dict when buses of different trips are about equally near
        (the answer to give back).
        """
        if vehicles is None:
            return None
        if vehicle_id:
            if vehicle_id not in vehicles:
                msg = "that bus is not in the feed any more; ask him which bus"
                raise ToolError(msg, code="no_vehicle")
            return vehicles[vehicle_id]
        if fix is None:
            return None
        near: list[tuple[float, str, Vehicle]] = []
        for one in vehicles.values():
            if wanted is not None and one.route.casefold() != wanted.casefold():
                continue
            gap = self._gap(conn, one, fix)
            if gap is not None and gap <= SAID_RADIUS_M:
                near.append((gap, one.vehicle_id, one))
        near.sort()
        goal = self._goal(target)
        if goal:
            going = [n for n in near if self._serves(n[2], goal, now)]
            near = going or near
        if len(near) > 1 and near[1][0] - near[0][0] < AMBIGUOUS_GAP_M:
            return self._ambiguous(near[:3])
        return near[0][2] if near else None

    def _gap(self, conn: sqlite3.Connection, bus: Vehicle, fix: Fix) -> float | None:
        """How far the bus was from his fix at one moment; None when they cannot be paired.

        The bus's history is read at the fix's time when it reaches that far; else the bus's
        report is set against his fix nearest its report time, no more than 30 s apart.
        """
        place = position_at(list(self._hist.get(bus.vehicle_id, ())), fix.at_ms)
        if place is not None:
            return metres((fix.lat, fix.lng), place)
        fixes = fixes_since(conn, bus.at_ms - FIX_LOOKBACK_MS)
        nearest = min((*fixes, fix), key=lambda f: abs(f.at_ms - bus.at_ms))
        if abs(nearest.at_ms - bus.at_ms) > SAME_MOMENT_MS:
            return None
        return metres((nearest.lat, nearest.lng), (bus.lat, bus.lng))

    def _goal(self, target: tuple[tuple[float, float], str | None] | None) -> frozenset[str]:
        """The ids of the stops within a walk of where he is going."""
        if target is None or self.live is None:
            return frozenset()
        try:
            near = self.live.table().stops_near(target[0], stop_finder.NEAR_M)
        except Exception:  # noqa: BLE001 - without the timetable the nearest bus decides.
            return frozenset()
        return frozenset(s.stop_id for s, _ in near)

    def _serves(self, bus: Vehicle, goal: frozenset[str], now: int) -> bool:
        """Whether the bus's trip still calls at one of the ``goal`` stops."""
        stops = self._timetable(bus.trip_id, bus.start_date, now)
        return bool(stops) and any(s.stop_id in goal for s in stops[next_index(stops, bus, now):])

    def _ambiguous(self, near: list[tuple[float, str, Vehicle]]) -> dict[str, Any]:
        names = [self.live.stop_name(v.stop_id) if self.live and v.stop_id else None
                 for _, _, v in near]
        return {
            "started": False,
            "ambiguous": True,
            "candidates": [
                {"vehicle_id": v.vehicle_id, "route": v.route,
                 "headsign": self._headsign(v.trip_id), "next_stop": name,
                 "distance_m": round(d)}
                for (d, _, v), name in zip(near, names, strict=True)
            ],
            "note": "buses are about equally near him: ask which one, then call"
                    " bus(ride='start') again with that vehicle_id",
        }

    def _headsign(self, trip_id: str | None) -> str | None:
        """Where a trip is going, from the timetable; None when it cannot be read."""
        if trip_id is None or self.live is None:
            return None
        try:
            return self.live.headsign(trip_id) or None
        except Exception:  # noqa: BLE001 - a ride is followed without its headsign.
            return None

    def _pin_for(self, conn: sqlite3.Connection, route: str | None, now: int) -> pins.Trip | None:
        """The pinned trip his bus is most likely (route and time), or None."""
        pin = pins.current(conn)
        near = [
            t for t in (pin.trips if pin else ())
            if abs(t.departs_at_ms - now) <= PIN_WINDOW_MS
            and (route is None or t.first_route.casefold() == route.casefold())
        ]
        return min(near, key=lambda t: abs(t.departs_at_ms - now), default=None)

    def _pin_trip(self, pin: pins.Trip) -> str | None:
        """The GTFS trip of a pinned bus, by its stop and time; None when it cannot be found."""
        if self.live is None or pin.stop_lat is None or pin.stop_lng is None:
            return None
        try:
            found = self.live.departure(
                pin.first_route, (pin.stop_lat, pin.stop_lng), pin.departs_at_ms,
            )
        except Exception:  # noqa: BLE001 - the export or the feed failing means no match.
            LOGGER.warning("rides: could not match the pinned trip", exc_info=True)
            return None
        return found[0] if found else None

    def _begin(  # noqa: PLR0913 - everything a ride begins with.
        self, conn: sqlite3.Connection, *, source: str, route: str, now: int,
        vehicle: Vehicle | None, trip_id: str | None, destination: str | None,
        pin: pins.Trip | None, near: tuple[float, float] | None, device: str | None,
        target: tuple[tuple[float, float], str | None] | None = None,
    ) -> tuple[folded.Ride, str]:
        service_date = vehicle.start_date if vehicle else None
        stops = self._timetable(trip_id, service_date, now)
        here = near or ((vehicle.lat, vehicle.lng) if vehicle else None)
        board = nearest_stop(stops, here) if stops and here else None
        ride = folded.Ride(
            ride_id=folded.new_id("ride"), route=route, source=source, started_at_ms=now,
            trip_id=trip_id, vehicle_id=vehicle.vehicle_id if vehicle else None,
            board_stop=board.name if board else (pin.board_stop if pin else None),
            board_stop_id=board.stop_id if board else None, destination=destination,
            pin_tid=pin.tid if pin else None, service_date=service_date,
            scheduled_end_ms=stops[-1].sched_ms if stops else now + FALLBACK_RIDE_MS,
            device=device, headsign=self._headsign(trip_id),
        )
        self._resolve_stop(ride, stops, board, target)
        event = folded.write_start(conn, ride)
        self._activity, self._passed_ms = None, None
        self._preds, self._pred_ms = {}, 0
        LOGGER.info("rides: ride %s started on route %s (%s)", ride.ride_id, route, source)
        self._snap = (ride, progress(
            stops, ride.alight_stop_id, vehicle, {}, now, alight_seq=ride.alight_seq,
        ) if stops else None)
        return ride, event

    def _resolve_stop(
        self, ride: folded.Ride, stops: list[StopTime], board: StopTime | None,
        target: tuple[tuple[float, float], str | None] | None,
    ) -> None:
        """Fill in the stop he gets off at, from his pin or the place he is going, if findable.

        It is the trip's stop nearest his stop's place, ahead of the one he boarded at and no
        farther than a walk (300 m) from it (ADR 0344).
        """
        if target is None:
            return
        point, named = target
        ahead = [s for s in stops if board is None or s.seq > board.seq]
        stop = nearest_stop(ahead, point)
        if stop is not None:
            ride.alight_stop = stop.name
            ride.alight_stop_id, ride.alight_seq = stop.stop_id, stop.seq
        elif named:
            ride.alight_stop = named

    def _timetable(self, trip_id: str | None, service_date: str | None, now: int) -> list[StopTime]:
        """The trip's stops with their scheduled times; empty when the export cannot be read."""
        if trip_id is None or self.live is None:
            return []
        key = (trip_id, service_date)
        if key in self._stops:
            return self._stops[key]
        if now - self._stops_failed_ms < STOPS_RETRY_MS:
            return []
        try:
            self._stops[key] = self.live.trip_stops(trip_id, service_date)
        except Exception:  # noqa: BLE001 - the export failing means a ride on its feed alone.
            LOGGER.warning("rides: the timetable of the trip could not be read", exc_info=True)
            self._stops_failed_ms = now
            return []
        return self._stops[key]

    # --- the taps and the routes ---------------------------------------------------------

    def act(self, body: dict[str, Any]) -> dict[str, Any]:
        """``POST /inherent/ride``: his answer to the boarding question, or ending the ride.

        A LookupError (404) when the question or ride is unknown.
        """
        now = self._now_ms()
        with self._lock, contextlib.closing(self._open()) as conn:
            if body["action"] == "end":
                ride = folded.active(conn, now)
                if ride is None or (body.get("ride_id") and body["ride_id"] != ride.ride_id):
                    msg = "no such ride"
                    raise LookupError(msg)
                self._finish(conn, ride, "said", now)
                return {"ride": None, "reason": None}
            found = {c.candidate_id: c for c in folded.candidates(conn, now - folded.ASKED_KEEP_MS)}
            cand = found.get(body["candidate_id"])
            if cand is None:
                msg = "that question is gone"
                raise LookupError(msg)
            if cand.answer is not None:
                return {"ride": self.view(), "reason": "answered"}
            said = "yes" if body["action"] == "board" else "no"
            folded.write_answer(conn, cand.candidate_id, said, now)
            if body["action"] == "not":
                return {"ride": self.view(), "reason": None}
            if folded.active(conn, now) is not None:
                return {"ride": self.view(), "reason": "already_riding"}
            bus = self._seen.get(cand.vehicle_id) or (self._poll() or {}).get(cand.vehicle_id)
            pin = self._pin_for(conn, cand.route, now)
            self._begin(
                conn, source="tapped", route=cand.route, now=now, vehicle=bus,
                trip_id=cand.trip_id, destination=None, pin=pin,
                near=None if cand.first_lat is None or cand.first_lng is None
                else (cand.first_lat, cand.first_lng),
                device=cand.device, target=self._target(pin, cand.route, None, lambda: None),
            )
        self.defer(self.tick)
        return {"ride": self.view(), "reason": None}

    def end(self) -> dict[str, Any]:
        """``bus(ride="end")``: he says he is off the bus."""
        now = self._now_ms()
        with self._lock, contextlib.closing(self._open()) as conn:
            ride = folded.active(conn, now)
            if ride is None:
                return {"ended": False, "note": "no ride is active"}
            event = self._finish(conn, ride, "said", now)
        return {"ended": True, "route": ride.route, "headsign": ride.headsign,
                "ride_id": ride.ride_id, "event_uid": event}

    def set_stop(
        self, words: str, here: HereReading | None = None, place: Place = lambda: None,
    ) -> dict[str, Any]:
        """``bus(ride="get_off")``: the stop he gets off at, by its name or a place (ADR 0344).

        Only the stops the bus has not reached yet count. A stop the followed trip does not serve,
        but another trip of the same route does, means the wrong trip was matched: when exactly
        one bus of such a trip is near his fix the ride moves to it, else the followed bus's
        headsign is said. ``place`` reads where he said, when no stop has that name.
        """
        now = self._now_ms()
        with self._lock, contextlib.closing(self._open()) as conn:
            ride = folded.active(conn, now)
            if ride is None:
                msg = "no ride is active"
                raise ToolError(msg, code="no_ride")
            stops = self._timetable(ride.trip_id, ride.service_date, now)
            bus = self._seen.get(ride.vehicle_id or "")
            reached = next_index(stops, bus if bus and bus.trip_id == ride.trip_id else None, now)
            ahead = [s for s in stops[reached:] if s.stop_id != ride.board_stop_id]
            point: tuple[float, float] | None = None
            hits = stop_named(ahead, words)
            if not hits:
                point = self._point(place)
                near = None if point is None else nearest_stop(ahead, point)
                hits = [near] if near else []
            if len({h.name for h in hits}) > 1:
                return {"set": False, "ambiguous": True,
                        "candidates": sorted({h.name for h in hits})[:5],
                        "note": "ask which stop he means, then call bus(ride='get_off') again"}
            moved = ""
            if not hits and stops:
                hits, moved = self._elsewhere(conn, ride, words, point, here, stops[:reached], now)
                stops = self._timetable(ride.trip_id, ride.service_date, now)
            if hits and hits[0].stop_id == ride.alight_stop_id and not moved:
                # ADR 0263: the same stop again, from a later turn: nothing to write.
                return {"set": True, "already": True, **self._describe(ride)}
            if hits:
                stop = hits[0]
                event = folded.write_alight(
                    conn, ride.ride_id, stop=stop.name, stop_id=stop.stop_id, seq=stop.seq,
                    at_ms=now,
                )
                ride.alight_stop, ride.alight_stop_id, ride.alight_seq = (
                    stop.name, stop.stop_id, stop.seq)
            elif not stops:
                event = folded.write_alight(
                    conn, ride.ride_id, stop=words, stop_id=None, seq=None, at_ms=now,
                )
                ride.alight_stop = words
            else:
                msg = (
                    f"no stop of the {ride.route} toward {ride.headsign or 'its end'} that is still"
                    " ahead matches that, and no other bus of the route near him goes there; ask"
                    " him for the stop's name or where he is going"
                )
                raise ToolError(msg, code="no_such_stop")
            self._refresh(ride, now, self._seen.get(ride.vehicle_id or ""))
            described = self._describe(ride)
        self.defer(self.tick)
        return {"set": True, "event_uid": event, **({"moved": moved} if moved else {}),
                **described}

    @staticmethod
    def _point(place: Place) -> tuple[float, float] | None:
        try:
            return place()
        except ToolError:
            return None

    def _elsewhere(  # noqa: PLR0913 - the ride, what he said, where, his fix, the stops passed.
        self, conn: sqlite3.Connection, ride: folded.Ride, words: str,
        point: tuple[float, float] | None, here: HereReading | None, passed: list[StopTime],
        now: int,
    ) -> tuple[list[StopTime], str]:
        """No stop ahead matches: a passed one (refused), or another trip of the route (moved).

        Returns the stop on the trip the ride now follows and the headsign it left, or nothing.
        """
        behind = stop_named(passed, words) or (
            [s] if point is not None and (s := nearest_stop(passed, point)) else []
        )
        if behind:
            msg = (
                f"the {ride.route} toward {ride.headsign or 'its end'} has already passed"
                f" {behind[0].name}; ask him for a stop still ahead of the bus"
            )
            raise ToolError(msg, code="stop_passed")
        if here is None or self.live is None:
            return [], ""
        fix = Fix(now - here.age_s * 1000, here.lat, here.lng, here.accuracy_m, here.device)
        found: list[tuple[Vehicle, list[StopTime]]] = []
        for one in (self._poll() or self._seen).values():
            if one.route != ride.route or one.trip_id == ride.trip_id:
                continue
            gap = self._gap(conn, one, fix)
            if gap is None or gap > SAID_RADIUS_M:
                continue
            stops = self._timetable(one.trip_id, one.start_date, now)
            rest = stops[next_index(stops, one, now):]
            hits = stop_named(rest, words) or (
                [s] if point is not None and (s := nearest_stop(rest, point)) else []
            )
            if hits:
                found.append((one, hits))
        if len(found) != 1:
            return [], ""
        bus, hits = found[0]
        was = ride.headsign or ""
        stops = self._timetable(bus.trip_id, bus.start_date, now)
        ride.trip_id, ride.vehicle_id = bus.trip_id, bus.vehicle_id
        ride.headsign, ride.service_date = self._headsign(bus.trip_id), bus.start_date
        ride.scheduled_end_ms = stops[-1].sched_ms if stops else ride.scheduled_end_ms
        folded.write_moved(
            conn, ride.ride_id, trip_id=bus.trip_id, vehicle_id=bus.vehicle_id,
            headsign=ride.headsign, service_date=bus.start_date,
            scheduled_end_ms=ride.scheduled_end_ms, at_ms=now,
        )
        self._passed_ms = None
        return hits, was or "its end"

    # --- following ---------------------------------------------------------------------

    def _follow(self, conn: sqlite3.Connection, ride: folded.Ride, now: int) -> None:
        vehicles = self._poll()
        bus = vehicles.get(ride.vehicle_id) if vehicles and ride.vehicle_id else None
        prog = self._refresh(ride, now, bus)
        reason = self._ending(conn, ride, bus, prog, now)
        if reason is not None:
            self._finish(conn, ride, *reason)
            return
        # One stop left is known from the bus's own position; a timetable alone can be minutes off.
        if ride.alight_stop_id and not ride.warned and prog.stops_left == 1 and prog.position_live:
            self._warn(conn, ride, prog, now)
        self._tell_phone(ride, prog, now)

    def _refresh(self, ride: folded.Ride, now: int, bus: Vehicle | None = None) -> Progress:
        """The ride's progress, kept for :meth:`line`. ``bus`` None is the timetable."""
        stops = self._timetable(ride.trip_id, ride.service_date, now)
        live_bus = bus if bus is not None and bus.trip_id == ride.trip_id else None
        if live_bus is not None and now - self._pred_ms >= PREDICT_EVERY_MS and self.live:
            self._pred_ms = now
            try:
                self._preds = self.live.predicted(self.live.snapshot(), ride.trip_id or "")
            except Exception:  # noqa: BLE001 - the arrival falls back to the timetable.
                LOGGER.warning("rides: BC Transit trip updates unavailable", exc_info=True)
        if not stops:  # no timetable: nothing to say of where the ride is
            self._snap = (ride, None)
            return Progress(None, None, None, None, passed=False, position_live=False)
        prog = progress(
            stops, ride.alight_stop_id, live_bus, self._preds, now, alight_seq=ride.alight_seq,
        )
        self._snap = (ride, prog)
        return prog

    def _ending(
        self, conn: sqlite3.Connection, ride: folded.Ride, bus: Vehicle | None, prog: Progress,
        now: int,
    ) -> tuple[str, int] | None:
        """``(reason, when)`` of the first ending rule that holds, or None."""
        end = ride.scheduled_end_ms
        if end is not None and now >= end + END_GRACE_MS:
            return "schedule_end", end
        if bus is not None:
            fixes = fixes_since(conn, ride.started_at_ms)
            gone = left_at(fixes, list(self._hist.get(bus.vehicle_id, ())))
            if gone is not None:
                return "left_vehicle", gone
        if prog.passed and prog.position_live:
            self._passed_ms = self._passed_ms or now
            if motion_now(conn, self._passed_ms - PASSED_BACK_MS) in _STILL:
                return "passed_stop", now
        else:
            self._passed_ms = None
        return None

    def _finish(
        self, conn: sqlite3.Connection, ride: folded.Ride, reason: str, at_ms: int,
    ) -> str:
        event = folded.write_end(conn, ride.ride_id, reason, max(at_ms, ride.started_at_ms))
        LOGGER.info("rides: ride %s ended (%s)", ride.ride_id, reason)
        snap = self._snap
        prog = snap[1] if snap and snap[0].ride_id == ride.ride_id else None
        if self.push is not None:
            self.push.ride_activity(
                ride.device, "end", self._attributes(ride), self._content(ride, prog, "ended"),
            )
        self._snap, self._activity = None, None
        self._passed_ms = None
        self._idle()
        return event

    def _warn(self, conn: sqlite3.Connection, ride: folded.Ride, prog: Progress, now: int) -> None:
        """One stop before his: the Live Activity buzzes at priority 10, else a plain alert."""
        folded.write_warned(conn, ride.ride_id, now)
        ride.warned = True
        if self.push is None:
            return
        title, body = lang.t("ride.warn_title"), lang.t("ride.warn_body", stop=ride.alight_stop)
        content = self._content(ride, prog, "arriving")
        sent = self.push.ride_activity(
            ride.device, "update", self._attributes(ride), content, urgent=True,
            alert=(title, body),
        )
        if sent:
            if self._activity is not None and self._activity.ride_id == ride.ride_id:
                self._activity.wire = json.dumps(content, sort_keys=True)
        else:
            self.push.send(
                title, body, category="ride_alight", thread="ride", time_sensitive=True,
                data={"ride": {"kind": "alight", "ride_id": ride.ride_id}}, device=ride.device,
            )

    # --- the phone ----------------------------------------------------------------------

    @staticmethod
    def _attributes(ride: folded.Ride) -> dict[str, Any]:
        """The Live Activity's static attributes (``RideAttributes``)."""
        return {
            "ride_id": ride.ride_id, "route": ride.route, "board_stop": ride.board_stop,
            "started_at_ms": ride.started_at_ms,
        }

    @staticmethod
    def _content(ride: folded.Ride, prog: Progress | None, phase: str = "") -> dict[str, Any]:
        """The Live Activity's content state (``RideAttributes.ContentState``)."""
        return {
            "route": ride.route,
            "headsign": ride.headsign,
            "next_stop": prog.next_stop if prog else None,
            "stops_left": prog.stops_left if prog else None,
            "minutes_to_alight": prog.minutes if prog else None,
            "alight_stop": ride.alight_stop,
            "position_live": prog is not None and prog.position_live,
            "arrival_live": prog is not None and prog.arrival_live,
            "phase": phase or ("arriving" if ride.warned else "riding"),
        }

    def _tell_phone(self, ride: folded.Ride, prog: Progress, now: int) -> None:
        """Start the ride's Live Activity once, then update it whenever its content changed."""
        if self.push is None:
            return
        state = self._activity
        if state is None or state.ride_id != ride.ride_id:
            state = self._activity = _Activity(ride.ride_id)
        content = self._content(ride, prog)
        wire = json.dumps(content, sort_keys=True)
        attributes = self._attributes(ride)
        if not state.started:
            if now - ride.started_at_ms <= START_WINDOW_MS and self.push.ride_activity(
                ride.device, "start", attributes, content,
            ):
                state.started, state.wire = True, wire
        elif wire != state.wire and self.push.ride_activity(
            ride.device, "update", attributes, content,
        ):
            state.wire = wire

    # --- what is read -------------------------------------------------------------------

    def _describe(self, ride: folded.Ride) -> dict[str, Any]:
        snap = self._snap
        prog = snap[1] if snap and snap[0].ride_id == ride.ride_id else None
        return {
            "ride_id": ride.ride_id, "route": ride.route, "headsign": ride.headsign,
            "board_stop": ride.board_stop,
            "alight_stop": ride.alight_stop, "alight_known": ride.alight_stop is not None,
            "next_stop": prog.next_stop if prog else None,
            "stops_left": prog.stops_left if prog else None,
            "minutes_to_alight": prog.minutes if prog else None,
            "eta_ms": prog.eta_ms if prog else None,
            "position_live": prog is not None and prog.position_live,
            "arrival_live": prog is not None and prog.arrival_live,
            "started_at_ms": ride.started_at_ms, "source": ride.source, "warned": ride.warned,
            "credit": CREDIT if ride.trip_id else None,
        }

    def status(self) -> dict[str, Any] | None:
        """The ride in force now, read from the log (``bus`` with no arguments); None if none."""
        now = self._now_ms()
        with self._lock, contextlib.closing(self._open()) as conn:
            ride = folded.active(conn, now)
        return None if ride is None else self._describe(ride)

    def view(self) -> dict[str, Any] | None:
        """The ride now, as ``GET /inherent/ride`` serves it; None when he is not on one."""
        snap = self._snap
        return None if snap is None else self._describe(snap[0])

    def line(self) -> str | None:
        """The state-block line of every turn while he rides (a ``live_context`` producer).

        Cheap and lock-free: it reads the snapshot the ticks keep. It tells the model once per
        ride to ask where he gets off when that is unknown.
        """
        snap = self._snap
        if snap is None:
            return None
        ride, prog = snap
        text = f"{RIDE_LINE_PREFIX}he is on route {ride.route}"
        if ride.headsign:
            text += f" toward {ride.headsign}"
        if ride.board_stop:
            text += f", boarded at {ride.board_stop} at {_clock(ride.started_at_ms)}"
        text += ". Answer questions about this bus, never another bus of the route."
        if prog is not None and prog.next_stop:
            text += f" Next stop: {prog.next_stop}."
        if ride.alight_stop is None:
            if ride.ride_id in self._asked_stop:
                text += " His stop is still unknown (you already asked)."
            else:
                self._asked_stop.add(ride.ride_id)
                text += (" His stop is unknown: ask him once where he gets off, then call"
                         " bus(ride='get_off', stop=<stop>).")
        else:
            text += f" His stop: {ride.alight_stop}"
            if prog is not None and prog.eta_ms is not None:
                text += (f", {prog.stops_left} stops left, arrives about {_clock(prog.eta_ms)}"
                         f" (in {prog.minutes} min)")
            text += "."
        if prog is None:
            text += " There is no bus data for this trip: say you cannot give times."
        else:
            text += " " + sources_said(
                position_live=prog.position_live, arrival_live=prog.arrival_live,
                has_arrival=prog.eta_ms is not None,
            )
        return text[:RIDE_LINE_CHARS]
