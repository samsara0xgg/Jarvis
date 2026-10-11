"""BC Transit's live departures for a pinned trip (ADR 0200).

Google plans the trip; this reads what the bus is actually doing. The realtime feed
(GTFS-realtime ``TripUpdates``, about 30 s fresh, no key) says per trip and stop when the bus will
leave and how far off its timetable that is. Its trips and stops are named by the static GTFS
export (a zip of about 16 MB), so that is kept under the runtime root and fetched at most once a
day; only the two small lookups a match needs (trip -> route number, stop -> position) are built,
once, on the first refresh. Google and BC Transit spell stop names differently, so a stop is found
by position: every GTFS stop within ``STOP_RADIUS_M`` of where Google put it.

A followed ride (ADR 0224) reads the same export and the VehiclePositions feed
(``vehicleupdates.pb``, about 13 KB): :meth:`BusLive.vehicles` for the buses,
:meth:`BusLive.trip_stops` for one trip's stops with their scheduled times and
:meth:`BusLive.predicted` for the live arrivals of a trip.
"""

from __future__ import annotations

import csv
import io
import shutil
import sys
import time
import urllib.request
import zipfile
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final
from zoneinfo import ZoneInfo

from google.transit import gtfs_realtime_pb2

from jarvis.shared.geo import metres
from jarvis.state.ride_progress import StopTime, Vehicle

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

FEED_URL: Final = "https://bct.tmix.se/gtfs-realtime/tripupdates.pb?operatorIds=48"
VEHICLES_URL: Final = "https://bct.tmix.se/gtfs-realtime/vehicleupdates.pb?operatorIds=48"
STATIC_URL: Final = "https://bct.tmix.se/Tmix.Cap.TdExport.WebApi/gtfs/?operatorIds=48"
STATIC_MAX_AGE_S: Final = 24 * 3600
STOP_RADIUS_M: Final = 40.0
MATCH_WINDOW_MS: Final = 10 * 60_000
_FEED_TIMEOUT_S: Final = 10.0
_STATIC_TIMEOUT_S: Final = 120.0
_ZONE: Final = ZoneInfo("America/Vancouver")
_TRIPS_KEPT: Final = 20


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
        """``cache_dir`` holds the static zip.

        ``feed`` (trip updates), ``vehicles`` (positions) and ``download`` (the export) are the
        network calls.
        """
        self._zip = cache_dir / "bctransit-gtfs.zip"
        self._feed, self._download, self._vehicles = feed, download, vehicles
        self._trips: dict[str, str] | None = None
        self._stops: dict[str, tuple[float, float]] = {}
        self._names: dict[str, str] = {}
        self._trip_cache: dict[tuple[str, str | None], list[StopTime]] = {}

    def departure(
        self,
        route: str,
        stop: tuple[float, float],
        around_ms: int,
        trip_id: str | None = None,
        feed: gtfs_realtime_pb2.FeedMessage | None = None,
    ) -> tuple[str, int] | None:
        """``(trip_id, live departure ms)`` of the bus on ``route`` at ``stop``, or None.

        With ``trip_id`` (the bus matched last time) that trip is looked for first; otherwise,
        or when it is gone from the feed, the trip whose timetable (or live) time is closest to
        ``around_ms``, and no more than ten minutes from it. ``feed`` is a :meth:`snapshot`
        shared by several lookups; without it the feed is downloaded for this one. Network and
        file errors propagate.
        """
        return _closest(self._stop_times(route, stop, around_ms, feed), trip_id)

    def snapshot(self) -> gtfs_realtime_pb2.FeedMessage:
        """The realtime feed, downloaded and parsed once, for lookups to share."""
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(self._feed())
        return feed

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
        found = self._stop_times(route, stop, around_ms)
        now = _closest(found, trip_id)
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
            trips: dict[str, str] = self._lookups()
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
        """The name of a stop in the static export, once it has been read; else None."""
        return self._names.get(stop_id) or None

    def trip_stops(self, trip_id: str, service_date: str | None = None) -> list[StopTime]:
        """The stops of a trip in order, with their scheduled times on ``service_date``.

        ``service_date`` is ``YYYYMMDD`` (the feed's ``start_date``); today when None. The
        timetable is read from the static export by one pass over its stop times, and the last
        few trips are kept. Errors propagate.
        """
        key = (trip_id, service_date)
        if key in self._trip_cache:
            return self._trip_cache[key]
        self._lookups()
        day = (
            datetime.strptime(service_date, "%Y%m%d").replace(tzinfo=_ZONE)
            if service_date
            else datetime.now(_ZONE).replace(hour=0, minute=0, second=0, microsecond=0)
        )
        found: list[StopTime] = []
        with zipfile.ZipFile(self._zip) as archive:
            for row in _rows(archive, "stop_times.txt"):
                if row["trip_id"] != trip_id or row["stop_id"] not in self._stops:
                    continue
                clock = row["arrival_time"] or row["departure_time"]
                h, m, sec = (int(part) for part in clock.split(":"))
                at = day + timedelta(hours=h, minutes=m, seconds=sec)
                lat, lng = self._stops[row["stop_id"]]
                found.append(StopTime(
                    int(row["stop_sequence"]), row["stop_id"], self._names[row["stop_id"]],
                    lat, lng, int(at.timestamp() * 1000),
                ))
        found.sort(key=lambda s: s.seq)
        if len(self._trip_cache) >= _TRIPS_KEPT:
            self._trip_cache.clear()
        self._trip_cache[key] = found
        return found

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
        feed: gtfs_realtime_pb2.FeedMessage | None = None,
    ) -> list[tuple[int, str, int]]:
        """Each bus on ``route`` at ``stop`` as ``(distance from around_ms, trip, live ms)``."""
        trips = self._lookups()
        near = {sid for sid, at in self._stops.items() if metres(stop, at) <= STOP_RADIUS_M}
        feed = feed if feed is not None else self.snapshot()
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

    def _lookups(self) -> dict[str, str]:
        """The trip -> route number map, building it (and the stop positions) on first use."""
        if self._trips is not None and self._fresh():
            return self._trips
        if not self._fresh():
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
        with zipfile.ZipFile(self._zip) as archive:
            routes = {
                row["route_id"]: sys.intern(row["route_short_name"].strip())
                for row in _rows(archive, "routes.txt")
            }
            self._trips = {
                row["trip_id"]: routes.get(row["route_id"], "")
                for row in _rows(archive, "trips.txt")
            }
            self._stops = {}
            self._names = {}
            for row in _rows(archive, "stops.txt"):
                self._stops[row["stop_id"]] = (float(row["stop_lat"]), float(row["stop_lon"]))
                self._names[row["stop_id"]] = (row.get("stop_name") or "").strip()
        return self._trips

    def _fresh(self) -> bool:
        return self._zip.exists() and time.time() - self._zip.stat().st_mtime < STATIC_MAX_AGE_S


def _closest(found: list[tuple[int, str, int]], trip_id: str | None) -> tuple[str, int] | None:
    """The matched bus: ``trip_id`` if the feed still has it, else the closest within the window."""
    if not found:
        return None
    off, best, live = min((-1 if trip == trip_id else off, trip, live) for off, trip, live in found)
    return (best, live) if off <= MATCH_WINDOW_MS else None


def _rows(archive: zipfile.ZipFile, name: str) -> csv.DictReader[str]:
    """One CSV of the export; BC Transit's files start with a UTF-8 byte-order mark."""
    return csv.DictReader(io.TextIOWrapper(archive.open(name), encoding="utf-8-sig", newline=""))
