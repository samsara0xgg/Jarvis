"""BC Transit's live departures for a pinned trip (ADR 0200).

Google plans the trip; this reads what the bus is actually doing. The realtime feed
(GTFS-realtime ``TripUpdates``, about 30 s fresh, no key) says per trip and stop when the bus will
leave and how far off its timetable that is. Its trips and stops are named by the static GTFS
export (a zip of about 16 MB), so that is kept under the runtime root and fetched at most once a
day; only the two small lookups a match needs (trip -> route number, stop -> position) are built,
once, on the first refresh. Google and BC Transit spell stop names differently, so a stop is found
by position: every GTFS stop within ``STOP_RADIUS_M`` of where Google put it.
"""

from __future__ import annotations

import csv
import io
import math
import shutil
import sys
import time
import urllib.request
import zipfile
from typing import TYPE_CHECKING, Final

from google.transit import gtfs_realtime_pb2

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

FEED_URL: Final = "https://bct.tmix.se/gtfs-realtime/tripupdates.pb?operatorIds=48"
STATIC_URL: Final = "https://bct.tmix.se/Tmix.Cap.TdExport.WebApi/gtfs/?operatorIds=48"
STATIC_MAX_AGE_S: Final = 24 * 3600
STOP_RADIUS_M: Final = 40.0
MATCH_WINDOW_MS: Final = 10 * 60_000
_FEED_TIMEOUT_S: Final = 10.0
_STATIC_TIMEOUT_S: Final = 120.0
_EARTH_M: Final = 6_371_000.0


def _fetch_feed() -> bytes:
    with urllib.request.urlopen(FEED_URL, timeout=_FEED_TIMEOUT_S) as reply:  # noqa: S310 - fixed https URL.
        data: bytes = reply.read()
    return data


def _download_static(dest: Path) -> None:
    reply = urllib.request.urlopen(STATIC_URL, timeout=_STATIC_TIMEOUT_S)  # noqa: S310
    with reply, dest.open("wb") as out:
        shutil.copyfileobj(reply, out)


def _metres(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance in metres; at 40 m the flat-earth shortcut would do, this is exact."""
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    h = math.sin((p2 - p1) / 2) ** 2 + (
        math.cos(p1) * math.cos(p2) * math.sin(math.radians(b[1] - a[1]) / 2) ** 2
    )
    return 2 * _EARTH_M * math.asin(math.sqrt(h))


class BusLive:
    """Live departure times from BC Transit; ``feed`` and ``download`` are the network calls."""

    def __init__(
        self,
        cache_dir: Path,
        *,
        feed: Callable[[], bytes] = _fetch_feed,
        download: Callable[[Path], None] = _download_static,
    ) -> None:
        """``cache_dir`` holds the static zip; ``feed`` and ``download`` are the network calls."""
        self._zip = cache_dir / "bctransit-gtfs.zip"
        self._feed, self._download = feed, download
        self._trips: dict[str, str] | None = None
        self._stops: dict[str, tuple[float, float]] = {}

    def departure(
        self,
        route: str,
        stop: tuple[float, float],
        around_ms: int,
        trip_id: str | None = None,
    ) -> tuple[str, int] | None:
        """``(trip_id, live departure ms)`` of the bus on ``route`` at ``stop``, or None.

        With ``trip_id`` (the bus matched last time) that trip is looked for first; otherwise,
        or when it is gone from the feed, the trip whose timetable (or live) time is closest to
        ``around_ms``, and no more than ten minutes from it. Network and file errors propagate.
        """
        return _closest(self._stop_times(route, stop, around_ms), trip_id)

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

    def _stop_times(
        self, route: str, stop: tuple[float, float], around_ms: int,
    ) -> list[tuple[int, str, int]]:
        """Each bus on ``route`` at ``stop`` as ``(distance from around_ms, trip, live ms)``."""
        trips = self._lookups()
        near = {sid for sid, at in self._stops.items() if _metres(stop, at) <= STOP_RADIUS_M}
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(self._feed())
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
            self._stops = {
                row["stop_id"]: (float(row["stop_lat"]), float(row["stop_lon"]))
                for row in _rows(archive, "stops.txt")
            }
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
