"""BC Transit's timetable, indexed once from the static GTFS export into one SQLite file (ADR 0342).

:func:`build` reads the export's zip into a temporary file and renames it over the store, so a
reader sees the old store or the new one, never a half. One builder works at a time, across threads
and processes (:func:`exclusive`); a builder that waited finds the store the other made and leaves
it. :class:`Timetable` opens the store and answers: the stops near a point or by code, the buses
calling at a stop in a window, a trip's stops in order, the trips of a route running on a service
day, the next trip after a given one.

A stop time may read past 24:00:00; it belongs to the service day the trip started on, so a call is
always dated by its service day and placed on the clock by that day's start (noon minus twelve hours
in the agency's zone, which is right across a clock change). Which service days run comes from
``calendar.txt`` and ``calendar_dates.txt``; an export with neither runs every trip every day.
Columns an export lacks (a service id, a direction, a headsign) read as empty.
"""

from __future__ import annotations

import contextlib
import csv
import fcntl
import io
import math
import os
import sqlite3
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final
from zoneinfo import ZoneInfo

from jarvis.shared.geo import metres
from jarvis.state.ride_progress import StopTime

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

DEFAULT_ZONE: Final = "America/Vancouver"
_DAY_S: Final = 86_400
_M_PER_DEG_LAT: Final = 111_320.0
_WEEKDAYS: Final = (
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
)
_SCHEMA: Final = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE stops (
    stop_id TEXT PRIMARY KEY, code TEXT NOT NULL, name TEXT NOT NULL,
    lat REAL NOT NULL, lng REAL NOT NULL);
CREATE TABLE routes (route_id TEXT PRIMARY KEY, short_name TEXT NOT NULL);
CREATE TABLE trips (
    trip_id TEXT PRIMARY KEY, route_id TEXT NOT NULL, service_id TEXT NOT NULL,
    direction INTEGER NOT NULL, headsign TEXT NOT NULL);
CREATE TABLE stop_times (
    trip_id TEXT NOT NULL, seq INTEGER NOT NULL, stop_id TEXT NOT NULL,
    arr_s INTEGER NOT NULL, dep_s INTEGER NOT NULL, PRIMARY KEY (trip_id, seq)) WITHOUT ROWID;
CREATE TABLE calendar (
    service_id TEXT PRIMARY KEY, days TEXT NOT NULL, first TEXT NOT NULL, last TEXT NOT NULL);
CREATE TABLE calendar_dates (service_id TEXT NOT NULL, day TEXT NOT NULL, added INTEGER NOT NULL);
CREATE INDEX stops_place ON stops (lat, lng);
CREATE INDEX stops_code ON stops (code);
CREATE INDEX trips_route ON trips (route_id, service_id);
CREATE INDEX stop_times_at ON stop_times (stop_id, dep_s);
CREATE INDEX calendar_dates_on ON calendar_dates (day);
"""


@dataclass(frozen=True)
class Stop:
    """One stop of the export."""

    stop_id: str
    code: str
    name: str
    lat: float
    lng: float


@dataclass(frozen=True)
class Call:
    """One bus calling at one stop on one service day, placed on the clock (epoch ms)."""

    trip_id: str
    route: str
    headsign: str
    direction: int
    stop_id: str
    seq: int
    arrive_ms: int
    depart_ms: int
    day: date


@dataclass(frozen=True)
class TripRun:
    """One trip of a route on a service day: where it starts and when."""

    trip_id: str
    headsign: str
    direction: int
    first_stop_id: str
    first_depart_ms: int
    day: date


@dataclass(frozen=True)
class Match:
    """The trip a Google bus leg is: its call at the board stop and later at the alight stop."""

    board: Call
    alight: Call


@dataclass(frozen=True)
class Timed:
    """A matched leg with the realtime feed's times at its two stops, when it has them.

    A stop the feed does not predict takes the delay of the nearest earlier stop it does (the
    runtime's reading of the feed); either way the time is the feed's and the leg is ``live``.
    """

    match: Match
    board_live_ms: int | None = None
    alight_live_ms: int | None = None

    @property
    def depart_ms(self) -> int:
        """When the bus leaves the board stop: the live prediction, else the schedule."""
        return self.board_live_ms if self.board_live_ms is not None else self.match.board.depart_ms

    @property
    def arrive_ms(self) -> int:
        """When it reaches the alight stop: the live prediction, else the schedule."""
        if self.alight_live_ms is not None:
            return self.alight_live_ms
        return self.match.alight.arrive_ms

    @property
    def source(self) -> str:
        """``live`` when the board time is a prediction, else ``scheduled``."""
        return "live" if self.board_live_ms is not None else "scheduled"


STOP_RADIUS_M: Final = 40.0
"""A Google stop and the export's are the same stop when they are no farther apart (ADR 0342)."""
MATCH_WINDOW_MS: Final = 10 * 60_000
LIVE_SLACK_MS: Final = 90 * 60_000
"""How late a bus can run and still be that bus.

The timetable is read this far from Google's time, and a prediction the feed does not date counts
for a call only this far from the call's scheduled time.
"""


def _seconds(clock: str) -> int | None:
    """``H:MM:SS`` (hours may pass 24) as seconds of the service day; None when blank."""
    parts = clock.strip().split(":")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):  # noqa: PLR2004 - H:M:S
        return None
    hours, minutes, seconds = (int(p) for p in parts)
    return hours * 3600 + minutes * 60 + seconds


def _rows(archive: zipfile.ZipFile, name: str) -> Iterator[dict[str, str]]:
    """One CSV of the export; BC Transit's files start with a UTF-8 byte-order mark."""
    if name not in archive.namelist():
        return
    with archive.open(name) as raw:
        yield from csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))


def _zone_of(archive: zipfile.ZipFile) -> str:
    for row in _rows(archive, "agency.txt"):
        if zone := (row.get("agency_timezone") or "").strip():
            return zone
    return DEFAULT_ZONE


def _stop_time_rows(archive: zipfile.ZipFile) -> Iterator[tuple[str, int, str, int, int]]:
    for row in _rows(archive, "stop_times.txt"):
        arrive, depart = _seconds(row["arrival_time"]), _seconds(row["departure_time"])
        if arrive is None and depart is None:
            continue
        yield (
            row["trip_id"], int(row["stop_sequence"]), row["stop_id"],
            arrive if arrive is not None else depart or 0,
            depart if depart is not None else arrive or 0,
        )


@contextlib.contextmanager
def exclusive(lock: Path) -> Iterator[None]:
    """Hold the lock file ``lock``, made if need be: one holder at a time, in any thread or process.

    ``flock`` belongs to the open file, so two callers in one process wait for each other as two
    processes do. Closing the file lets go, whether the holder finished or died.
    """
    with lock.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def scratch(final: Path, suffix: str) -> Path:
    """A new empty file beside ``final`` for one writer to fill and rename over it.

    Call it holding :func:`exclusive`: the files an earlier writer left when it was killed go.
    """
    for stale in final.parent.glob(f"{final.name}.*{suffix}"):
        stale.unlink(missing_ok=True)
    handle, name = tempfile.mkstemp(dir=final.parent, prefix=f"{final.name}.", suffix=suffix)
    os.close(handle)
    return Path(name)


def _built_from(source: Path, dest: Path) -> bool:
    """Whether ``dest`` is a readable store made from the export ``source`` as it is now."""
    try:
        with contextlib.closing(sqlite3.connect(f"file:{dest}?mode=ro", uri=True)) as db:
            row = db.execute("SELECT value FROM meta WHERE key = 'source'").fetchone()
    except sqlite3.Error:
        return False
    return row is not None and str(row[0]) == str(source.stat().st_mtime_ns)


def build(source: Path, dest: Path) -> None:
    """Index the export zip ``source`` into the store ``dest``, replacing it whole.

    One builder at a time (the lock file beside ``dest``); one that waited finds the store made
    from this export and leaves it. The store is written to a file of the builder's own and
    renamed over ``dest``. Errors propagate and leave ``dest`` as it was.
    """
    with exclusive(dest.with_name(dest.name + ".lock")):
        if _built_from(source, dest):
            return
        part = scratch(dest, ".building")
        try:
            with (
                contextlib.closing(sqlite3.connect(part)) as db, zipfile.ZipFile(source) as archive,
            ):
                db.executescript(_SCHEMA)
                db.execute("INSERT INTO meta VALUES ('zone', ?)", (_zone_of(archive),))
                db.execute(
                    "INSERT INTO meta VALUES ('source', ?)", (str(source.stat().st_mtime_ns),),
                )
                _fill(db, archive)
                db.commit()
            part.replace(dest)
        finally:
            part.unlink(missing_ok=True)


def _fill(db: sqlite3.Connection, archive: zipfile.ZipFile) -> None:
    db.executemany(
        "INSERT OR REPLACE INTO stops VALUES (?, ?, ?, ?, ?)",
        (
            (
                r["stop_id"], (r.get("stop_code") or "").strip(),
                (r.get("stop_name") or "").strip(), float(r["stop_lat"]), float(r["stop_lon"]),
            )
            for r in _rows(archive, "stops.txt")
        ),
    )
    db.executemany(
        "INSERT OR REPLACE INTO routes VALUES (?, ?)",
        ((r["route_id"], r["route_short_name"].strip()) for r in _rows(archive, "routes.txt")),
    )
    db.executemany(
        "INSERT OR REPLACE INTO trips VALUES (?, ?, ?, ?, ?)",
        (
            (
                r["trip_id"], r["route_id"], (r.get("service_id") or "").strip(),
                int(r.get("direction_id") or 0), (r.get("trip_headsign") or "").strip(),
            )
            for r in _rows(archive, "trips.txt")
        ),
    )
    db.executemany(
        "INSERT OR REPLACE INTO stop_times VALUES (?, ?, ?, ?, ?)", _stop_time_rows(archive),
    )
    db.executemany(
        "INSERT OR REPLACE INTO calendar VALUES (?, ?, ?, ?)",
        (
            (
                r["service_id"].strip(),
                "".join(r.get(name, "0").strip() for name in _WEEKDAYS),
                r["start_date"].strip(), r["end_date"].strip(),
            )
            for r in _rows(archive, "calendar.txt")
        ),
    )
    db.executemany(
        "INSERT INTO calendar_dates VALUES (?, ?, ?)",
        (
            (r["service_id"].strip(), r["date"].strip(), int(r["exception_type"] == "1"))
            for r in _rows(archive, "calendar_dates.txt")
        ),
    )


class Timetable:
    """Reads one store file; each question opens its own short connection."""

    def __init__(self, path: Path) -> None:
        """``path`` is a store made by :func:`build`."""
        self._path = path
        with self._db() as db:
            zone = db.execute("SELECT value FROM meta WHERE key = 'zone'").fetchone()[0]
            self.zone = ZoneInfo(str(zone))
            #: The mtime (ns) of the export this store was built from.
            source = db.execute("SELECT value FROM meta WHERE key = 'source'").fetchone()[0]
            self.source = str(source)
            self._every_day = (
                db.execute("SELECT count(*) FROM calendar").fetchone()[0] == 0
                and db.execute("SELECT count(*) FROM calendar_dates").fetchone()[0] == 0
            )

    @contextlib.contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        with contextlib.closing(sqlite3.connect(f"file:{self._path}?mode=ro", uri=True)) as db:
            yield db

    # --- the clock ----------------------------------------------------------------------

    def day_start_ms(self, day: date) -> int:
        """When service day ``day`` starts: noon minus twelve hours, local."""
        noon = datetime.combine(day, time(12), tzinfo=self.zone).astimezone(UTC)
        return int((noon - timedelta(hours=12)).timestamp() * 1000)

    def _ms(self, day: date, seconds: int) -> int:
        return self.day_start_ms(day) + seconds * 1000

    def service_ids(self, day: date) -> frozenset[str]:
        """The service ids that run on ``day`` (calendar, then its added and removed dates)."""
        stamp = f"{day:%Y%m%d}"
        with self._db() as db:
            weekly = db.execute(
                "SELECT service_id, days FROM calendar WHERE first <= ? AND last >= ?",
                (stamp, stamp),
            ).fetchall()
            exceptions = db.execute(
                "SELECT service_id, added FROM calendar_dates WHERE day = ?", (stamp,),
            ).fetchall()
        running = {sid for sid, days in weekly if days[day.weekday()] == "1"}
        for sid, added in exceptions:
            (running.add if added else running.discard)(sid)
        return frozenset(running)

    def runs(self, service_id: str, day: date, running: frozenset[str] | None = None) -> bool:
        """Whether trips of ``service_id`` run on ``day``."""
        if self._every_day:
            return True
        return service_id in (running if running is not None else self.service_ids(day))

    # --- stops --------------------------------------------------------------------------

    @staticmethod
    def _stop(row: tuple[str, str, str, float, float]) -> Stop:
        return Stop(*row)

    def stop(self, stop_id: str) -> Stop | None:
        """The stop with that id, or None."""
        with self._db() as db:
            row = db.execute(
                "SELECT stop_id, code, name, lat, lng FROM stops WHERE stop_id = ?", (stop_id,),
            ).fetchone()
        return None if row is None else self._stop(row)

    def stop_name(self, stop_id: str) -> str | None:
        """A stop's name, or None."""
        found = self.stop(stop_id)
        return found.name if found else None

    def stops_by_code(self, code: str) -> list[Stop]:
        """The stops whose code (the number on the pole) is exactly ``code``."""
        with self._db() as db:
            rows = db.execute(
                "SELECT stop_id, code, name, lat, lng FROM stops WHERE code = ? AND code != ''",
                (code.strip(),),
            ).fetchall()
        return [self._stop(r) for r in rows]

    def all_stops(self) -> list[Stop]:
        """Every stop; for matching names, which no index serves."""
        with self._db() as db:
            rows = db.execute("SELECT stop_id, code, name, lat, lng FROM stops").fetchall()
        return [self._stop(r) for r in rows]

    def stops_near(self, point: tuple[float, float], radius_m: float) -> list[tuple[Stop, float]]:
        """Stops within ``radius_m`` of ``point`` as ``(stop, metres)``, nearest first."""
        lat, lng = point
        dlat = radius_m / _M_PER_DEG_LAT
        dlng = radius_m / (_M_PER_DEG_LAT * max(0.01, math.cos(math.radians(lat))))
        with self._db() as db:
            rows = db.execute(
                "SELECT stop_id, code, name, lat, lng FROM stops"
                " WHERE lat BETWEEN ? AND ? AND lng BETWEEN ? AND ?",
                (lat - dlat, lat + dlat, lng - dlng, lng + dlng),
            ).fetchall()
        found = [(s, metres(point, (s.lat, s.lng))) for s in map(self._stop, rows)]
        return sorted((f for f in found if f[1] <= radius_m), key=lambda f: f[1])

    def serves(self, stop_id: str) -> list[tuple[str, str]]:
        """The ``(route, headsign)`` pairs of the trips that call at a stop, in route order."""
        with self._db() as db:
            rows = db.execute(
                "SELECT DISTINCT r.short_name, t.headsign FROM stop_times st"
                " JOIN trips t ON t.trip_id = st.trip_id JOIN routes r ON r.route_id = t.route_id"
                " WHERE st.stop_id = ?",
                (stop_id,),
            ).fetchall()
        return sorted((str(r), str(h)) for r, h in rows)

    # --- trips --------------------------------------------------------------------------

    def route_of(self, trip_id: str) -> str | None:
        """A trip's route number, or None."""
        with self._db() as db:
            row = db.execute(
                "SELECT r.short_name FROM trips t JOIN routes r ON r.route_id = t.route_id"
                " WHERE t.trip_id = ?",
                (trip_id,),
            ).fetchone()
        return None if row is None else str(row[0])

    def route_numbers(self) -> dict[str, str]:
        """Every trip's route number, by trip id."""
        with self._db() as db:
            rows = db.execute(
                "SELECT t.trip_id, r.short_name FROM trips t"
                " JOIN routes r ON r.route_id = t.route_id",
            ).fetchall()
        return {str(t): sys.intern(str(r)) for t, r in rows}

    def headsign_of(self, trip_id: str) -> str:
        """A trip's headsign (where it is going), empty when the export has none."""
        with self._db() as db:
            row = db.execute(
                "SELECT headsign FROM trips WHERE trip_id = ?", (trip_id,),
            ).fetchone()
        return "" if row is None else str(row[0])

    def trip_stops(self, trip_id: str, day: date) -> list[StopTime]:
        """A trip's stops in order, each with its scheduled time on service day ``day``."""
        with self._db() as db:
            rows = db.execute(
                "SELECT st.seq, st.stop_id, s.name, s.lat, s.lng, st.arr_s FROM stop_times st"
                " JOIN stops s ON s.stop_id = st.stop_id WHERE st.trip_id = ? ORDER BY st.seq",
                (trip_id,),
            ).fetchall()
        return [
            StopTime(seq, sid, name, lat, lng, self._ms(day, arr))
            for seq, sid, name, lat, lng, arr in rows
        ]

    def calls_after(self, trip_id: str, day: date, seq: int) -> list[Call]:
        """The calls of a trip after stop sequence ``seq``, in order."""
        return [c for c in self.trip_calls(trip_id, day) if c.seq > seq]

    def trip_calls(self, trip_id: str, day: date) -> list[Call]:
        """Every call of a trip on service day ``day``, in order."""
        with self._db() as db:
            rows = db.execute(
                "SELECT st.seq, st.stop_id, st.arr_s, st.dep_s, r.short_name, t.headsign,"
                " t.direction FROM stop_times st JOIN trips t ON t.trip_id = st.trip_id"
                " JOIN routes r ON r.route_id = t.route_id WHERE st.trip_id = ? ORDER BY st.seq",
                (trip_id,),
            ).fetchall()
        return [
            Call(trip_id, route, head, direction, sid, seq, self._ms(day, arr),
                 self._ms(day, dep), day)
            for seq, sid, arr, dep, route, head, direction in rows
        ]

    def calls_at(
        self, stop_id: str, start_ms: int, end_ms: int, route: str | None = None,
    ) -> list[Call]:
        """Buses calling at a stop with a departure in ``[start_ms, end_ms]``, earliest first.

        A trip's last stop is not one: nobody boards there. ``route`` limits it to that number
        (any case). Trips of the service day before and the day itself are both looked at, since
        a late trip runs past midnight.
        """
        first = datetime.fromtimestamp(start_ms / 1000, self.zone).date() - timedelta(days=1)
        last = datetime.fromtimestamp(end_ms / 1000, self.zone).date()
        found: list[Call] = []
        day = first
        while day <= last:
            found.extend(self._calls_on(stop_id, day, start_ms, end_ms, route))
            day += timedelta(days=1)
        return sorted(found, key=lambda c: (c.depart_ms, c.trip_id))

    def _calls_on(
        self, stop_id: str, day: date, start_ms: int, end_ms: int, route: str | None,
    ) -> list[Call]:
        base = self.day_start_ms(day)
        low, high = (start_ms - base) // 1000, (end_ms - base) // 1000
        if high < 0 or low > 2 * _DAY_S:
            return []
        sql = (
            "SELECT st.trip_id, st.seq, st.arr_s, st.dep_s, r.short_name, t.headsign, t.direction,"
            " t.service_id FROM stop_times st JOIN trips t ON t.trip_id = st.trip_id"
            " JOIN routes r ON r.route_id = t.route_id"
            " WHERE st.stop_id = ? AND st.dep_s BETWEEN ? AND ?"
            " AND EXISTS (SELECT 1 FROM stop_times nx WHERE nx.trip_id = st.trip_id"
            " AND nx.seq > st.seq)"
        )
        args: list[object] = [stop_id, low, high]
        if route:
            sql += " AND r.short_name = ? COLLATE NOCASE"
            args.append(route)
        with self._db() as db:
            rows = db.execute(sql, args).fetchall()
        running = None if self._every_day else self.service_ids(day)
        return [
            Call(tid, rt, head, direction, stop_id, seq, base + arr * 1000, base + dep * 1000, day)
            for tid, seq, arr, dep, rt, head, direction, sid in rows
            if self.runs(sid, day, running)
        ]

    def trips_of(self, route: str, day: date) -> list[TripRun]:
        """The trips of ``route`` running on service day ``day``, by when they start."""
        with self._db() as db:
            rows = db.execute(
                "SELECT t.trip_id, t.headsign, t.direction, t.service_id, st.stop_id, st.dep_s"
                " FROM trips t JOIN routes r ON r.route_id = t.route_id"
                " JOIN stop_times st ON st.trip_id = t.trip_id"
                " AND st.seq = (SELECT min(seq) FROM stop_times WHERE trip_id = t.trip_id)"
                " WHERE r.short_name = ? COLLATE NOCASE",
                (route,),
            ).fetchall()
        running = None if self._every_day else self.service_ids(day)
        return sorted(
            (
                TripRun(tid, head, direction, sid_stop, self._ms(day, dep), day)
                for tid, head, direction, service, sid_stop, dep in rows
                if self.runs(service, day, running)
            ),
            key=lambda t: t.first_depart_ms,
        )

    def call_near(self, trip_id: str, stop_id: str, around_ms: int) -> Call | None:
        """The call of ``trip_id`` at ``stop_id`` on the service day nearest ``around_ms`` in time.

        A trip runs every day under one id, so which day is meant is told by the time. None when
        the trip does not call at that stop.
        """
        first = datetime.fromtimestamp(around_ms / 1000, self.zone).date() - timedelta(days=1)
        best: Call | None = None
        for offset in range(3):
            day = first + timedelta(days=offset)
            call = next((c for c in self.trip_calls(trip_id, day) if c.stop_id == stop_id), None)
            if call is not None and (
                best is None or abs(call.depart_ms - around_ms) < abs(best.depart_ms - around_ms)
            ):
                best = call
        return best

    def next_trip_after(self, trip_id: str, day: date, stop_id: str) -> Call | None:
        """The next call at ``stop_id`` by a trip of the same route and direction.

        It is the earliest one leaving after ``trip_id`` does, on the service day ``day`` or the
        next; None when the trip does not call there or no later trip does.
        """
        here = next((c for c in self.trip_calls(trip_id, day) if c.stop_id == stop_id), None)
        if here is None:
            return None
        end = here.depart_ms + 2 * _DAY_S * 1000
        later = (
            c for c in self.calls_at(stop_id, here.depart_ms + 1, end, here.route)
            if c.direction == here.direction and c.trip_id != trip_id
        )
        return next(later, None)

    def match_leg(
        self,
        route: str,
        board: tuple[float, float],
        alight: tuple[float, float],
        around_ms: int,
        live: Callable[[Call], int | None] | None = None,
    ) -> Match | None:
        """The trip of ``route`` that is Google's bus leg, or None (ADR 0342).

        It stops within 40 m of ``board`` and, later on the same trip, within 40 m of ``alight``;
        of those trips the one whose departure at the board stop is closest to ``around_ms``, and
        no more than ten minutes from it. A trip is compared by its scheduled time and by its live
        one (``live(call)`` for the call on its service day, epoch ms or None), whichever is
        closer.
        """
        boards = [s.stop_id for s, _ in self.stops_near(board, STOP_RADIUS_M)]
        alights = {s.stop_id for s, _ in self.stops_near(alight, STOP_RADIUS_M)}
        best: tuple[int, int, Match] | None = None
        for stop_id in boards:
            for call in self.calls_at(
                stop_id, around_ms - LIVE_SLACK_MS, around_ms + LIVE_SLACK_MS, route,
            ):
                now_live = live(call) if live else None
                off = min(
                    abs(t - around_ms) for t in (call.depart_ms, now_live) if t is not None
                )
                if off > MATCH_WINDOW_MS or (best is not None and off >= best[0]):
                    continue
                end = next(
                    (c for c in self.calls_after(call.trip_id, call.day, call.seq)
                     if c.stop_id in alights),
                    None,
                )
                if end is not None:
                    best = (off, call.depart_ms, Match(call, end))
        return None if best is None else best[2]
