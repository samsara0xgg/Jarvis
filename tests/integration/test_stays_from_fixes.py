"""ADR 0301 — where he stayed is cut from the phone's fixes when no visit covers the time.

Acceptance checks, each against the real code and a real event log:

- the day line, as a table of log rows (fixes, motion, visits) to the stays and moves of a day: a
  day of fixes only (two stays and a walk between), a night at one spot as one stay, a stay that
  began days before the window, a drive that is no stay (even stopped at a light), a fix of 300 m
  ignored, the five-minute floor from both sides, a stay left by motion, by the next fix outside
  the run, or not at all (open), motion that does not count as leaving, a slow walk that is no
  stay, a move inside a cut stay dropped, and a visit winning over a cut stay in the middle, at the
  head, at the tail, leaving less than five minutes, open;
- the stamp noticing a late fix, and the stays folded again from the log with it;
- the places tally and the ledger counting a stay cut from fixes.

Every coordinate is made up, around (10.0000, 20.0000). Times in the table are minutes from the
local midnight that opens the case's day; the day is 2026-10-10 in Vancouver.
"""

from __future__ import annotations

import math
from datetime import timedelta
from typing import TYPE_CHECKING, Any, NamedTuple
from zoneinfo import ZoneInfo

import pytest

from jarvis.shared.geo import metres
from jarvis.state import phone_day, places
from jarvis.state.day_line import day_window, fold_day, parse_day
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.state.ledger import _Phone
from jarvis.state.phone_location import LOCATION_EVENT, VISIT_EVENT
from tests.integration import test_places
from tests.integration.test_day_line import MINUTE, Row, _load, _ms, _stores, motion

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

ZONE = ZoneInfo("America/Vancouver")
DAY = "2026-10-10"
HOME = (10.0, 20.0)
_M_PER_DEGREE = 111_194.9


def spot(
    north: float = 0.0, east: float = 0.0, *, of: tuple[float, float] = HOME,
) -> tuple[float, float]:
    """The point ``north`` metres north and ``east`` metres east of ``of``."""
    lat = of[0] + north / _M_PER_DEGREE
    return lat, of[1] + east / (_M_PER_DEGREE * math.cos(math.radians(of[0])))


SCHOOL = spot(2240)  # 2.24 km north of home
FAR = spot(5000)  # a fix a little before now, nowhere near: the first of the next run
QUARRY, AWAY = (30.0, 40.0), (30.05, 40.0)


def fix(
    uid: str, minute: float, at: tuple[float, float] = HOME, *, acc: float = 20.0,
    place: str | None = None,
) -> Row:
    """A ``phone.location_observed`` row at ``minute``."""
    payload: dict[str, Any] = {"lat": at[0], "lng": at[1], "accuracy_m": acc}
    if place is not None:
        payload["place"] = place
    return Row(LOCATION_EVENT, uid, payload, minute)


def visit(uid: str, arrived: float, departed: float | None = None, place: str | None = None) -> Row:
    """A ``phone.visit_observed`` row (a made-up point 20 m from home)."""
    lat, lng = spot(20)
    payload: dict[str, Any] = {"lat": lat, "lng": lng, "accuracy_m": 65.0, "arrived_at_ms": arrived}
    if departed is not None:
        payload["departed_at_ms"] = departed
    if place is not None:
        payload["place"] = place
    return Row(VISIT_EVENT, uid, payload, departed or arrived)


def trail(  # noqa: PLR0913 - a straight line of fixes: its name, span, step and ends
    name: str, first: float, last: float, step: float, start: tuple[float, float],
    end: tuple[float, float],
) -> list[Row]:
    """Fixes every ``step`` minutes from ``first`` to ``last``, on the line ``start`` to ``end``."""
    count = round((last - first) / step)
    return [
        fix(
            f"{name}{k}", first + k * step,
            (
                start[0] + (end[0] - start[0]) * k / count,
                start[1] + (end[1] - start[1]) * k / count,
            ),
        )
        for k in range(count + 1)
    ]


def line(name: str, first: float, last: float, step: float, metres: float) -> list[Row]:
    """Fixes every ``step`` minutes, ``metres`` apart, going north from home."""
    return [
        fix(f"{name}{k}", first + k * step, spot(metres * k))
        for k in range(round((last - first) / step) + 1)
    ]


class Seen(NamedTuple):
    """An item of the day as the table expects it; ``acc`` is checked only when given."""

    kind: str
    uid: str
    start: float
    end: float | None
    place: str | None = None
    cut: bool = False
    acc: float | None = None


def cut(
    uid: str, start: float, end: float | None, place: str | None = None, acc: float | None = None,
) -> Seen:
    """An expected stay cut from fixes."""
    return Seen("stay", uid, start, end, place, cut=True, acc=acc)


def visited(uid: str, start: float, end: float | None, place: str | None = None) -> Seen:
    """An expected stay from a visit."""
    return Seen("stay", uid, start, end, place)


def went(uid: str, start: float, end: float | None) -> Seen:
    """An expected move (the activity is the case's business)."""
    return Seen("move", uid, start, end)


class Case(NamedTuple):
    """Log rows of one day, and the stays and moves that day folds to."""

    name: str
    rows: list[Row]
    items: list[Seen]
    now: float = 1200  # 20:00


CASES: list[Case] = [
    Case(
        "a day of fixes only is two stays and a walk between, then home again",
        [
            fix("a1", 540, place="Home St", acc=10), fix("a2", 543, place="Home St", acc=20),
            fix("a3", 547, place="Other Rd", acc=30),
            motion("w1", "walking", 560, 588),
            *trail("t", 562, 588, 2, spot(160), SCHOOL),
            fix("s1", 592, SCHOOL, place="School Rd"), fix("s2", 596, SCHOOL),
            fix("s3", 600, SCHOOL),
            motion("w2", "walking", 660, 688),
            *trail("u", 662, 688, 2, spot(2080), HOME),
            fix("b1", 692, acc=40), fix("b2", 696, acc=40),
        ],
        [
            cut("a1", 540, 560, "Home St", acc=20.0),
            went("w1", 560, 588),
            cut("t13", 588, 660, "School Rd"),
            went("w2", 660, 688),
            cut("u13", 688, None),
        ],
    ),
    Case(
        "a night at one spot, hours between the last fix of the evening and the first of the "
        "morning, is one stay",
        [
            fix("e1", -180), fix("e2", -176), fix("e3", -172),
            fix("m1", 430, spot(30)), fix("m2", 433, spot(-30)),
            motion("w1", "walking", 450, 470), fix("far", 1198, FAR),
        ],
        [cut("e1", -180, 450), went("w1", 450, 470)],
    ),
    Case(
        "a short walk about the flat at night does not end the stay: he left by the move that "
        "carried him away, before a first fix that is already outside",
        [
            fix("e1", -150), fix("e2", -120),
            motion("flat", "walking", -60, -56, "medium"),
            motion("go", "automotive", 530, 545), fix("out", 540, FAR),
        ],
        [cut("e1", -150, 530), went("go", 530, 545), cut("out", 545, None)],
    ),
    Case(
        "with no move between the runs (a doubtful walk is none) the stay ends at the next fix",
        [
            fix("e1", -150), fix("e2", -120),
            motion("flat", "walking", -60, -56, "low"), fix("out", 540, FAR),
        ],
        [cut("e1", -150, 540), cut("out", 540, None)],
    ),
    Case(
        "a move that crosses a stay's edges keeps its minutes: the first fix of the next run came "
        "while he was still walking in, the last of this run while he was already walking off",
        [
            fix("a1", 540), fix("a2", 545), fix("a3", 601),
            motion("w1", "walking", 600, 609),
            fix("b1", 604, SCHOOL), fix("b2", 612, SCHOOL), motion("w2", "walking", 700, 720),
            fix("far", 1198, FAR),
        ],
        [cut("a1", 540, 600), went("w1", 600, 609), cut("b1", 609, 700), went("w2", 700, 720)],
    ),
    Case(
        "a run with no run after it is open, whatever moves follow its last fix",
        [
            fix("a1", 540), fix("a2", 545),
            motion("w1", "walking", 600, 610), motion("w2", "walking", 700, 720),
        ],
        [cut("a1", 540, None)],  # the moves are inside the open stay
    ),
    Case(
        "a stay that began days before the window still covers it, from the last good fix",
        [fix("old", -4000), fix("poor", -3500, spot(5000), acc=300)],
        [cut("old", -4000, None)],
    ),
    Case(
        "only the last good fix before the window is read",
        [fix("old", -4000), fix("last", -3000, SCHOOL)],
        [cut("last", -3000, None)],
    ),
    Case(
        "a drive is no stay, even stopped at a light for six minutes",
        [
            motion("d1", "automotive", 600, 615), motion("wk", "walking", 616, 630),
            fix("far", 1198, FAR),
            *[fix(f"x{k}", 600.5 + k / 2, spot(800 * (k / 2 + 0.5))) for k in range(13)],
            fix("light1", 607, spot(5600)), fix("light2", 613, spot(5650)),
            fix("x14", 614, spot(11000)),
        ],
        [went("d1", 600, 615), went("wk", 616, 630)],
    ),
    Case(
        "a fix of 300 m is ignored: it neither splits a stay nor moves its centre",
        [
            fix("g1", 540, acc=10), fix("g2", 545, acc=30), fix("bad", 548, spot(5000), acc=300),
            fix("g3", 551, acc=20), motion("w1", "walking", 560, 575), fix("far", 1198, FAR),
        ],
        [cut("g1", 540, 560, acc=20.0), went("w1", 560, 575)],
    ),
    Case(
        "a fix of exactly 100 m counts and one a little worse does not",
        [fix("k1", 540, acc=100), fix("k2", 546, spot(5000), acc=100.5), fix("k3", 548, acc=100)],
        [cut("k1", 540, None, acc=100.0)],
    ),
    Case(
        "a stay of exactly five minutes is kept",
        [
            fix("f1", 600), fix("f2", 601), fix("f3", 602),
            motion("w1", "walking", 605, 615), fix("far", 1198, FAR),
        ],
        [cut("f1", 600, 605), went("w1", 605, 615)],
    ),
    Case(
        "a stay of four minutes is dropped",
        [
            fix("f1", 600), fix("f2", 601), fix("f3", 602),
            motion("w1", "walking", 604, 614), fix("far", 1198, FAR),
        ],
        [went("w1", 604, 614)],
    ),
    Case(
        "a run nothing has left is open: five minutes to now is a stay",
        [fix("o1", 1195), fix("o2", 1196)],
        [cut("o1", 1195, None)],
    ),
    Case(
        "an open run only three minutes old is no stay yet",
        [fix("o1", 1197), fix("o2", 1199)],
        [],
    ),
    Case(
        "an open run is still going however long ago it began",
        [fix("o1", 1100, SCHOOL), fix("o2", 1103, SCHOOL), fix("o3", 1106, SCHOOL)],
        [cut("o1", 1100, None)],
    ),
    Case(
        "without motion the next fix outside the run ends the stay and begins the next",
        [
            fix("a1", 540), fix("a2", 545), fix("b1", 700, SCHOOL), fix("b2", 705, SCHOOL),
        ],
        [cut("a1", 540, 700), cut("b1", 700, None)],
    ),
    Case(
        "motion that is doubtful or under three minutes does not end a stay",
        [
            fix("a1", 540), fix("a2", 545),
            motion("low", "walking", 560, 575, "low"), motion("blip", "walking", 600, 602),
            motion("go", "walking", 650, 670), fix("far", 1198, FAR),
        ],
        [cut("a1", 540, 650), went("go", 650, 670)],
    ),
    Case(
        "a move inside a stay cut from fixes is dropped, as inside a visit",
        [fix("a1", 540), fix("a2", 545), fix("a3", 620), motion("pace", "walking", 600, 610)],
        [cut("a1", 540, None)],
    ),
    Case(
        "a slow walk's fixes are close enough to look like runs and are no stay",
        [
            motion("w1", "walking", 600, 640),
            *line("p", 600, 639, 1, 55),
            fix("q1", 645, spot(2145)), fix("q2", 650, spot(2145)),
        ],
        [went("w1", 600, 640), cut("p35", 640, None)],
    ),
    # --- visits win -----------------------------------------------------------------------
    Case(
        "a visit in the middle of a cut stay splits it, the later piece taking the minute",
        [
            fix("f1", 540), fix("f2", 545), fix("f3", 550), motion("w1", "walking", 700, 720),
            fix("far", 1198, FAR),
            visit("v1", 600, 640, "Cafe"),
        ],
        [
            cut("f1", 540, 600), visited("v1", 600, 640, "Cafe"), cut("f1@640", 640, 700),
            went("w1", 700, 720),
        ],
    ),
    Case(
        "a visit over the head of a cut stay leaves the rest under the first fix's id",
        [
            fix("f1", 540), fix("f2", 545), fix("f3", 550), motion("w1", "walking", 700, 720),
            fix("far", 1198, FAR),
            visit("v1", 520, 600),
        ],
        [visited("v1", 520, 600), cut("f1", 600, 700), went("w1", 700, 720)],
    ),
    Case(
        "a visit that leaves under five minutes of a cut stay takes all of it",
        [
            fix("f1", 540), fix("f2", 545), fix("f3", 550), motion("w1", "walking", 700, 720),
            fix("far", 1198, FAR),
            visit("v1", 520, 697),
        ],
        [visited("v1", 520, 697), went("w1", 700, 720)],
    ),
    Case(
        "a visit that leaves exactly five minutes leaves a stay",
        [
            fix("f1", 540), fix("f2", 545), fix("f3", 550), motion("w1", "walking", 700, 720),
            fix("far", 1198, FAR),
            visit("v1", 520, 695),
        ],
        [visited("v1", 520, 695), cut("f1", 695, 700), went("w1", 700, 720)],
    ),
    Case(
        "an arrival with no departure covers what follows it, so the cut stay ends where it begins",
        [fix("f1", 540), fix("f2", 545), fix("f3", 550), visit("v1", 600)],
        [cut("f1", 540, 600), visited("v1", 600, None)],
    ),
]


def _seen(items: list[dict[str, Any]], origin: int, expected: list[Seen]) -> list[Seen]:
    """The day's stays and moves as the table words them (``@`` ids with their start in minutes)."""
    found: list[Seen] = []
    for item, want in zip(items, expected, strict=False):
        uid = str(item["id"])
        if "@" in uid:
            head, tail = uid.split("@")
            uid = f"{head}@{(int(tail) - origin) // MINUTE}"
        found.append(Seen(
            item["kind"], uid, (item["start_ms"] - origin) / MINUTE,
            None if item["end_ms"] is None else (item["end_ms"] - origin) / MINUTE,
            item.get("place"), item.get("cut_from") == "fixes",
            item.get("accuracy_m") if want.acc is not None else None,
        ))
    found.extend(
        Seen(item["kind"], str(item["id"]), 0, None) for item in items[len(expected):]
    )
    return found


@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
def test_the_fixes_fold_into_the_stays_of_the_day(tmp_path: Path, case: Case) -> None:
    """Each case: rows appended in order, the day cut in its zone, folded at its own now."""
    start_ms, end_ms = day_window(parse_day(DAY), ZONE)
    conn = open_event_log(tmp_path / "events.db")
    _load(conn, case.rows, start_ms)
    line_ = fold_day(conn, start_ms, end_ms, _ms(start_ms, case.now), _stores(tmp_path, ZONE))
    conn.close()

    items = [i for i in line_.items if i["kind"] in ("stay", "move")]
    assert _seen(items, start_ms, case.items) == case.items
    assert line_.missing == []


def test_a_cut_stay_has_the_shape_of_a_visit_stay_and_its_first_fixs_uid(tmp_path: Path) -> None:
    """Fields, id and origin of one stay: the mean of its fixes, the phone's commonest name."""
    start_ms, end_ms = day_window(parse_day(DAY), ZONE)
    conn = open_event_log(tmp_path / "events.db")
    rows = [
        fix("a1", 540, spot(10), place="Home St"), fix("a2", 543, spot(-10), place="Home St"),
        fix("a3", 547, spot(30), place="Other Rd"), fix("a4", 549, spot(-30), place=None),
    ]
    _load(conn, rows, start_ms)
    line_ = fold_day(conn, start_ms, end_ms, _ms(start_ms, 1200), _stores(tmp_path, ZONE))
    conn.close()

    (item,) = [i for i in line_.items if i["kind"] == "stay"]
    assert set(item) == {
        "id", "lane", "kind", "start_ms", "end_ms", "state", "lat", "lng", "accuracy_m", "place",
        "cut_from",
    }
    assert (item["id"], item["lane"], item["state"], item["end_ms"]) == ("a1", "you", "now", None)
    assert (item["lat"], item["lng"]) == pytest.approx(HOME, abs=1e-9)
    assert (item["accuracy_m"], item["place"], item["cut_from"]) == (20.0, "Home St", "fixes")


def test_the_stamp_notices_a_late_fix_and_the_stays_are_folded_again(tmp_path: Path) -> None:
    """A fix timed in the range changes the stamp; one timed outside does not; stays follow."""
    start_ms, _ = day_window(parse_day(DAY), ZONE)
    conn = open_event_log(tmp_path / "events.db")
    rows = [fix("a1", 540), fix("a2", 545), fix("a3", 550)]
    _load(conn, rows, start_ms)
    first, last = _ms(start_ms, 0), _ms(start_ms, 1440)
    now = _ms(start_ms, 1200)

    def folded() -> list[tuple[str, int, int | None]]:
        read = phone_day.read_rows(conn, first - phone_day.LOOKBACK_MS)
        return [(s.origin[1], (s.start - start_ms) // MINUTE, None if s.end is None else
                 (s.end - start_ms) // MINUTE) for s in phone_day.stays(read, now)]

    stamp = phone_day.stamp(conn, first, last)
    assert folded() == [("a1", 540, None)]

    _load(conn, [fix("late", 800, SCHOOL)], start_ms)  # a late batch: timed in the range
    assert phone_day.stamp(conn, first, last) != stamp
    assert folded() == [("a1", 540, 800), ("late", 800, None)]

    again = phone_day.stamp(conn, first, last)
    _load(conn, [fix("tomorrow", 1500, SCHOOL)], start_ms)  # timed after the range
    assert phone_day.stamp(conn, first, last) == again
    conn.close()


# --- what reads the stays ----------------------------------------------------------------------


def _days_at_the_quarry(conn: sqlite3.Connection, name: str | None) -> None:
    """Three days of fixes only at one made-up spot, an hour each, left on foot for elsewhere."""
    at = QUARRY
    for days_ago in (1, 2, 3):
        noon = test_places.at(days_ago, 12)
        for number, minutes in enumerate((0, 3, 6)):
            emit_event(
                conn, type=LOCATION_EVENT, ts_epoch_ms=noon + minutes * MINUTE,
                payload={"lat": at[0], "lng": at[1], "accuracy_m": 25.0, "place": name or ""},
                event_uid=f"q{days_ago}-{number}", ingestion_node="phone",
            )
        emit_event(
            conn, type=LOCATION_EVENT, ts_epoch_ms=noon + 70 * MINUTE, event_uid=f"qa{days_ago}",
            payload={"lat": AWAY[0], "lng": AWAY[1], "accuracy_m": 25.0}, ingestion_node="phone",
        )
        emit_event(
            conn, type=phone_day.MOTION_EVENT, ts_epoch_ms=noon + 60 * MINUTE, payload={
                "activity": "walking", "confidence": "high",
                "started_at_ms": noon + 60 * MINUTE, "ended_at_ms": noon + 70 * MINUTE,
            },
            event_uid=f"qw{days_ago}", ingestion_node="phone",
        )


def test_the_places_tally_counts_a_stay_cut_from_fixes(tmp_path: Path) -> None:
    """Three days of fixes and no visit make a spot with their days and the usual length."""
    conn = open_event_log(tmp_path / "events.db")
    _days_at_the_quarry(conn, "Quarry Rd 9")
    seen = places.read_stays(conn, test_places.NOW_MS, ZONE)
    there = [s for s in seen if s.fields.get("place") == "Quarry Rd 9"]
    assert [(s.fields["cut_from"], (s.end or 0) - s.start) for s in there] == (
        [("fixes", 60 * MINUTE)] * 3
    )

    counted = places.tally(seen, [], test_places.NOW_MS, ZONE)
    (found,) = [one for one in counted.spots if metres((one.lat, one.lng), QUARRY) < 100]
    assert found.facts == places.Facts(
        3, 3, test_places.TODAY - timedelta(days=1), 60, "Quarry Rd 9",
    )
    assert (found.lat, found.lng) == pytest.approx(QUARRY, abs=1e-9)

    kept = places.Place("p1", "Quarry", "other", 30.0, 40.0, 120, None, "user", "t", "t")
    held = places.tally(seen, [kept], test_places.NOW_MS, ZONE)
    assert held.kept == {"p1": found.facts}
    assert not [one for one in held.spots if metres((one.lat, one.lng), QUARRY) < 100]
    conn.close()


def test_the_ledger_names_a_cut_stay_by_its_kept_place_or_the_phones_words(tmp_path: Path) -> None:
    """The ledger's stay minutes take the merged list: named by the kept circle, else the fix's."""
    conn = open_event_log(tmp_path / "events.db")
    _days_at_the_quarry(conn, "Quarry Rd 9")
    since = test_places.at(5, 0)
    rows = phone_day.read_rows(conn, since - phone_day.LOOKBACK_MS)
    as_of = test_places.NOW
    kept = places.Place("p1", "Quarry", "other", 30.0, 40.0, 120, None, "user", "t", "t")

    for held, name in (([], "Quarry Rd 9"), ([kept], "Quarry")):
        phone = _Phone()
        phone._fold(ZONE, rows, as_of, held)  # noqa: SLF001
        assert phone.day(as_of.date() - timedelta(days=1)).places == [(name, 3600.0)]
    conn.close()
