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
  ``watch_for_boarding`` after he said he is heading out): when two consecutive phone fixes are
  within 60 m of one bus and move with it, ask him once, by notification, whether he boarded.
- **Neither**: forget the vehicle history and return.

He starts a ride by tapping the notification's button (:meth:`Rides.act`) or by saying so
(``start_ride``); it is never inferred. When the feed fails the ride falls back to the timetable
and says ``stale``. Everything runs off the event loop; the one thing read on it is :meth:`line`,
from a snapshot the ticks keep.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final
from zoneinfo import ZoneInfo

from jarvis.execution.ride_tool import RideControl
from jarvis.execution.tools import ToolError
from jarvis.shared import lang
from jarvis.shared.geo import metres
from jarvis.state import departures as pins
from jarvis.state import rides as folded
from jarvis.state.event_log import open_runtime_event_log
from jarvis.state.phone_location import fixes_since, motion_now, visit_position
from jarvis.state.ride_progress import (
    Fix,
    Progress,
    Sample,
    StopTime,
    Vehicle,
    left_at,
    nearest_stop,
    position_at,
    progress,
    rides_with,
    stop_named,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from jarvis.execution.transit_tool import TransitOffers
    from jarvis.runtime.bus_live import BusLive
    from jarvis.runtime.push import Push

LOGGER = logging.getLogger(__name__)

TICK_S: Final = 15.0
_MIN_MS: Final = 60_000
PIN_LEAD_MS: Final = 10 * _MIN_MS
"""A pinned trip is watched from this long before its bus leaves ..."""
PIN_AFTER_MS: Final = 15 * _MIN_MS
"""... until this long after, since he is only seen moving with the bus once it has left."""
WATCH_MS: Final = 45 * _MIN_MS
"""How long ``watch_for_boarding`` looks for him."""
FIX_WINDOW_MS: Final = 4 * _MIN_MS
FIX_FRESH_MS: Final = 2 * _MIN_MS
"""The newest of the two fixes that show him moving with a bus is no older than this."""
PAIR_GAP_MS: Final = 2 * _MIN_MS
"""The two fixes are no farther apart than this (the phone sends one every 30 s on the move)."""
SAID_QUIET_MS: Final = 5 * _MIN_MS
"""After he says he boarded, even a ride that did not start gets no notification for this long."""
SAID_FIX_MS: Final = 10 * _MIN_MS
SAID_RADIUS_M: Final = 300.0
"""The bus nearest his last fix counts when it is no farther; a fix is up to a minute old."""
AMBIGUOUS_GAP_M: Final = 100.0
"""A second bus closer than this behind the nearest one makes the match ambiguous."""
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
RIDE_LINE_PREFIX: Final = "Bus ride: "
RIDE_LINE_CHARS: Final = 520

_ZONE: Final = ZoneInfo("America/Vancouver")
_STILL: Final = frozenset({"walking", "running", "stationary"})


def _clock(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, _ZONE).strftime("%H:%M")


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
        places: Callable[[], Mapping[str, str]] | None = None,
        offers: TransitOffers | None = None,
    ) -> None:
        """``live`` is BC Transit's feeds (without it a ride is only what he declares).

        ``places`` reads the kept ``home`` and ``school`` as ``lat,lng`` at each use (ADR 0234) and
        ``offers`` is what the last ``transit`` answer said about where his bus lets him off; both
        only help find his stop.
        """
        self._path = event_log
        self.live = live
        self._places: Callable[[], Mapping[str, str]] = places or dict
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
        """The four model tools' callables."""
        return RideControl(self.watch, self.start, self.set_stop, self.end)

    def watch(self, destination: str) -> dict[str, Any]:
        """``watch_for_boarding``: look for him boarding, any route, for ``WATCH_MS``."""
        now = self._now_ms()
        with self._lock, contextlib.closing(self._open()) as conn:
            if folded.active(conn, now) is not None:
                return {"watching": False, "note": "he is already on a bus ride"}
            folded.write_watch(conn, destination=destination, until_ms=now + WATCH_MS, at_ms=now)
        self.defer(self.tick)
        return {"watching": True, "minutes": WATCH_MS // _MIN_MS,
                "note": "if his phone moves with a bus he is asked on his phone whether he boarded"}

    def start(
        self, route: str | None, destination: str | None, vehicle_id: str | None,
    ) -> dict[str, Any]:
        """``start_ride``: he said he boarded; find the bus and begin, or return the candidates."""
        now = self._now_ms()
        with self._lock, contextlib.closing(self._open()) as conn:
            self._said_ms = now
            current = folded.active(conn, now)
            if current is not None:
                return {"started": False, "already_riding": True, **self._describe(current)}
            fix = next(iter(reversed(fixes_since(conn, now - SAID_FIX_MS))), None)
            pin = self._pin_for(conn, route, now)
            wanted = route or (pin.first_route if pin else None)
            vehicles = self._poll()
            picked = self._pick(vehicles, fix, wanted, vehicle_id)
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
            ride = self._begin(
                conn, source=source, route=picked.route if picked else (wanted or "?"), now=now,
                vehicle=picked, trip_id=trip_id, destination=destination, pin=pin,
                near=(fix.lat, fix.lng) if fix else None, device=fix.device if fix else None,
            )
            result = {"started": True, "source": source, **self._describe(ride)}
        self.defer(self.tick)
        return result

    def _pick(
        self, vehicles: dict[str, Vehicle] | None, fix: Fix | None, wanted: str | None,
        vehicle_id: str | None,
    ) -> Vehicle | dict[str, Any] | None:
        """The bus he said he is on: the one named, else the nearest to his last fix.

        None when there is no feed or fix to match by; a dict when two buses are about equally
        near (the answer to give back).
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
        near = sorted(
            (self._gap(one, fix), one.vehicle_id, one) for one in vehicles.values()
            if wanted is None or one.route.casefold() == wanted.casefold()
        )
        near = [n for n in near if n[0] <= SAID_RADIUS_M]
        if len(near) > 1 and near[1][0] - near[0][0] < AMBIGUOUS_GAP_M:
            return self._ambiguous(near[:3])
        return near[0][2] if near else None

    def _gap(self, bus: Vehicle, fix: Fix) -> float:
        """How far the bus was from this fix, at the fix's time when the history reaches."""
        place = position_at(list(self._hist.get(bus.vehicle_id, ())), fix.at_ms)
        return metres((fix.lat, fix.lng), place or (bus.lat, bus.lng))

    def _ambiguous(self, near: list[tuple[float, str, Vehicle]]) -> dict[str, Any]:
        names = [self.live.stop_name(v.stop_id) if self.live and v.stop_id else None
                 for _, _, v in near]
        return {
            "started": False,
            "ambiguous": True,
            "candidates": [
                {"vehicle_id": v.vehicle_id, "route": v.route, "next_stop": name,
                 "distance_m": round(d)}
                for (d, _, v), name in zip(near, names, strict=True)
            ],
            "note": "two buses are about equally near him: ask which one, then call start_ride"
                    " again with that vehicle_id",
        }

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
    ) -> folded.Ride:
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
            device=device,
        )
        self._resolve_stop(conn, ride, stops, board, pin)
        folded.write_start(conn, ride)
        self._activity, self._passed_ms = None, None
        self._preds, self._pred_ms = {}, 0
        LOGGER.info("rides: ride %s started on route %s (%s)", ride.ride_id, route, source)
        self._snap = (ride, progress(
            stops, ride.alight_stop_id, vehicle, {}, now, alight_seq=ride.alight_seq,
        ) if stops else None)
        return ride

    def _resolve_stop(
        self, conn: sqlite3.Connection, ride: folded.Ride, stops: list[StopTime],
        board: StopTime | None, pin: pins.Trip | None,
    ) -> None:
        """Fill in the stop he gets off at, from his pin or the place he is going, if findable."""
        target: tuple[float, float] | None = None
        named: str | None = None
        if pin is not None and pin.alight_lat is not None and pin.alight_lng is not None:
            target, named = (pin.alight_lat, pin.alight_lng), pin.alight_stop
        elif ride.destination:
            hit = self._offers.alight_for(ride.route, ride.destination) if self._offers else None
            if hit is not None:
                target, named = (hit[1], hit[2]), hit[0]
            else:
                target = self._place(conn, ride.destination)
        if target is None:
            return
        ahead = [s for s in stops if board is None or s.seq > board.seq]
        stop = nearest_stop(ahead, target)
        if stop is not None:
            ride.alight_stop = stop.name
            ride.alight_stop_id, ride.alight_seq = stop.stop_id, stop.seq
        elif named:
            ride.alight_stop = named

    def _place(self, conn: sqlite3.Connection, words: str) -> tuple[float, float] | None:
        """A named place's position: his saved places, else somewhere his phone put him by name."""
        saved = self._places().get(words.strip().casefold())
        if saved:
            lat, _, lng = saved.partition(",")
            with contextlib.suppress(ValueError):
                return float(lat), float(lng)
        return visit_position(conn, words.strip())

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
            self._begin(
                conn, source="tapped", route=cand.route, now=now, vehicle=bus,
                trip_id=cand.trip_id, destination=None, pin=self._pin_for(conn, cand.route, now),
                near=None if cand.first_lat is None or cand.first_lng is None
                else (cand.first_lat, cand.first_lng),
                device=cand.device,
            )
        self.defer(self.tick)
        return {"ride": self.view(), "reason": None}

    def end(self) -> dict[str, Any]:
        """``end_ride``: he says he is off the bus."""
        now = self._now_ms()
        with self._lock, contextlib.closing(self._open()) as conn:
            ride = folded.active(conn, now)
            if ride is None:
                return {"ended": False, "note": "no ride is active"}
            self._finish(conn, ride, "said", now)
        return {"ended": True, "route": ride.route}

    def set_stop(self, words: str) -> dict[str, Any]:
        """``set_ride_stop``: the stop he gets off at, by its name or a place."""
        now = self._now_ms()
        with self._lock, contextlib.closing(self._open()) as conn:
            ride = folded.active(conn, now)
            if ride is None:
                msg = "no ride is active"
                raise ToolError(msg, code="no_ride")
            stops = self._timetable(ride.trip_id, ride.service_date, now)
            ahead = [s for s in stops if s.stop_id != ride.board_stop_id]
            hits = stop_named(ahead, words)
            if not hits and (spot := self._place(conn, words)) is not None:
                hit = nearest_stop(ahead, spot)
                hits = [hit] if hit else []
            if len({h.name for h in hits}) > 1:
                return {"set": False, "ambiguous": True,
                        "candidates": sorted({h.name for h in hits})[:5],
                        "note": "ask which stop he means, then call set_ride_stop again"}
            if hits:
                stop = hits[0]
                folded.write_alight(conn, ride.ride_id, stop=stop.name, stop_id=stop.stop_id,
                                    seq=stop.seq, at_ms=now)
                ride.alight_stop, ride.alight_stop_id, ride.alight_seq = (
                    stop.name, stop.stop_id, stop.seq)
            elif not stops:
                folded.write_alight(conn, ride.ride_id, stop=words, stop_id=None, seq=None,
                                    at_ms=now)
                ride.alight_stop = words
            else:
                msg = "no stop of this trip matches that; ask him for the stop's name"
                raise ToolError(msg, code="no_such_stop")
            self._refresh(ride, now)
        self.defer(self.tick)
        return {"set": True, **self._describe(ride)}

    # --- following ---------------------------------------------------------------------

    def _follow(self, conn: sqlite3.Connection, ride: folded.Ride, now: int) -> None:
        vehicles = self._poll()
        bus = vehicles.get(ride.vehicle_id) if vehicles and ride.vehicle_id else None
        prog = self._refresh(ride, now, bus)
        reason = self._ending(conn, ride, bus, prog, now)
        if reason is not None:
            self._finish(conn, ride, *reason)
            return
        if ride.alight_stop_id and not ride.warned and prog.stops_left == 1 and not prog.stale:
            self._warn(conn, ride, prog, now)
        self._tell_phone(ride, prog, now)

    def _refresh(self, ride: folded.Ride, now: int, bus: Vehicle | None = None) -> Progress:
        """The ride's progress, kept for :meth:`line`. ``bus`` None is the timetable (stale)."""
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
            return Progress(None, None, None, None, passed=False, stale=True)
        prog = progress(
            stops, ride.alight_stop_id, live_bus, self._preds, now, alight_seq=ride.alight_seq,
        )
        if prog.eta_ms is not None and ride.alight_stop_id not in self._preds:
            prog = replace(prog, stale=True)  # the arrival is a timetable time
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
        if prog.passed and not prog.stale:
            self._passed_ms = self._passed_ms or now
            if motion_now(conn, self._passed_ms - PASSED_BACK_MS) in _STILL:
                return "passed_stop", now
        else:
            self._passed_ms = None
        return None

    def _finish(
        self, conn: sqlite3.Connection, ride: folded.Ride, reason: str, at_ms: int,
    ) -> None:
        folded.write_end(conn, ride.ride_id, reason, max(at_ms, ride.started_at_ms))
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
            "next_stop": prog.next_stop if prog else None,
            "stops_left": prog.stops_left if prog else None,
            "minutes_to_alight": prog.minutes if prog else None,
            "alight_stop": ride.alight_stop,
            "stale": True if prog is None else prog.stale,
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
            "ride_id": ride.ride_id, "route": ride.route, "board_stop": ride.board_stop,
            "alight_stop": ride.alight_stop, "alight_known": ride.alight_stop is not None,
            "next_stop": prog.next_stop if prog else None,
            "stops_left": prog.stops_left if prog else None,
            "minutes_to_alight": prog.minutes if prog else None,
            "eta_ms": prog.eta_ms if prog else None,
            "stale": True if prog is None else prog.stale,
            "live": self.live is not None and not self._feed_down,
            "started_at_ms": ride.started_at_ms, "source": ride.source, "warned": ride.warned,
            "credit": CREDIT if ride.trip_id else None,
        }

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
                         " set_ride_stop.")
        else:
            text += f" His stop: {ride.alight_stop}"
            if prog is not None and prog.eta_ms is not None:
                text += (f", {prog.stops_left} stops left, arrives about {_clock(prog.eta_ms)}"
                         f" (in {prog.minutes} min)")
            text += "."
        if prog is None:
            text += " There is no bus data for this trip: say you cannot give times."
        elif prog.stale:
            text += " No live bus position now: these are timetable times and may be off."
        else:
            text += " Times are from BC Transit's live bus position."
        return text[:RIDE_LINE_CHARS]
