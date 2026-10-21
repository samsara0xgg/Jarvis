"""ADR 0316 — he is on the way where his fixes change place, and motion only names how.

Acceptance checks, each against the real code and a real event log:

- the day line, as a table of log rows (fixes, motion) to the stays and moves of a day: a driving
  lesson that motion called automotive for six minutes and unknown for the rest, with two parked
  stops; a fix that strays from home and comes back; a walk motion missed, unnamed; the same walk
  named by the one activity motion gave on it; a walk to the car then a drive no motion saw;
  cycling at car speed; a drive still going; a drive whose fixes are too poor to count;
- a bus ride replacing an unnamed move as it replaces an automotive one;
- the plan facts seeing a drive no motion saw, and an unnamed move as ``moving``.

Every coordinate is made up, north of (10.0000, 20.0000). Times in the table are minutes from the
local midnight that opens the case's day; the day is 2026-10-10 in Vancouver.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

import pytest

from jarvis.state.day_line import _without_ridden_moves, day_window, fold_day, parse_day
from jarvis.state.event_log import open_event_log
from jarvis.state.plan_facts import facts_at
from tests.support.day_line import Row, load_rows, make_stores, minutes_ms, motion
from tests.support.fixes import DAY, Seen, as_seen, cut, fix, spot, trail
from tests.support.places import ZONE

if TYPE_CHECKING:
    from pathlib import Path

PARK, CORNER = spot(15000), spot(19500)  # the lesson's two parked stops


def went(uid: str, start: float, end: float | None, activity: str | None) -> Seen:
    """An expected move; ``place`` carries its activity, absent when nothing named it."""
    return Seen("move", uid, start, end, activity)


class Case(NamedTuple):
    """Log rows of one day, and the stays and moves that day folds to."""

    name: str
    rows: list[Row]
    items: list[Seen]
    now: float = 1200  # 20:00


def _home(name: str, *minutes: float) -> list[Row]:
    return [fix(f"{name}{k}", minute) for k, minute in enumerate(minutes, 1)]


CASES: list[Case] = [
    Case(
        "a lesson motion called automotive for six minutes is driven from home to home, around "
        "two parked stops",
        [
            *_home("h", 700, 760),
            motion("unk", "unknown", 788, 850), motion("car", "automotive", 789, 795),
            *trail("d", 788.5, 814, 0.5, spot(200), PARK),  # about 35 km/h
            fix("p1", 814.5, PARK), fix("p2", 815, spot(10, of=PARK)),
            *trail("e", 827, 834, 0.5, spot(300, of=PARK), CORNER),
            fix("q1", 835, CORNER), fix("q2", 836, CORNER),
            *trail("f", 844, 870, 0.5, spot(-300, of=CORNER), spot(200)),
            *_home("b", 872, 900),
        ],
        [
            cut("h1", 700, 788.5),
            went("d0@788", 788.5, 814, "automotive"),
            cut("d51", 814, 827),
            went("e0@827", 827, 834, "automotive"),
            cut("e14", 834, 844),
            went("f0@844", 844, 872, "automotive"),
            cut("b1", 872, None),
        ],
    ),
    Case(
        "a fix that strays 200 m from home with 65 m accuracy and comes back is not a trip",
        [*_home("h", 540, 600), fix("stray", 610, spot(200), acc=65), *_home("b", 614, 700)],
        [cut("h1", 540, 610), cut("b1", 614, None)],
    ),
    Case(
        "a walk around the block motion missed is a move nothing names",
        [
            *_home("h", 540, 600),
            *trail("out", 601, 611, 1, spot(200), spot(1300)),
            *trail("back", 612, 622, 1, spot(1190), spot(200)),
            *_home("b", 624, 700),
        ],
        [cut("h1", 540, 601), went("out0@601", 601, 624, None), cut("b1", 624, None)],
    ),
    Case(
        "the one activity motion gave on a path names what it did not cover",
        [
            *_home("h", 540, 600),
            *trail("out", 601, 611, 1, spot(200), spot(1300)),
            motion("w", "walking", 611, 615),
            *trail("back", 612, 622, 1, spot(1190), spot(200)),
            *_home("b", 624, 700),
        ],
        [cut("h1", 540, 601), went("out0@601", 601, 624, "walking"), cut("b1", 624, None)],
    ),
    Case(
        "a walk to the car keeps motion's name, and the drive no motion saw is automotive",
        [
            *_home("h", 540, 600),
            motion("w", "walking", 600, 605),
            *trail("walk", 601, 605, 1, spot(200), spot(400)),
            *trail("car", 605.5, 625, 0.5, spot(700), spot(15000)),
            fix("there1", 626, spot(15000)), fix("there2", 640, spot(15000)),
        ],
        [
            cut("h1", 540, 600),
            went("w", 600, 605, "walking"),
            went("h2@605", 605, 625, "automotive"),  # the walk took h2's minute off the stay
            cut("car39", 625, None),
        ],
    ),
    Case(
        "cycling at car speed is cycling: motion said so on the path, and its id leads",
        [
            *_home("h", 540, 600),
            motion("bike", "cycling", 601, 606),
            *trail("ride", 601, 620, 0.5, spot(300), spot(12000)),
            fix("there1", 621, spot(12000)), fix("there2", 640, spot(12000)),
        ],
        [
            cut("h1", 540, 601),
            went("bike", 601, 620, "cycling"),
            cut("ride38", 620, None),
        ],
    ),
    Case(
        "a drive whose last fix is under five minutes old is still going",
        [*_home("h", 1000, 1150), *trail("d", 1180, 1198, 0.5, spot(300), spot(15000))],
        [cut("h1", 1000, 1180), went("d0@1180", 1180, None, "automotive")],
    ),
    Case(
        "fixes worse than 100 m make no path: home is one stay",
        [
            *_home("h", 540, 600),
            *[fix(f"poor{k}", 601 + k, spot(500 * k + 300), acc=150) for k in range(10)],
            *_home("b", 612, 700),
        ],
        [cut("h1", 540, None)],
    ),
]


@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
def test_the_fixes_between_stays_fold_into_moves(tmp_path: Path, case: Case) -> None:
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
    assert line_.missing == []


def test_a_ride_replaces_an_unnamed_move_as_it_replaces_a_drive() -> None:
    """A ride covering half of a move with no activity, or an automotive one, takes its place."""
    def move(uid: str, start: int, end: int, activity: str | None) -> dict[str, object]:
        item: dict[str, object] = {"id": uid, "kind": "move", "start_ms": start, "end_ms": end}
        if activity is not None:
            item["activity"] = activity
        return item

    ride = {"id": "r", "kind": "ride", "start_ms": 100, "end_ms": 200}
    items = [
        move("unnamed", 100, 190, None), move("drive", 90, 200, "automotive"),
        move("walk", 100, 200, "walking"), move("half", 150, 260, None), ride,
    ]

    kept = [item["id"] for item in _without_ridden_moves(items, now_ms=1000)]

    assert kept == ["walk", "half", "r"]


def test_the_plan_facts_see_a_drive_no_motion_saw_and_an_unnamed_move(tmp_path: Path) -> None:
    """The lesson's first leg is an automotive Move; the walk motion missed is ``moving``."""
    start_ms, _ = day_window(parse_day(DAY), ZONE)
    conn = open_event_log(tmp_path / "events.db")
    load_rows(conn, [
        *_home("h", 400, 480),
        *trail("out", 481, 491, 1, spot(200), spot(1300)),
        *trail("back", 492, 502, 1, spot(1190), spot(200)),
        *_home("b", 504, 560),
        *trail("d", 600, 614, 0.5, spot(200), PARK),
        fix("p1", 615, PARK), fix("p2", 640, PARK),
    ], start_ms)
    facts = facts_at(conn, parse_day(DAY), minutes_ms(start_ms, 700), ZONE, [])
    conn.close()

    assert [((m.start_ms - start_ms) / 60_000, m.activity) for m in facts.moves] == [
        (481, "moving"), (600, "automotive"),
    ]
