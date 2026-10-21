"""ADR 0342 — BC Transit's export is indexed once into a store that answers every timetable read.

Each test names what the store (or `BusLive` over it) says about a made-up export: which service
days run, where a late-night trip falls, which stops are near a point, which trip a Google leg is,
and that a new export replaces the store whole without the reader seeing a half.
"""

from __future__ import annotations

import threading
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest

from jarvis.runtime.bus_live import BusLive
from jarvis.state import timetable
from tests.support import gtfs

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

ZONE = ZoneInfo("America/Vancouver")
# Two stops of one street, one per direction, 30 m apart; a third a kilometre on; a fourth far off.
EAST = gtfs.StopSpec("E1", "Alder St at Birch Rd", 48.5000, -123.4000, "10001")
WEST = gtfs.StopSpec("W1", "Alder St at Birch Rd", 48.5000, -123.40040, "10002")
NEXT = gtfs.StopSpec("E2", "Cedar Ave at Elm Cres", 48.5090, -123.4000, "10003")
BACK = gtfs.StopSpec("W2", "Cedar Ave at Elm Cres", 48.5090, -123.40040, "10004")
FAR = gtfs.StopSpec("F1", "Far Rd", 48.6, -123.2, "10005")
STOPS = (EAST, WEST, NEXT, BACK, FAR)


def _local_ms(day: date, hhmm: str) -> int:
    hour, minute = map(int, hhmm.split(":"))
    return int(datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZONE).timestamp() * 1000)


def _store(tmp_path: Path, export: gtfs.Export) -> timetable.Timetable:
    zip_path, store = tmp_path / "export.zip", tmp_path / "store.sqlite"
    gtfs.write(zip_path, export)
    timetable.build(zip_path, store)
    return timetable.Timetable(store)


def _trips() -> tuple[gtfs.TripSpec, ...]:
    return (
        gtfs.TripSpec("out1", "7", (("E1", "9:00:00"), ("E2", "9:10:00")), direction=0,
                      headsign="Harbour"),
        gtfs.TripSpec("out2", "7", (("E1", "9:30:00"), ("E2", "9:40:00")), direction=0,
                      headsign="Harbour"),
        gtfs.TripSpec("back1", "7", (("W2", "8:52:00"), ("W1", "9:01:00")), direction=1,
                      headsign="Hill"),
        gtfs.TripSpec("late", "7", (("E1", "24:10:00"), ("E2", "24:20:00")), direction=0,
                      headsign="Harbour", service="WK"),
    )


def test_stops_near_a_point_come_nearest_first_with_their_distance(tmp_path: Path) -> None:
    """Stops within a radius come nearest first with metres; a pole code finds exactly its stop."""
    table = _store(tmp_path, gtfs.Export(STOPS, _trips()))
    found = table.stops_near((48.5000, -123.4001), 100)
    assert [s.stop_id for s, _ in found] == ["E1", "W1"]
    assert 5 < found[0][1] < 15
    assert [s.stop_id for s in table.stops_by_code("10003")] == ["E2"]
    assert table.stops_by_code("99999") == []


def test_only_service_days_that_run_have_trips(tmp_path: Path) -> None:
    """A weekday service has trips on Friday and none on Saturday."""
    friday, saturday = date(2026, 10, 9), date(2026, 10, 10)
    export = gtfs.Export(STOPS, _trips(), services=(gtfs.WEEKDAYS,))
    table = _store(tmp_path, export)
    assert len(table.trips_of("7", friday)) == 4
    assert table.trips_of("7", saturday) == []
    assert table.calls_at("E1", _local_ms(saturday, "8:00"), _local_ms(saturday, "12:00")) == []


def test_calendar_dates_add_and_remove_a_day(tmp_path: Path) -> None:
    """A removed date has no trips and an added Saturday has them."""
    holiday_monday, extra_saturday = date(2026, 10, 12), date(2026, 10, 10)
    export = gtfs.Export(
        STOPS, _trips(), services=(gtfs.WEEKDAYS,),
        dates=(("WK", "20261012", False), ("WK", "20261010", True)),
    )
    table = _store(tmp_path, export)
    assert table.trips_of("7", holiday_monday) == []
    assert len(table.trips_of("7", extra_saturday)) == 4


def test_a_trip_past_midnight_belongs_to_the_day_it_started(tmp_path: Path) -> None:
    """A 24:10 stop time is 00:10 on the clock and Friday's service day."""
    friday, saturday = date(2026, 10, 9), date(2026, 10, 10)
    table = _store(tmp_path, gtfs.Export(STOPS, _trips(), services=(gtfs.WEEKDAYS,)))
    # 00:10 on Saturday is the end of Friday's service day.
    after_midnight = table.calls_at("E1", _local_ms(saturday, "0:00"), _local_ms(saturday, "1:00"))
    assert [(c.trip_id, c.day) for c in after_midnight] == [("late", friday)]
    assert after_midnight[0].depart_ms == _local_ms(saturday, "0:10")


def test_a_trips_stops_come_in_order_with_their_times(tmp_path: Path) -> None:
    """A trip's stops come in sequence with their scheduled times on that day."""
    table = _store(tmp_path, gtfs.Export(STOPS, _trips()))
    stops = table.trip_stops("out2", date(2026, 10, 9))
    assert [(s.stop_id, s.name) for s in stops] == [
        ("E1", "Alder St at Birch Rd"), ("E2", "Cedar Ave at Elm Cres"),
    ]
    assert [s.sched_ms for s in stops] == [
        _local_ms(date(2026, 10, 9), "9:30"), _local_ms(date(2026, 10, 9), "9:40"),
    ]


def test_the_next_trip_is_the_same_route_stop_and_direction(tmp_path: Path) -> None:
    """The next trip after one is the same route and direction calling at the same stop."""
    table = _store(tmp_path, gtfs.Export(STOPS, _trips()))
    nxt = table.next_trip_after("out1", date(2026, 10, 9), "E1")
    assert nxt is not None
    assert (nxt.trip_id, nxt.direction) == ("out2", 0)
    # The trip going the other way at the next stop is not "the next trip" of an outbound one.
    last = table.next_trip_after("out2", date(2026, 10, 9), "E1")
    assert last is not None
    assert last.trip_id == "late"


def test_a_leg_matches_the_trip_whose_alight_stop_comes_after_its_board_stop(
    tmp_path: Path,
) -> None:
    """The other-way trip reaches W1 nearer Google's time, but only after the north stop."""
    table = _store(tmp_path, gtfs.Export(STOPS, _trips()))
    day = date(2026, 10, 9)
    board, alight = (48.5000, -123.4002), (48.5090, -123.4002)
    found = table.match_leg("7", board, alight, _local_ms(day, "9:01"))
    (reverse,) = table.calls_at("W1", _local_ms(day, "9:01"), _local_ms(day, "9:01"))
    assert reverse.trip_id == "back1"
    assert found is not None
    assert (found.board.trip_id, found.board.stop_id, found.alight.stop_id) == (
        "out1", "E1", "E2",
    )
    # Google's time ten minutes from the nearest trip finds nothing.
    assert table.match_leg("7", board, alight, _local_ms(day, "9:15")) is None


def test_a_late_bus_is_matched_by_its_live_time(tmp_path: Path) -> None:
    """A trip with no scheduled time near Google's matches by its predicted one."""
    table = _store(tmp_path, gtfs.Export(STOPS, _trips()))
    day = date(2026, 10, 9)
    board, alight = (48.5000, -123.4002), (48.5090, -123.4002)
    # Google says 9:50; no trip is scheduled then, but out1 is predicted 50 minutes late.
    around = _local_ms(day, "9:50")
    assert table.match_leg("7", board, alight, around) is None
    late = _local_ms(day, "9:50")
    found = table.match_leg(
        "7", board, alight, around, lambda trip, _stop: late if trip == "out1" else None,
    )
    assert found is not None
    assert found.board.trip_id == "out1"


def test_an_export_without_calendars_runs_every_trip_every_day(tmp_path: Path) -> None:
    """An export with no calendar files runs every trip on every day."""
    table = _store(tmp_path, gtfs.Export(STOPS, _trips(), calendars=False))
    assert len(table.trips_of("7", date(2026, 10, 10))) == 4


def test_the_store_places_times_in_the_exports_own_zone(tmp_path: Path) -> None:
    """Stop times are placed on the clock in the agency's zone."""
    table = _store(tmp_path, gtfs.Export(STOPS, _trips(), zone="America/Toronto"))
    (call,) = table.calls_at(
        "E1", int(datetime(2026, 10, 9, 13, 0, tzinfo=UTC).timestamp() * 1000),
        int(datetime(2026, 10, 9, 13, 5, tzinfo=UTC).timestamp() * 1000),
    )
    assert call.depart_ms == int(datetime(2026, 10, 9, 13, 0, tzinfo=UTC).timestamp() * 1000)


def _export_with(trips: tuple[gtfs.TripSpec, ...]) -> gtfs.Export:
    return gtfs.Export(STOPS, trips)


def test_a_new_export_replaces_the_store_in_the_background(tmp_path: Path) -> None:
    """The first store is waited for; a newer export is indexed behind a reader that answers."""
    cache = tmp_path / "cache"
    cache.mkdir()
    gtfs.write(cache / "bctransit-gtfs.zip", _export_with(_trips()[:1]))
    fetched = threading.Event()

    def download(part: Path) -> None:
        gtfs.write(part, _export_with(_trips()))
        fetched.set()

    live = BusLive(cache, download=download, feed=lambda: b"")
    day = date(2026, 10, 9)
    assert len(live.table().trips_of("7", day)) == 1  # built on the first call
    # The export goes stale: a read still answers from the old store while the new one is made.
    zip_path = cache / "bctransit-gtfs.zip"
    old = zip_path.stat().st_mtime - 2 * 86_400
    import os  # noqa: PLC0415 - one test sets a file's age.
    os.utime(zip_path, (old, old))
    assert len(live.table().trips_of("7", day)) == 1
    assert fetched.wait(5)
    deadline = threading.Event()
    for _ in range(100):
        if len(live.table().trips_of("7", day)) == 4:
            break
        deadline.wait(0.05)
    assert len(live.table().trips_of("7", day)) == 4


def test_a_failed_build_leaves_the_old_store_in_place(tmp_path: Path) -> None:
    """A broken export raises, the store keeps its old rows and no half-built file stays."""
    zip_path, store = tmp_path / "export.zip", tmp_path / "store.sqlite"
    gtfs.write(zip_path, _export_with(_trips()))
    timetable.build(zip_path, store)
    zip_path.write_bytes(b"not a zip")
    with pytest.raises(Exception):  # noqa: B017, PT011 - any broken export refuses the same way.
        timetable.build(zip_path, store)
    assert len(timetable.Timetable(store).trips_of("7", date(2026, 10, 9))) == 4
    assert not list(tmp_path.glob("*.building"))


# --- one builder at a time ----------------------------------------------------------------


def _long_export(trips: int) -> gtfs.Export:
    """One line of 40 stops and ``trips`` trips: a build long enough for builders to overlap."""
    stops = tuple(
        gtfs.StopSpec(f"S{i}", f"Stop {i}", 48.5 + i * 0.001, -123.4, f"{10000 + i}")
        for i in range(40)
    )
    runs = tuple(
        gtfs.TripSpec(
            f"T{n}", "7", tuple((f"S{i}", f"{8 + n % 12}:{i:02d}:00") for i in range(40)),
            headsign="Harbour",
        )
        for n in range(trips)
    )
    return gtfs.Export(stops, runs)


def _count_trips(reader: BusLive) -> Callable[[], object]:
    """What a caller needing the timetable does: take it and count the trips of line 7 on a day."""
    return lambda: len(reader.table().trips_of("7", date(2026, 10, 9)))


def _together(callers: list[Callable[[], object]]) -> list[object]:
    """Run every caller on a thread of its own, released at one instant; what each returned.

    An exception is returned in place of a result, as the caller would have met it.
    """
    start = threading.Barrier(len(callers))
    found: list[object] = [None] * len(callers)

    def run(slot: int) -> None:
        start.wait()
        try:
            found[slot] = callers[slot]()
        except BaseException as exc:  # noqa: BLE001 - the failure is the finding.
            found[slot] = exc

    threads = [threading.Thread(target=run, args=(n,)) for n in range(len(callers))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return found


@pytest.mark.parametrize("shared", [True, False], ids=["one reader", "a reader each"])
def test_callers_that_need_the_first_store_at_once_each_get_it_whole(
    tmp_path: Path, *, shared: bool,
) -> None:
    """Three callers ask for the timetable together before any store exists; each gets all of it.

    They are the daemon's threads (one reader) or two daemons on one folder (a reader each), and
    nothing half-built stays behind.
    """
    cache = tmp_path / "cache"
    cache.mkdir()
    gtfs.write(cache / "bctransit-gtfs.zip", _long_export(600))
    one = BusLive(cache, feed=lambda: b"", download=lambda _p: None)
    readers = [
        one if shared else BusLive(cache, feed=lambda: b"", download=lambda _p: None)
        for _ in range(3)
    ]
    assert _together([_count_trips(reader) for reader in readers]) == [600, 600, 600]
    assert not [p for p in cache.iterdir() if p.suffix in (".building", ".part")]


def test_callers_that_find_no_export_at_once_download_it_once(tmp_path: Path) -> None:
    """The first of three callers downloads the export; the others wait for it and use it."""
    cache = tmp_path / "cache"
    cache.mkdir()
    downloads: list[Path] = []

    def download(part: Path) -> None:
        downloads.append(part)
        gtfs.write(part, _long_export(60))

    readers = [BusLive(cache, feed=lambda: b"", download=download) for _ in range(3)]
    assert _together([_count_trips(reader) for reader in readers]) == [60, 60, 60]
    assert len(downloads) == 1
    assert not [p for p in cache.iterdir() if p.suffix in (".building", ".part")]


def test_an_export_that_has_its_store_is_not_indexed_again(tmp_path: Path) -> None:
    """A second build from the same export leaves the store as it is.

    A file a killed builder left is cleared by the next build, and a new export replaces the store.
    """
    zip_path, store = tmp_path / "export.zip", tmp_path / "store.sqlite"
    gtfs.write(zip_path, _export_with(_trips()))
    timetable.build(zip_path, store)
    built = store.stat()
    (tmp_path / "store.sqlite.1x2y.building").write_bytes(b"half a store")
    timetable.build(zip_path, store)
    again = store.stat()
    assert (again.st_ino, again.st_mtime_ns) == (built.st_ino, built.st_mtime_ns)
    gtfs.write(zip_path, _export_with(_trips()[:1]))
    timetable.build(zip_path, store)
    assert len(timetable.Timetable(store).trips_of("7", date(2026, 10, 9))) == 1
    assert not list(tmp_path.glob("*.building"))
