"""ADR 0330 — stays, moves and rides share no minute: rides, then moves, then stays.

Acceptance checks, each against the real code and a real event log:

- the day line, as a table of log rows (visits, motion, fixes) to the stays and moves of a day,
  with no two of them overlapping: two drives whose visits iOS dated before the car stopped and
  after it started (10-20 evening); a walk across two places iOS put back to back (10-17); a walk
  across one visit iOS split in two; a visit wholly inside a walk; a stop inside a drive; a walk
  and a path named walking kept apart by a short visit between them;
- a ride taking its time from every stay and move: an automotive or unnamed move it covers half of
  dropped as before, a walk inside it dropped, a move it covers less than half of cut, a stay
  around it split, stays it overlaps cut short, pieces outside the window left out.

Every coordinate is made up, north of (10.0000, 20.0000). Times in the table are minutes from the
local midnight that opens the case's day; the day is 2026-10-10 in Vancouver.
"""

from __future__ import annotations

from itertools import pairwise
from typing import TYPE_CHECKING, Any, NamedTuple

import pytest

from jarvis.state.day_line import _around_rides, day_window, fold_day, parse_day
from jarvis.state.event_log import open_event_log
from jarvis.state.phone_location import VISIT_EVENT
from tests.support.day_line import Row, load_rows, make_stores, minutes_ms, motion
from tests.support.fixes import DAY, HOME, Seen, as_seen, cut, fix, spot, trail
from tests.support.places import ZONE

if TYPE_CHECKING:
    from pathlib import Path


def visit(
    uid: str, arrived: float, departed: float | None = None, at: tuple[float, float] = HOME,
) -> Row:
    """A ``phone.visit_observed`` row at ``at``, with 20 m accuracy."""
    payload: dict[str, Any] = {
        "lat": at[0], "lng": at[1], "accuracy_m": 20.0, "arrived_at_ms": arrived,
    }
    if departed is not None:
        payload["departed_at_ms"] = departed
    return Row(VISIT_EVENT, uid, payload, departed or arrived)


def visited(uid: str, start: float, end: float | None) -> Seen:
    """An expected stay from a visit."""
    return Seen("stay", uid, start, end)


def went(uid: str, start: float, end: float | None, activity: str | None) -> Seen:
    """An expected move; ``place`` carries its activity."""
    return Seen("move", uid, start, end, activity)


class Case(NamedTuple):
    """Log rows of one day, and the stays and moves that day folds to."""

    name: str
    rows: list[Row]
    items: list[Seen]
    now: float = 1200  # 20:00


CASES: list[Case] = [
    Case(
        "a drive keeps the minutes iOS dated its visits by, before the car stopped and after it "
        "started",
        [
            visit("a", 600, 700), motion("d1", "automotive", 699, 710),
            visit("b", 709, 780), motion("d2", "automotive", 779, 795), visit("c", 794),
        ],
        [
            visited("a", 600, 699), went("d1", 699, 710, "automotive"), visited("b", 710, 779),
            went("d2", 779, 795, "automotive"), visited("c", 795, None),
        ],
    ),
    Case(
        "a walk across two places iOS put back to back keeps its minutes from both",
        [
            visit("a", 600, 625), visit("b", 625, 640, spot(400)),
            motion("w", "walking", 618, 632), motion("d", "automotive", 637, 642),
        ],
        [
            visited("a", 600, 618), went("w", 618, 632, "walking"), visited("b", 632, 637),
            went("d", 637, 642, "automotive"),
        ],
    ),
    Case(
        "a walk across a visit iOS split in two at one place, a second apart, is about the place",
        [
            visit("a", 600, 625), visit("b", 625 + 1 / 60, 640, spot(40)),
            motion("w", "walking", 618, 632),
        ],
        [visited("a", 600, 625), visited("b", 625 + 1 / 60, 640)],
    ),
    Case(
        "a stop inside a drive is a stop: the drive is cut in two around it",
        [
            visit("home", 480, 600), motion("d", "automotive", 600, 640),
            visit("shop", 610, 625, spot(3000)), visit("work", 640, 700, spot(6000)),
        ],
        [
            visited("home", 480, 600), went("d", 600, 610, "automotive"),
            visited("shop", 610, 625), went("d@625", 625, 640, "automotive"),
            visited("work", 640, 700),
        ],
    ),
    Case(
        "a visit wholly inside a walk is dropped, as a cut stay is, and the walk keeps home's "
        "early arrival",
        [
            visit("a", 600, 615), motion("w", "walking", 615, 631), visit("v", 618, 623),
            visit("h", 623),
        ],
        [visited("a", 600, 615), went("w", 615, 631, "walking"), visited("h", 631, None)],
    ),
    Case(
        "a walk and the path after it, named walking, stay apart across a short visit",
        [
            fix("h1", 540), fix("h2", 598),
            motion("w1", "walking", 600, 605), visit("v", 605, 607, spot(600)),
            motion("w2", "walking", 609, 612),  # more than three minutes after w1: two walks
            *trail("t", 607.5, 615, 0.5, spot(700), spot(1500)),
            fix("e1", 616, spot(1800)), fix("e2", 640, spot(1800)),
        ],
        [
            cut("h1", 540, 600), went("w1", 600, 605, "walking"), visited("v", 605, 607),
            went("t0@607", 607, 616, "walking"), cut("e1", 616, None),
        ],
    ),
]


def _apart(items: list[dict[str, Any]], now_ms: int) -> bool:
    spans = sorted((i["start_ms"], now_ms if i["end_ms"] is None else i["end_ms"]) for i in items)
    return all(end <= start for (_, end), (start, _) in pairwise(spans))


@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
def test_the_stays_and_moves_of_a_day_never_overlap(tmp_path: Path, case: Case) -> None:
    """Each case: rows appended in order, the day cut in its zone, folded at its own now."""
    start_ms, end_ms = day_window(parse_day(DAY), ZONE)
    conn = open_event_log(tmp_path / "events.db")
    load_rows(conn, case.rows, start_ms)
    now_ms = minutes_ms(start_ms, case.now)
    line_ = fold_day(conn, start_ms, end_ms, now_ms, make_stores(tmp_path, ZONE))
    conn.close()

    items = [i for i in line_.items if i["kind"] in ("stay", "move")]
    for item in items:  # the table's place column carries a move's activity
        if item["kind"] == "move":
            item["place"] = item.get("activity")
    assert as_seen(items, start_ms, case.items) == case.items
    assert _apart(items, now_ms)
    assert line_.missing == []


def test_a_ride_takes_its_time_from_every_stay_and_move() -> None:
    """Dropped, cut short, split or left alone, as the ride covers each; pieces in the window."""
    def item(kind: str, uid: str, start: int, end: int | None, **fields: Any) -> dict[str, Any]:  # noqa: ANN401
        return {
            "id": uid, "lane": "you", "kind": kind, "start_ms": start, "end_ms": end,
            "state": "done", **fields,
        }

    items = [
        item("ride", "r", 100, 200), item("ride", "open", 500, None),
        item("move", "unnamed", 100, 190), item("move", "drive", 90, 200, activity="automotive"),
        item("move", "walk", 120, 180, activity="walking"), item("move", "half", 150, 260),
        item("stay", "around", 50, 300, place="Mall"), item("stay", "into", -60, 150),
        item("stay", "home", 450, None), item("sleep", "night", 120, 140),
        item("stay", "gone", 120, 160),
    ]

    kept = _around_rides(items, 0, 1000, now_ms=600)

    assert [(i["id"], i["start_ms"], i["end_ms"], i["state"]) for i in kept] == [
        ("r", 100, 200, "done"), ("open", 500, None, "done"),
        ("half", 200, 260, "done"),
        ("around", 50, 100, "done"), ("around@200", 200, 300, "done"),
        ("into", -60, 100, "done"),
        ("home", 450, 500, "done"),
        ("night", 120, 140, "done"),
    ]
    assert kept[3]["place"] == kept[4]["place"] == "Mall"
