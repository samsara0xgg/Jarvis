"""ADR 0279 — his routines are counted from what he did and offered only once they held for weeks.

A real event log and memory.db in a tmp dir; home and school are kept through ``places.save``, the
visits are the phone's own ``phone.visit_observed`` rows, the rides go through ``rides.write_*``
and sleep is ``phone.health_observed``. The weeks are made up around (48.0000, -123.0000) and
planned for Tuesday 2026-11-17 in Vancouver: five Tuesdays before it carry phone data.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from jarvis.state import places, rides, routines
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.state.phone_day import HEALTH_EVENT
from tests.integration.test_places import stay

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

ZONE = ZoneInfo("America/Vancouver")
DAY = date(2026, 11, 17)  # a Tuesday
TUESDAYS = tuple(DAY - timedelta(weeks=n) for n in (5, 4, 3, 2, 1))
HOME, SCHOOL, CAFE = (48.0000, -123.0000), (48.0100, -123.0000), (48.0000, -123.0100)


def at(day: date, clock: str) -> int:
    """Epoch ms of ``clock`` ("HH:MM") local on ``day``."""
    hour, minute = map(int, clock.split(":"))
    return int(datetime.combine(day, time(hour, minute), ZONE).timestamp() * 1000)


def _world(tmp_path: Path) -> tuple[sqlite3.Connection, list[places.Place]]:
    memory = tmp_path / "memory.db"
    places.save(memory, name="Home", lat=HOME[0], lng=HOME[1], role="home", origin="user")
    places.save(
        memory, name="Maple College", lat=SCHOOL[0], lng=SCHOOL[1], role="school", origin="user"
    )
    return open_event_log(tmp_path / "events.db"), places.read_kept(memory)


def _school_day(conn: sqlite3.Connection, day: date, leave: str, *, cafe: bool = True) -> None:
    """Home overnight, a short glitch split at home, school from 08:50, the café, home again."""
    tag = day.isoformat()
    stay(conn, f"{tag}-h0", *HOME, at(day, "00:00") - 3_600_000, at(day, "06:00"))
    stay(conn, f"{tag}-h1", *HOME, at(day, "06:05"), at(day, leave))
    stay(conn, f"{tag}-s", *SCHOOL, at(day, "08:50"), at(day, "15:00"))
    if cafe:
        stay(conn, f"{tag}-c", *CAFE, at(day, "15:20"), at(day, "16:00"))
    stay(conn, f"{tag}-h2", *HOME, at(day, "16:30"), at(day, "23:00"))


def _ride(conn: sqlite3.Connection, ride_id: str, route: str, day: date, start: str) -> None:
    started = at(day, start)
    rides.write_start(conn, rides.Ride(ride_id, route, "tapped", started, board_stop="Alder St"))
    rides.write_alight(conn, ride_id, stop="Birch Ave", stop_id=None, seq=None, at_ms=started)
    rides.write_end(conn, ride_id, "left_vehicle", started + 20 * 60_000)


def _sleep(conn: sqlite3.Connection, uid: str, night: date, bed: str, minutes: int) -> None:
    start = at(night, bed) if bed >= "12:00" else at(night + timedelta(days=1), bed)
    payload = {
        "metric": "sleep_min",
        "start_ms": start,
        "end_ms": start + minutes * 60_000,
        "value": minutes,
    }
    emit_event(
        conn,
        type=HEALTH_EVENT,
        payload=payload,
        ts_epoch_ms=start + minutes * 60_000,
        event_uid=uid,
        ingestion_node="phone",
    )


def test_what_held_on_most_tuesdays_for_three_weeks_is_his_tuesday(tmp_path: Path) -> None:
    """Leaving home, school, the 99 and sleep held on Tuesdays; the café and the 7 did not count."""
    conn, kept = _world(tmp_path)
    leaves = ("08:10", "08:20", "08:15", "08:05")
    for day, leave in zip(TUESDAYS[:4], leaves, strict=True):
        _school_day(conn, day, leave)
    # the fifth Tuesday he stayed home: the phone saw the day, and school was missed
    stay(conn, "home-all-day", *HOME, at(TUESDAYS[4], "00:00"), at(TUESDAYS[4], "23:00"))
    for day in (TUESDAYS[0], TUESDAYS[2], TUESDAYS[4]):
        _ride(conn, f"ride-99-{day}", "99", day, "08:20")
    for day in TUESDAYS[1:3]:
        _ride(conn, f"ride-7-{day}", "7", day, "17:00")
    for index, (night, bed) in enumerate(
        zip(TUESDAYS[:4], ("23:30", "00:30", "23:50", "00:10"), strict=True)
    ):
        _sleep(conn, f"nap-{index}", night + timedelta(days=1), "14:00", 20)  # loses to the night
        _sleep(conn, f"night-{index}", night, bed, 420)
    # a Wednesday at school counts for Wednesdays only
    _school_day(conn, DAY - timedelta(days=6), "07:00")

    found = {r.kind: r for r in routines.for_day(conn, DAY, zone=ZONE, places=kept)}

    assert set(found) == {"leave_home", "other", "ride", "sleep"}  # the café is not kept
    out = found["leave_home"]
    assert (out.start, out.end, out.spread_min, out.seen, out.of) == (
        "08:12",
        "",
        5,
        4,
        5,
    )
    assert out.place == "Home"
    assert out.weekdays == (1,)
    school = found["other"]
    assert (school.what, school.start, school.end, school.seen, school.of) == (
        "Maple College",
        "08:50",
        "15:00",
        4,
        5,
    )
    assert school.last_seen == TUESDAYS[3].isoformat()
    assert school.sources == tuple(f"{d.isoformat()}-s" for d in TUESDAYS[:4])
    ride = found["ride"]
    assert (ride.line, ride.from_stop, ride.to_stop, ride.start, ride.end) == (
        "99",
        "Alder St",
        "Birch Ave",
        "08:20",
        "08:40",
    )
    assert (ride.seen, ride.of) == (3, 5)
    assert ride.sources == tuple(f"ride-99-{d}" for d in (TUESDAYS[0], TUESDAYS[2], TUESDAYS[4]))
    sleep = found["sleep"]
    assert (sleep.start, sleep.end, sleep.spread_min, sleep.seen, sleep.of) == (
        "00:00",
        "07:00",
        20,
        4,
        4,
    )
    assert sleep.sources == tuple(f"night-{i}" for i in range(4))

    again = {r.kind: r.routine_id for r in routines.for_day(conn, DAY, zone=ZONE, places=kept)}
    assert again == {kind: r.routine_id for kind, r in found.items()}
    later = routines.for_day(conn, DAY + timedelta(weeks=1), zone=ZONE, places=kept)
    assert {r.routine_id for r in later} == {r.routine_id for r in found.values()}
    assert routines.for_day(conn, DAY + timedelta(days=2), zone=ZONE, places=kept) == []


def test_two_weeks_are_not_a_routine_yet(tmp_path: Path) -> None:
    """Two Tuesdays of the same day are not enough to call it his Tuesday."""
    conn, kept = _world(tmp_path)
    for day in TUESDAYS[3:]:
        _school_day(conn, day, "08:10")
        _ride(conn, f"ride-{day}", "99", day, "08:20")

    assert routines.for_day(conn, DAY, zone=ZONE, places=kept) == []
