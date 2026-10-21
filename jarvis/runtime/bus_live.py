"""BC Transit's live departures (ADR 0200) and the timetable store behind every bus time (ADR 0342).

Google plans the trip; this reads what the bus is actually doing. The realtime feed
(GTFS-realtime ``TripUpdates``, about 30 s fresh, no key) says per trip and stop when the bus will
leave. Its trips and stops are named by the static GTFS export (a zip of about 16 MB), fetched at
most once a day, in the background once there is a copy. Each export is indexed once, off the
request path, into the timetable store (:mod:`jarvis.state.timetable`) beside it, and only the
first store ever built is waited for. Google gives no BC Transit ids, so a Google bus leg is matched
to a trip by position and stop order (:meth:`BusLive.timed`), against one :meth:`BusLive.snapshot`
of the feed that all the legs of an answer share.

A followed ride (ADR 0224) reads the same store and the VehiclePositions feed
(``vehicleupdates.pb``, about 13 KB): :meth:`BusLive.vehicles` for the buses,
:meth:`BusLive.trip_stops` for one trip's stops with their scheduled times and
:meth:`BusLive.predicted` for the live arrivals of a trip.
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
import urllib.request
from datetime import datetime
from typing import TYPE_CHECKING, Final

from google.transit import gtfs_realtime_pb2

from jarvis.state import timetable
from jarvis.state.ride_progress import Vehicle

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from jarvis.state.ride_progress import StopTime

LOGGER = logging.getLogger(__name__)

FEED_URL: Final = "https://bct.tmix.se/gtfs-realtime/tripupdates.pb?operatorIds=48"
VEHICLES_URL: Final = "https://bct.tmix.se/gtfs-realtime/vehicleupdates.pb?operatorIds=48"
STATIC_URL: Final = "https://bct.tmix.se/Tmix.Cap.TdExport.WebApi/gtfs/?operatorIds=48"
STATIC_MAX_AGE_S: Final = 24 * 3600
STOP_RADIUS_M: Final = timetable.STOP_RADIUS_M
MATCH_WINDOW_MS: Final = timetable.MATCH_WINDOW_MS
_FEED_TIMEOUT_S: Final = 10.0
_STATIC_TIMEOUT_S: Final = 120.0
_NEXT_TRIPS_TRIED: Final = 6


def _fetch_feed() -> bytes:
    with urllib.request.urlopen(FEED_URL, timeout=_FEED_TIMEOUT_S) as reply:  # noqa: S310 - fixed https URL.
        data: bytes = reply.read()
    return data


def _fetch_vehicles() -> bytes:
    with urllib.request.urlopen(VEHICLES_URL, timeout=_FEED_TIMEOUT_S) as reply:  # noqa: S310 - fixed https URL.
        data: bytes = reply.read()
    return data


def _download_static(dest: Path) -> None:
    reply = urllib.request.urlopen(STATIC_URL, timeout=_STATIC_TIMEOUT_S)  # noqa: S310
    with reply, dest.open("wb") as out:
        shutil.copyfileobj(reply, out)


def live_departure(feed: gtfs_realtime_pb2.FeedMessage, trip_id: str, stop_id: str) -> int | None:
    """When the feed says ``trip_id`` leaves ``stop_id`` (epoch ms), else when it arrives."""
    for entity in feed.entity:
        update = entity.trip_update
        if not entity.HasField("trip_update") or update.trip.trip_id != trip_id:
            continue
        for one in update.stop_time_update:
            if one.stop_id != stop_id:
                continue
            event = one.departure if one.HasField("departure") else one.arrival
            if event.time:
                return int(event.time) * 1000
    return None


def live_arrival(feed: gtfs_realtime_pb2.FeedMessage, trip_id: str, stop_id: str) -> int | None:
    """When the feed says ``trip_id`` reaches ``stop_id`` (epoch ms), else when it leaves."""
    for entity in feed.entity:
        update = entity.trip_update
        if not entity.HasField("trip_update") or update.trip.trip_id != trip_id:
            continue
        for one in update.stop_time_update:
            if one.stop_id != stop_id:
                continue
            event = one.arrival if one.HasField("arrival") else one.departure
            if event.time:
                return int(event.time) * 1000
    return None


class BusLive:
    """Live departure times from BC Transit; ``feed`` and ``download`` are the network calls."""

    def __init__(
        self,
        cache_dir: Path,
        *,
        feed: Callable[[], bytes] = _fetch_feed,
        download: Callable[[Path], None] = _download_static,
        vehicles: Callable[[], bytes] = _fetch_vehicles,
    ) -> None:
        """``cache_dir`` holds the static zip and the timetable store built from it.

        ``feed`` (trip updates), ``vehicles`` (positions) and ``download`` (the export) are the
        network calls.
        """
        self._zip = cache_dir / "bctransit-gtfs.zip"
        self._store = cache_dir / "bctransit-timetable.sqlite"
        self._feed, self._download, self._vehicles = feed, download, vehicles
        self._table: timetable.Timetable | None = None
        self._routes: dict[str, str] = {}
        self._refreshing = threading.Lock()  # held while a background download or build runs
        self._guard = threading.Lock()

    # --- the timetable ------------------------------------------------------------------

    def table(self) -> timetable.Timetable:
        """The timetable store for the newest export.

        Only a first download and a first build are waited for. A day-old export still names the
        trips, so it answers while the new one downloads and is indexed in the background (inline,
        a bus answer once took 64 s); the next call reads the new store. Errors propagate.
        """
        if not self._zip.exists():
            self._refresh()
        elif not self._fresh() and self._refreshing.acquire(blocking=False):
            threading.Thread(target=self._refresh_then_release, daemon=True).start()
        stamp = str(self._zip.stat().st_mtime_ns)
        with self._guard:
            held = self._table
        if held is not None and held.source == stamp:
            return held
        if held is None and self._store.exists():
            held = self._open()
            if held.source == stamp:
                return held
        if held is None:
            return self._build(stamp)
        if self._refreshing.acquire(blocking=False):
            threading.Thread(target=self._build_then_release, args=(stamp,), daemon=True).start()
        return held

    def _open(self) -> timetable.Timetable:
        opened = timetable.Timetable(self._store)
        with self._guard:
            self._table, self._routes = opened, {}
        return opened

    def _build(self, stamp: str) -> timetable.Timetable:
        """Index the export now; used when no store can answer yet."""
        with self._guard:
            held = self._table
        if held is not None and held.source == stamp:
            return held
        timetable.build(self._zip, self._store)
        return self._open()

    def _build_then_release(self, stamp: str) -> None:
        try:
            self._build(stamp)
        except Exception:  # noqa: BLE001 - the old store keeps answering.
            LOGGER.warning("bus_live: could not index the new export", exc_info=True)
        finally:
            self._refreshing.release()

    def _routes_of(self, table: timetable.Timetable) -> dict[str, str]:
        with self._guard:
            if not self._routes:
                self._routes = table.route_numbers()
            return self._routes

    # --- the realtime feed --------------------------------------------------------------

    def snapshot(self) -> gtfs_realtime_pb2.FeedMessage:
        """The realtime feed, downloaded and parsed once, for lookups to share."""
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(self._feed())
        return feed

    def timed(
        self,
        route: str,
        board: tuple[float, float],
        alight: tuple[float, float],
        around_ms: int,
        feed: gtfs_realtime_pb2.FeedMessage | None = None,
    ) -> timetable.Timed | None:
        """The trip a Google bus leg is, with the feed's times at its stops (ADR 0342).

        ``feed`` is the answer's one :meth:`snapshot`; without it the trip has its scheduled
        times only. None when no trip fits. Errors propagate.
        """
        table = self.table()
        found = table.match_leg(
            route, board, alight, around_ms,
            None if feed is None else lambda trip, stop: live_departure(feed, trip, stop),
        )
        if found is None:
            return None
        return timetable.Timed(
            found,
            None if feed is None else live_departure(
                feed, found.board.trip_id, found.board.stop_id,
            ),
            None if feed is None else live_arrival(
                feed, found.alight.trip_id, found.alight.stop_id,
            ),
        )

    def following(
        self, after: timetable.Timed, feed: gtfs_realtime_pb2.FeedMessage | None = None,
    ) -> timetable.Timed | None:
        """The next trip of the same route, from the same stop, in the same direction.

        It is read to the same alight stop; None when no later trip calls at both. Errors
        propagate.
        """
        table, board = self.table(), after.match.board
        run, day = board.trip_id, board.day
        for _ in range(_NEXT_TRIPS_TRIED):
            nxt = table.next_trip_after(run, day, board.stop_id)
            if nxt is None:
                return None
            end = next(
                (c for c in table.calls_after(nxt.trip_id, nxt.day, nxt.seq)
                 if c.stop_id == after.match.alight.stop_id),
                None,
            )
            if end is not None:
                return timetable.Timed(
                    timetable.Match(nxt, end),
                    None if feed is None else live_departure(feed, nxt.trip_id, nxt.stop_id),
                    None if feed is None else live_arrival(feed, nxt.trip_id, end.stop_id),
                )
            run, day = nxt.trip_id, nxt.day
        return None

    def last_gone(self, after: timetable.Timed, now_ms: int) -> timetable.Call | None:
        """The last bus of this route, stop and direction that had left by ``now_ms``, or None."""
        table, board = self.table(), after.match.board
        gone = [
            c for c in table.calls_at(board.stop_id, now_ms - 24 * 3_600_000, now_ms, board.route)
            if c.direction == board.direction
        ]
        return gone[-1] if gone else None

    def departure(
        self,
        route: str,
        stop: tuple[float, float],
        around_ms: int,
        trip_id: str | None = None,
        feed: gtfs_realtime_pb2.FeedMessage | None = None,
    ) -> tuple[str, int] | None:
        """``(trip_id, live departure ms)`` of the bus on ``route`` at ``stop``, or None.

        With ``trip_id`` (the trip the pin was matched to, ADR 0342) that trip alone is read: a
        trip the feed has stopped covering gives None. Without one, the trip whose timetable (or
        live) time is closest to ``around_ms``, and no more than ten minutes from it. ``feed`` is a
        :meth:`snapshot` shared by several lookups; without it the feed is downloaded for this
        one. Network and file errors propagate.
        """
        feed = feed if feed is not None else self.snapshot()
        if trip_id is not None:
            table = self.table()
            for near, _ in table.stops_near(stop, STOP_RADIUS_M):
                if (at := live_departure(feed, trip_id, near.stop_id)) is not None:
                    return trip_id, at
            return None
        return _closest(self._stop_times(route, stop, around_ms, feed))

    def next_departure(
        self,
        route: str,
        stop: tuple[float, float],
        around_ms: int,
        trip_id: str | None = None,
    ) -> tuple[str, int] | None:
        """``(trip_id, live departure ms)`` of the bus after the one :meth:`departure` would match.

        The bus now matched (or, when it is gone from the feed, ``around_ms``) is the line:
        the earliest bus on ``route`` at ``stop`` leaving after it. Errors propagate.
        """
        found = self._stop_times(route, stop, around_ms, self.snapshot())
        now = _closest([f for f in found if trip_id is None or f[1] == trip_id] or found)
        line = now[1] if now else around_ms
        later = [(live, trip) for _, trip, live in found if live > line]
        return (min(later)[1], min(later)[0]) if later else None

    def vehicles(self) -> list[Vehicle]:
        """Every bus in the VehiclePositions feed (ADR 0224), with its route number.

        The route is the static export's number for the trip, else the head of the feed's own
        ``route_id`` (``14-VIC`` is the 14). Network and feed errors propagate.
        """
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(self._vehicles())
        try:
            trips = self._routes_of(self.table())
        except Exception:  # noqa: BLE001 - the feed's own route id still names the route.
            trips = {}
        found: list[Vehicle] = []
        for entity in feed.entity:
            one = entity.vehicle
            if not (
                entity.HasField("vehicle") and one.HasField("trip") and one.HasField("position")
            ):
                continue
            trip = one.trip
            found.append(Vehicle(
                vehicle_id=one.vehicle.id or entity.id,
                trip_id=trip.trip_id,
                route=trips.get(trip.trip_id) or trip.route_id.split("-")[0],
                lat=one.position.latitude,
                lng=one.position.longitude,
                at_ms=(one.timestamp or feed.header.timestamp) * 1000,
                stop_id=one.stop_id or None,
                stop_seq=(
                    one.current_stop_sequence if one.HasField("current_stop_sequence") else None
                ),
                status=one.current_status if one.HasField("current_status") else None,
                start_date=trip.start_date or None,
            ))
        return found

    def stop_name(self, stop_id: str) -> str | None:
        """The name of a stop in the timetable, once it has been indexed; else None."""
        if self._table is None:
            return None
        return self._table.stop_name(stop_id) or None

    def trip_stops(self, trip_id: str, service_date: str | None = None) -> list[StopTime]:
        """The stops of a trip in order, with their scheduled times on ``service_date``.

        ``service_date`` is ``YYYYMMDD`` (the feed's ``start_date``); today when None. Errors
        propagate.
        """
        table = self.table()
        day = (
            datetime.strptime(service_date, "%Y%m%d").date()  # noqa: DTZ007 - a date only.
            if service_date else datetime.now(table.zone).date()
        )
        return table.trip_stops(trip_id, day)

    @staticmethod
    def predicted(feed: gtfs_realtime_pb2.FeedMessage, trip_id: str) -> dict[str, int]:
        """The live arrival (epoch ms) at each stop of ``trip_id`` that ``feed`` predicts."""
        out: dict[str, int] = {}
        for entity in feed.entity:
            update = entity.trip_update
            if not entity.HasField("trip_update") or update.trip.trip_id != trip_id:
                continue
            for one in update.stop_time_update:
                event = one.arrival if one.arrival.time else one.departure
                if event.time:
                    out[one.stop_id] = event.time * 1000
        return out

    def _stop_times(
        self,
        route: str,
        stop: tuple[float, float],
        around_ms: int,
        feed: gtfs_realtime_pb2.FeedMessage,
    ) -> list[tuple[int, str, int]]:
        """Each bus on ``route`` at ``stop`` as ``(distance from around_ms, trip, live ms)``."""
        table = self.table()
        trips = self._routes_of(table)
        near = {s.stop_id for s, _ in table.stops_near(stop, STOP_RADIUS_M)}
        found: list[tuple[int, str, int]] = []
        for entity in feed.entity:
            update = entity.trip_update
            if not entity.HasField("trip_update") or trips.get(update.trip.trip_id) != route:
                continue
            for one in update.stop_time_update:
                event = one.departure if one.HasField("departure") else one.arrival
                if one.stop_id not in near or not event.time:
                    continue
                live = event.time * 1000
                off = min(abs(live - event.delay * 1000 - around_ms), abs(live - around_ms))
                found.append((off, update.trip.trip_id, live))
        return found

    # --- the export ---------------------------------------------------------------------

    def _refresh(self) -> None:
        """Download the export over the old copy; with no copy at all, a failure raises."""
        self._zip.parent.mkdir(parents=True, exist_ok=True)
        part = self._zip.with_suffix(".part")
        try:
            self._download(part)
            part.replace(self._zip)
        except Exception:
            part.unlink(missing_ok=True)
            if not self._zip.exists():
                raise
            # An old copy still names the trips; its age restarts, so a day without the
            # export is the price of a failed download, not a retry every refresh.
            self._zip.touch()

    def _refresh_then_release(self) -> None:
        try:
            self._refresh()
            self._build(str(self._zip.stat().st_mtime_ns))
        except Exception:  # noqa: BLE001 - the old copy keeps answering.
            LOGGER.warning("bus_live: could not refresh the export", exc_info=True)
        finally:
            self._refreshing.release()

    def _fresh(self) -> bool:
        return self._zip.exists() and time.time() - self._zip.stat().st_mtime < STATIC_MAX_AGE_S


def _closest(found: list[tuple[int, str, int]]) -> tuple[str, int] | None:
    """The matched bus: the closest in time, if it is within the window."""
    if not found:
        return None
    off, best, live = min(found)
    return (best, live) if off <= MATCH_WINDOW_MS else None


__all__ = ["BusLive", "live_arrival", "live_departure"]
