"""ADR 0302 — what he was doing: fact spans, the waking cut, serving, and his answers.

Acceptance checks, as tables of day-line items to the spans the lane serves, over real code and,
for the stores and the route, a real memory.db, event log and FastAPI app in ``tmp_path``:

- the fact spans: each item that lasts a while to its lane, kind, title, category and place, and
  the points that are not spans;
- the waking window, with and without a night's sleep, for a past day and for today;
- the cut: every waking minute is in exactly one segment, nothing overlaps, a stretch no fact
  covers is a segment, and a piece under ten minutes is folded into the neighbour that shares its
  place, else the longer one;
- serving: the latest run up to what it covers, the program's segments after, his answers over
  both, the plan after now, and the coverage those add up to;
- the two tables: the newest run of a day wins and older ones stay, answers are appended and read
  back by day and by place;
- a store or a run that cannot be read leaves the lane program-cut, never a failed day;
- ``GET /inherent/day`` gains ``spans`` and ``coverage`` and its items stay as they were;
- ``POST /inherent/day/answer``: 200 with the span as now served, 404, 422, the guard, and the
  page showing his words afterwards.

Times in the tables are minutes from the day's start.
"""

from __future__ import annotations

import functools
import random
import re
from contextlib import closing
from datetime import date, datetime
from itertools import pairwise
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.runtime.doing_answers import DoingAnswers
from jarvis.runtime.inherent_loop import _answer_doing, _read_day
from jarvis.state import places, plan
from jarvis.state.day_line import DaySources, day_window, fold_day
from jarvis.state.doing import (
    BASES,
    CATEGORIES,
    CLUE_KINDS,
    CONFIDENCES,
    Waking,
    coverage,
    cut,
    fact_spans,
    his_facts,
    read_doing,
    serve,
    waking_window,
)
from jarvis.state.event_log import open_event_log, open_runtime_event_log
from jarvis.state.memory_db import (
    DoingAnswer,
    DoingRun,
    append_doing_answer,
    append_doing_run,
    doing_answers,
    doing_answers_near,
    latest_doing_run,
    open_memory_db,
)
from tests.support.day_highlights import DAY, DAY_KEY, NOW_MS, VANCOUVER, served_day
from tests.support.ledger import ledger_at, ledger_world
from tests.support.timeline import peers_with, refused_without_credentials, stays_of

if TYPE_CHECKING:
    from pathlib import Path

    from jarvis.state.ledger import LedgerSources

MIN = 60_000
DAY_START, DAY_END = 0, 24 * 60 * MIN
ANSWER = "/inherent/day/answer"


def _m(minutes: int | None) -> int | None:
    return None if minutes is None else minutes * MIN


def _item(
    item_id: str, kind: str, start: int, end: int | None, **fields: Any,  # noqa: ANN401 — item fields
) -> dict[str, Any]:
    """A day-line item as ``fold_day`` serves it, with its times in minutes."""
    return {
        "id": item_id, "lane": "you", "kind": kind, "start_ms": _m(start), "end_ms": _m(end),
        "state": "done", **fields,
    }


def _stay(item_id: str, start: int, end: int | None, **fields: Any) -> dict[str, Any]:  # noqa: ANN401
    return _item(item_id, "stay", start, end, lat=1.0, lng=2.0, accuracy_m=30.0, **fields)


# --- the vocabularies ------------------------------------------------------------------------


def test_the_closed_vocabularies_are_the_ones_the_adr_names() -> None:
    """The lists the lane and the timeline agree on."""
    assert CATEGORIES == (
        "home", "study", "work", "commute", "errand", "meal", "social", "rest", "sleep",
        "exercise", "unknown",
    )
    assert BASES == ("observed", "stated", "inferred", "planned")
    assert CONFIDENCES == ("high", "medium", "low")
    assert CLUE_KINDS == (
        "words", "picture", "mail", "calendar", "plan", "payment", "chat", "code", "screen",
        "answer", "photo", "away_note",
    )


# --- the fact spans --------------------------------------------------------------------------

HOME = {"id": "place:home", "name": "Home", "role": "home"}
SCHOOL = {"id": "place:uvic", "name": "UVic", "role": "school"}
GYM = {"id": "place:gym", "name": "Gym", "role": "other"}

FACT_CASES = [
    # the item, then the span: (lane, kind, title, category, place_id)
    (
        _stay("a", 0, 60, kept_place=HOME, place="1 Main St"),
        ("place", "stay", "Home", "home", "place:home"),
    ),
    (_stay("a", 0, 60, kept_place=SCHOOL), ("place", "stay", "UVic", "study", "place:uvic")),
    (_stay("a", 0, 60, kept_place=GYM), ("place", "stay", "Gym", "unknown", "place:gym")),
    (_stay("a", 0, 60, place="Corner Cafe"), ("place", "stay", "Corner Cafe", "unknown", None)),
    (_stay("a", 0, None), ("place", "stay", None, "unknown", None)),
    (_item("a", "move", 0, 30, activity="walking"), ("place", "walk", None, "commute", None)),
    (_item("a", "move", 0, 30, activity="running"), ("place", "run", None, "commute", None)),
    (_item("a", "move", 0, 30, activity="cycling"), ("place", "cycle", None, "commute", None)),
    (_item("a", "move", 0, 30, activity="automotive"), ("place", "drive", None, "commute", None)),
    (_item("a", "ride", 0, 30, route="99"), ("place", "ride", "99", "commute", None)),
    (
        _item("a", "work", 0, 60, projects=[{"name": "jarvis", "seconds": 900}],
              apps=[{"name": "Xcode", "seconds": 900}]),
        ("use", "work", "jarvis", "unknown", None),
    ),
    (
        _item("a", "work", 0, 60, projects=[], apps=[{"name": "Chrome", "seconds": 900}]),
        ("use", "work", "Chrome", "unknown", None),
    ),
    (_item("a", "work", 0, 60, projects=[], apps=[]), ("use", "work", None, "unknown", None)),
    (_item("a", "call", 0, 30, app="Zoom"), ("use", "call", "Zoom", "unknown", None)),
    (_item("a", "phone_call", 0, 5, app="Phone"), ("use", "call", "Phone", "unknown", None)),
    (
        _item("a", "screen", 0, 40, device="iPad", active_s=2100,
              apps=[{"name": "抖音", "bundle": "com.ss.iphone.ugc.Aweme", "minutes": 30,
                     "category": "Entertainment"}]),
        ("use", "screen", "抖音", "unknown", None),
    ),
    (
        _item("a", "screen", 0, 40, device="iPhone", apps=[]),
        ("use", "screen", None, "unknown", None),
    ),
    (
        _item("a", "code", 0, 90, title="Fix the cut"),
        ("use", "code", "Fix the cut", "unknown", None),
    ),
    (_item("a", "code", 0, 90, title=""), ("use", "code", None, "unknown", None)),
    (_item("a", "talk", 0, 5, turns=3), ("use", "talk", None, "unknown", None)),
    (_item("a", "sleep", 0, 450, minutes=440, stages=[]), ("body", "sleep", None, "sleep", None)),
    (
        _item("a", "workout", 0, 90, activity="Badminton"),
        ("body", "workout", "Badminton", "exercise", None),
    ),
]


@pytest.mark.parametrize(("item", "expected"), FACT_CASES)
def test_an_item_that_lasts_is_a_fact_span_of_its_lane(
    item: dict[str, Any], expected: tuple[Any, ...],
) -> None:
    """Lane, kind, title, category and place per kind; every fact is observed, high, unconfirmed."""
    [span] = fact_spans([item])
    served = (span["lane"], span["kind"], span["title"], span["category"], span["place_id"])
    assert served == expected
    assert span == {
        "id": "a", "lane": expected[0], "kind": expected[1], "start_ms": item["start_ms"],
        "end_ms": item["end_ms"], "title": expected[2], "detail": None, "category": expected[3],
        "basis": "observed", "confidence": "high", "confirmed": False, "clues": [],
        "question": None, "place_id": expected[4], "item_ids": ["a"],
    }


@pytest.mark.parametrize(
    "kind",
    ["picture", "payment", "event", "reminder", "departure", "claude_chat", "claude_day"],
)
def test_a_point_or_a_note_of_the_day_is_not_a_span(kind: str) -> None:
    """Pictures, payments, events and the rest stay items; they are clues, not spans."""
    assert fact_spans([_item("a", kind, 10, 10)]) == []


def test_fact_spans_come_by_start_then_id_and_an_open_end_stays_open() -> None:
    """A stay still going has no end; items that start together are ordered by id."""
    spans = fact_spans([
        _item("b", "call", 10, 20, app="x"), _stay("z", 30, None), _item("a", "talk", 10, 12),
    ])
    assert [(s["id"], s["end_ms"]) for s in spans] == [("a", _m(12)), ("b", _m(20)), ("z", None)]


# --- the waking window -----------------------------------------------------------------------


def _sleep(start: int, end: int | None) -> dict[str, Any]:
    return _item(f"sleep-{start}", "sleep", start, end, minutes=(end or start) - start, stages=[])


WAKING_CASES = [
    # sleeps, now (None: a past day), the window
    ([], None, (360, 1440)),  # no sleep: six hours in, to the day's end
    ([(-120, 450)], None, (450, 1440)),  # woke 07:30
    ([(-120, 450), (1380, 1800)], None, (450, 1380)),  # to bed 23:00
    ([(-120, 450), (1380, 1800)], 600, (450, 600)),  # today at 10:00
    ([(-120, 450)], 300, (450, 450)),  # today, before he woke: nothing yet
    ([], 300, (360, 360)),  # today before the default start
    ([(-120, 200), (250, 450)], None, (450, 1440)),  # the latest sleep to end in the morning
    ([(300, 840)], None, (840, 1440)),  # a sleep that ends 14 h in is the night
    ([(300, 841)], None, (360, 1440)),  # one minute later is not
    ([(-120, 450), (1799, 2200)], None, (450, 1799)),  # to bed 29:59: within six hours of the end
    ([(-120, 450), (1800, 2200)], None, (450, 1440)),  # six hours after the end is not
    ([(-120, 450), (840, 870), (1380, 1800)], None, (450, 1380)),  # a nap does not end it
    ([(-120, 450), (780, 820)], None, (450, 1440)),  # nor is a morning nap his waking
    ([(-120, 450), (840, 1020), (1380, 1800)], None, (450, 840)),  # three hours is a night
    ([(-120, 450), (1380, None)], 1600, (450, 1380)),  # a night still going ends it too
    ([(-120, 450)], 99999, (450, 1440)),  # a clock far past the day does not stretch it
    ([(-120, 450)], 1500, (450, 1500)),  # 01:00 next day, no night begun: he may be up (0313)
    ([(-120, 450)], 1799, (450, 1799)),  # still within six hours of midnight
    ([(-120, 450)], 1800, (450, 1440)),  # six hours past midnight with no night: to midnight
    ([(-120, 450), (1590, None)], 1780, (450, 1590)),  # to bed at 02:30, three hours asleep now
]


@pytest.mark.parametrize(("sleeps", "now", "expected"), WAKING_CASES)
def test_the_waking_window(
    sleeps: list[tuple[int, int | None]], now: int | None, expected: tuple[int, int],
) -> None:
    """Start at the latest morning waking, else 06:00; end at the first bedtime, else midnight.

    Only a sleep of three hours or more is a night; a nap is part of the waking day. Past
    midnight with no night begun, the day runs on to now for six hours (ADR 0313).
    """
    facts = fact_spans([_sleep(a, b) for a, b in sleeps])
    got = waking_window(facts, DAY_START, DAY_END, DAY_END * 10 if now is None else now * MIN)
    assert got == Waking(expected[0] * MIN, expected[1] * MIN)


# --- the cut ---------------------------------------------------------------------------------

WINDOW = Waking(480 * MIN, 1080 * MIN)  # 08:00-18:00

CUT_CASES = {
    "nothing at all": ([], [(480, 1080, ())]),
    "a stay with the day around it": (
        [_stay("s1", 540, 960)],
        [(480, 540, ()), (540, 960, ("s1",)), (960, 1080, ())],
    ),
    "a short move folds into the longer neighbour": (
        [_stay("s1", 540, 720), _item("m1", "move", 720, 725, activity="walking"),
         _stay("s2", 725, 960)],
        [(480, 540, ()), (540, 720, ("s1",)), (720, 960, ("m1", "s2")), (960, 1080, ())],
    ),
    "equal neighbours: the earlier takes the short piece": (
        [_stay("s1", 540, 600), _item("m1", "move", 600, 605, activity="walking"),
         _stay("s2", 605, 665)],
        [(480, 540, ()), (540, 605, ("s1", "m1")), (605, 665, ("s2",)), (665, 1080, ())],
    ),
    "short pieces inside one stay fold into the stay": (
        [_stay("s1", 540, 900), _item("c1", "call", 600, 612, app="Zoom"),
         _item("w1", "workout", 613, 620, activity="Run")],
        [
            (480, 540, ()), (540, 600, ("s1",)), (600, 613, ("s1", "c1")),
            (613, 900, ("s1", "w1")), (900, 1080, ()),
        ],
    ),
    "the neighbour with the same place wins over a longer one": (
        [_stay("s1", 540, 700), _stay("s2", 700, 1000),
         _item("w1", "workout", 695, 702, activity="Run")],
        [(480, 540, ()), (540, 700, ("s1", "w1")), (700, 1000, ("w1", "s2")), (1000, 1080, ())],
    ),
    "work under ten minutes does not cut": (
        [_stay("s1", 540, 900), _item("k1", "work", 600, 609, projects=[], apps=[])],
        [(480, 540, ()), (540, 900, ("s1", "k1")), (900, 1080, ())],
    ),
    "work of ten minutes cuts": (
        [_stay("s1", 540, 900), _item("k1", "work", 600, 610, projects=[], apps=[])],
        [
            (480, 540, ()), (540, 600, ("s1",)), (600, 610, ("s1", "k1")),
            (610, 900, ("s1",)), (900, 1080, ()),
        ],
    ),
    "a block of phone or tablet use of ten minutes cuts": (
        [_stay("s1", 540, 900), _item("p1", "screen", 600, 610, device="iPad", apps=[])],
        [
            (480, 540, ()), (540, 600, ("s1",)), (600, 610, ("s1", "p1")),
            (610, 900, ("s1",)), (900, 1080, ()),
        ],
    ),
    "a talk never cuts": (
        [_stay("s1", 540, 900), _item("t1", "talk", 600, 700, turns=9)],
        [(480, 540, ()), (540, 900, ("s1", "t1")), (900, 1080, ())],
    ),
    "a stay still going reaches the end": (
        [_stay("s1", 960, None)],
        [(480, 960, ()), (960, 1080, ("s1",))],
    ),
    "a stay across both edges is clipped": (
        [_stay("s1", 400, 1200)],
        [(480, 1080, ("s1",))],
    ),
    "the first piece folds into its only neighbour": (
        [_stay("s1", 485, None)],
        [(480, 1080, ("s1",))],
    ),
    "a piece nothing covers is a segment of its own": (
        [_stay("s1", 480, 700), _stay("s2", 800, 1080)],
        [(480, 700, ("s1",)), (700, 800, ()), (800, 1080, ("s2",))],
    ),
}


@pytest.mark.parametrize("name", list(CUT_CASES))
def test_the_cut_of_the_waking_window(name: str) -> None:
    """Segments as (start, end, fact ids) for each shape of day, and the invariants of any cut."""
    facts_in, expected = CUT_CASES[name]
    segments = cut(fact_spans(facts_in), WINDOW)
    assert [(s.start_ms // MIN, s.end_ms // MIN, s.item_ids) for s in segments] == expected
    assert [s.id for s in segments] == [f"doing-{s.start_ms}" for s in segments]
    _assert_a_whole_cut(segments, WINDOW)


def _assert_a_whole_cut(segments: list[Any], window: Waking) -> None:
    """Every waking minute in exactly one segment, none under ten minutes unless alone."""
    assert segments[0].start_ms == window.start_ms
    assert segments[-1].end_ms == window.end_ms
    for before, after in pairwise(segments):
        assert before.end_ms == after.start_ms
    assert sum(s.end_ms - s.start_ms for s in segments) == window.end_ms - window.start_ms
    if len(segments) > 1:
        assert all(s.end_ms - s.start_ms >= 10 * MIN for s in segments)


def _random_item(rng: random.Random, n: int) -> dict[str, Any]:  # noqa: PLR0911 — one per kind
    start = rng.randint(300, 1200)
    end = start + rng.choice([1, 5, 9, 10, 25, 90, 400])
    kind = rng.choice(["stay", "stay", "move", "work", "call", "workout", "code", "talk"])
    if kind == "stay":
        return _stay(f"i{n}", start, rng.choice([end, None]))
    if kind == "move":
        return _item(f"i{n}", "move", start, end, activity="walking")
    if kind == "work":
        return _item(f"i{n}", "work", start, end, projects=[], apps=[])
    if kind == "call":
        return _item(f"i{n}", "call", start, end, app="Zoom")
    if kind == "workout":
        return _item(f"i{n}", "workout", start, end, activity="Run")
    if kind == "code":
        return _item(f"i{n}", "code", start, end, title="t")
    return _item(f"i{n}", "talk", start, end, turns=1)


@pytest.mark.parametrize("seed", range(60))
def test_any_pile_of_facts_is_cut_into_a_whole_window(seed: int) -> None:
    """Seeded days of up to twelve facts: no gap, no overlap, nothing under ten minutes."""
    rng = random.Random(seed)  # noqa: S311 — a test's dice, not a secret
    items = [_random_item(rng, n) for n in range(rng.randint(0, 12))]
    window = Waking(rng.randint(360, 600) * MIN, rng.randint(700, 1300) * MIN)
    segments = cut(fact_spans(items), window)
    _assert_a_whole_cut(segments, window)
    served = serve(segments, fact_spans(items), None, [], items, window.end_ms)
    assert [(s["start_ms"], s["end_ms"]) for s in served] == [
        (g.start_ms, g.end_ms) for g in segments
    ]
    assert coverage(served, window)["waking_ms"] == window.end_ms - window.start_ms


def test_the_primary_place_span_is_the_one_that_covers_most() -> None:
    """A segment keeps the place span with the most of it, not just any it overlaps."""
    facts = fact_spans([
        _stay("s1", 540, 940), _item("m1", "move", 940, 960, activity="walking"),
    ])
    [_, segment, _] = cut(facts, WINDOW)[:3]
    assert segment.start_ms == 540 * MIN
    plain = [s for s in cut(facts, WINDOW) if s.start_ms == 940 * MIN]
    assert plain[0].place_span_id == "m1"
    assert segment.place_span_id == "s1"


def test_an_empty_window_has_no_segments() -> None:
    """Before he woke, today: nothing to cut."""
    assert cut([], Waking(300 * MIN, 300 * MIN)) == []


def test_a_waking_window_shorter_than_ten_minutes_is_one_segment() -> None:
    """There is no neighbour to fold it into."""
    [only] = cut(fact_spans([_stay("s1", 0, 100)]), Waking(480 * MIN, 485 * MIN))
    assert (only.start_ms, only.end_ms) == (480 * MIN, 485 * MIN)


# --- serving ---------------------------------------------------------------------------------

NOW = 1020  # 17:00
TODAY = Waking(480 * MIN, NOW * MIN)


def _world_items() -> list[dict[str, Any]]:
    return [
        _stay("s1", 540, 720, kept_place=SCHOOL),
        _stay("s2", 720, None, place="Corner Cafe"),
        _item("k1", "work", 600, 660, projects=[{"name": "jarvis", "seconds": 600}], apps=[]),
        _item("p1", "event", 900, 960, source="plan", title="Gym", place=None, private=False),
        _item("p2", "event", 1100, 1160, source="plan", title="Dinner", place=None, private=False),
        _item("c1", "event", 1200, 1260, source="mail", title="Dentist", confirmed=False),
        _item("e1", "event", 1300, 1330, title="Outlook meeting"),  # the calendar's: no source
        _item("pic", "picture", 700, 700, attachment="x"),
    ]


def _run(covers: int, spans: list[dict[str, Any]]) -> DoingRun:
    return DoingRun("doing-run:d:1", "d", draft=False, written_at="t", covers_until_ms=covers * MIN,
                    model="haiku", inputs_digest="x", spans=spans)


def _said(
    start: int, end: int, *, choice: str | None = None, text: str | None = None,
) -> DoingAnswer:
    return DoingAnswer(
        "doing-answer:d:1", "d", "doing-x", start * MIN, end * MIN, choice, text, None, None, None,
        "timeline", "t",
    )


def _run_span(span_id: str, start: int, end: int, **fields: Any) -> dict[str, Any]:  # noqa: ANN401
    return {
        "id": span_id, "start_ms": _m(start), "end_ms": _m(end), "kind": "doing",
        "title": "Study", "detail": "d", "category": "study", "basis": "inferred",
        "confidence": "medium", "clues": [{"kind": "words", "at_ms": _m(start), "text": "t"}],
        "question": None, "place_id": "place:uvic", **fields,
    }


def _served_spans(
    run: DoingRun | None = None, answers: list[DoingAnswer] | None = None,
    items: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    items = _world_items() if items is None else items
    facts = fact_spans(items)
    return serve(cut(facts, TODAY), facts, run, answers or [], items, NOW * MIN)


def test_each_stretch_holds_the_items_of_its_time_save_those_about_the_whole_day() -> None:
    """A moment is held by the stretch it falls in, one that lasts by each it overlaps."""
    items = [
        *_world_items(),
        _item("code", "code", 610, 700, title="a session"),  # held, though it never cuts the lane
        _item("pay", "payment", 545, 545),
        _item("rem", "reminder", 630, None, text="call mum"),  # a due time, not still going
        _item("tunes", "media", 650, 680, app="Music"),  # over two stretches: held by both
        _item("label", "label", 0, 1440, title="Thanksgiving"),
        _item("letter", "claude_day", 300, 1300, text="the day's chats"),
    ]
    held = {s["start_ms"] // MIN: s["item_ids"] for s in _served_spans(items=items)
            if s["basis"] != "planned"}
    assert held == {
        480: [],
        540: ["s1", "pay"],
        600: ["s1", "k1", "code", "rem", "tunes"],
        660: ["s1", "code", "tunes", "pic"],
        720: ["s2", "p1"],  # a plan item whose time has come is held too
    }


def test_without_a_run_the_program_cut_speaks_for_the_facts_alone() -> None:
    """A segment with facts is a doing span worded by its primary fact, low and observed."""
    spans = _served_spans()
    by_start = {s["start_ms"] // MIN: s for s in spans if s["basis"] != "planned"}
    assert sorted(by_start) == [480, 540, 600, 660, 720]
    assert by_start[480] == {
        "id": f"doing-{480 * MIN}", "lane": "doing", "kind": "unknown", "start_ms": 480 * MIN,
        "end_ms": 540 * MIN, "title": None, "detail": None, "category": "unknown",
        "basis": "observed", "confidence": "low", "confirmed": False, "clues": [],
        "question": None, "place_id": None, "item_ids": [],
    }
    assert by_start[540] | {"item_ids": []} == {  # a place alone colours it, says not what he did
        "id": f"doing-{540 * MIN}", "lane": "doing", "kind": "unknown", "start_ms": 540 * MIN,
        "end_ms": 600 * MIN, "title": None, "detail": None, "category": "study",
        "basis": "observed", "confidence": "low", "confirmed": False, "clues": [],
        "question": None, "place_id": "place:uvic", "item_ids": [],
    }
    assert by_start[600]["item_ids"] == ["s1", "k1"]  # the Mac use is on the segment it overlaps
    # the work block covers the whole segment: what he was doing, not where
    assert (by_start[600]["title"], by_start[600]["category"]) == ("jarvis", "work")
    assert by_start[600]["place_id"] == "place:uvic"  # the place stays available
    assert (by_start[660]["title"], by_start[660]["category"]) == (None, "study")
    assert (by_start[720]["title"], by_start[720]["category"]) == (None, "unknown")
    assert by_start[720]["end_ms"] == NOW * MIN


HOME_STAY = _stay("h", 400, 1200, kept_place=HOME)
JARVIS = _item("k", "work", 400, 1200, projects=[{"name": "jarvis"}], apps=[])
RIDE = _item("r", "ride", 400, 1200, route="99")
WALK = _item("m", "move", 400, 1200, activity="walking")
ZOOM = _item("z", "call", 400, 1200, app="Zoom")
IPAD = _item("p", "screen", 400, 1200, device="iPad", apps=[{"name": "抖音", "minutes": 600}])

DOMINANT_CASES = {
    # facts over the whole window (no edge inside it) to the (title, category) of its one segment
    "nothing": ([], (None, "unknown")),
    "a stay": ([_stay("h", 400, 1200)], (None, "unknown")),
    "a home stay alone": ([HOME_STAY], (None, "home")),
    "a school stay alone": ([_stay("h", 400, 1200, kept_place=SCHOOL)], (None, "study")),
    "a stay with a place text is not named for it": (
        [_stay("h", 400, 1200, place="Corner Cafe")], (None, "unknown"),
    ),
    "work names the segment by its project": ([HOME_STAY, JARVIS], ("jarvis", "work")),
    "work with only an app": (
        [HOME_STAY, _item("k", "work", 400, 1200, projects=[], apps=[{"name": "Chrome"}])],
        ("Chrome", "work"),
    ),
    "work comes before a ride": ([RIDE, JARVIS], ("jarvis", "work")),
    "a ride comes before a move and a call": ([RIDE, WALK, ZOOM], ("99", "commute")),
    "a walk has no title": ([WALK], (None, "commute")),
    "a drive outranks a call": (
        [_item("m", "move", 400, 1200, activity="automotive"), ZOOM], (None, "commute"),
    ),
    "a call names itself by its app": ([HOME_STAY, ZOOM], ("Zoom", "unknown")),
    "phone or tablet use names it by the app used most": ([HOME_STAY, IPAD], ("抖音", "unknown")),
    "work comes before phone or tablet use": ([HOME_STAY, JARVIS, IPAD], ("jarvis", "work")),
    "a call comes before phone or tablet use": ([HOME_STAY, ZOOM, IPAD], ("Zoom", "unknown")),
    "phone or tablet use comes before sleep": (
        [IPAD, _item("s", "sleep", 400, 1200, minutes=1)], ("抖音", "unknown"),
    ),
    "a workout": (
        [HOME_STAY, _item("w", "workout", 400, 1200, activity="Badminton")],
        ("Badminton", "exercise"),
    ),
    "sleep has no title": ([_item("s", "sleep", 400, 1200, minutes=1)], (None, "sleep")),
    "work under half of the segment does not name it": (
        [HOME_STAY, _item("k", "work", 500, 509, projects=[{"name": "jarvis"}], apps=[])],
        (None, "home"),
    ),
    "a talk never names it": (
        [HOME_STAY, _item("t", "talk", 400, 1200, turns=4)], (None, "home"),
    ),
}
NOTHING_SAYS_WHAT = frozenset({
    "nothing", "a stay", "a home stay alone", "a school stay alone",
    "a stay with a place text is not named for it",
    "work under half of the segment does not name it", "a talk never names it",
})
"""The cases where no fact says what he did: the span is unknown, a place at most its colour."""


@pytest.mark.parametrize("name", list(DOMINANT_CASES))
def test_a_program_segment_is_named_for_what_he_did_never_where(name: str) -> None:
    """Work, then a ride, a move, a call, sleep and workouts; a stay alone is a colour."""
    facts_in, (title, category) = DOMINANT_CASES[name]
    facts = fact_spans(facts_in)
    window = Waking(480 * MIN, 1080 * MIN)
    [span] = serve(cut(facts, window), facts, None, [], facts_in, window.end_ms)
    assert (span["title"], span["category"]) == (title, category)
    assert span["kind"] == ("unknown" if name in NOTHING_SAYS_WHAT else "doing")
    assert (span["basis"], span["confidence"]) == ("observed", "low")


def test_a_claude_code_session_running_on_its_own_is_not_him() -> None:
    """A session that goes on while he rests neither cuts the stay nor names it; his work does."""
    session = _item("c", "code", 400, 1200, title="Fix the cut")
    work = _item("k", "work", 600, 900, projects=[{"name": "jarvis"}], apps=[])
    items = [HOME_STAY, session, work]
    facts = his_facts(fact_spans(items))
    assert "c" not in {s["id"] for s in facts}
    window = Waking(480 * MIN, 1080 * MIN)
    served = serve(cut(facts, window), facts, None, [], items, window.end_ms)
    assert [(s["title"], s["category"]) for s in served] == [
        (None, "home"), ("jarvis", "work"), (None, "home"),
    ]


def test_a_long_stay_with_work_in_it_is_named_by_the_work_where_it_is() -> None:
    """The work cuts the stay in three: the pieces around it are a colour, the work is named."""
    items = [HOME_STAY, _item("k", "work", 600, 900, projects=[{"name": "jarvis"}], apps=[])]
    facts = fact_spans(items)
    window = Waking(480 * MIN, 1080 * MIN)
    served = serve(cut(facts, window), facts, None, [], items, window.end_ms)
    assert [(s["title"], s["category"], s["place_id"]) for s in served] == [
        (None, "home", "place:home"), ("jarvis", "work", "place:home"),
        (None, "home", "place:home"),
    ]


def test_a_run_speaks_up_to_what_it_covers_and_the_program_after() -> None:
    """The run's spans to ``covers_until_ms``; the segment across it is cut there."""
    run = _run(630, [
        _run_span("a", 480, 540, kind="unknown", title=None, category="unknown",
                  basis="observed", confidence="low", clues=[], place_id=None,
                  question={"id": "q1", "text": "What was this?", "options": ["Gym", "Walk"]}),
        _run_span("b", 540, 700),  # runs past what it covers: cut at 10:30
        _run_span("late", 640, 700),  # starts after what it covers: left out
    ])
    spans = [s for s in _served_spans(run) if s["basis"] != "planned"]
    assert [(s["id"], s["start_ms"] // MIN, s["end_ms"] // MIN) for s in spans] == [
        ("a", 480, 540), ("b", 540, 630), (f"doing-{630 * MIN}", 630, 660),
        (f"doing-{660 * MIN}", 660, 720), (f"doing-{720 * MIN}", 720, NOW),
    ]
    a, b = spans[0], spans[1]
    assert a["question"] == {"id": "q1", "text": "What was this?", "options": ["Gym", "Walk"]}
    assert (b["title"], b["detail"], b["basis"], b["confidence"], b["place_id"]) == (
        "Study", "d", "inferred", "medium", "place:uvic",
    )
    assert b["clues"] == [{"kind": "words", "at_ms": 540 * MIN, "text": "t"}]
    assert b["item_ids"] == ["s1", "k1"]
    assert spans[2]["item_ids"] == ["s1", "k1"]  # the piece after the run keeps its facts


def test_the_ids_of_the_program_cut_do_not_move_when_a_run_is_added() -> None:
    """After a run's reach the program's segments are the same ones, by where they start."""
    before = {s["id"] for s in _served_spans() if s["start_ms"] >= 660 * MIN}
    after = {s["id"] for s in _served_spans(_run(660, [_run_span("a", 480, 660)]))
             if s["start_ms"] >= 660 * MIN}
    assert before == after


@pytest.mark.parametrize(
    ("answers", "expected_title"),
    [
        ([_said(540, 600, text="Quiz")], "Quiz"),  # all of it
        ([_said(540, 570, choice="Gym")], "Gym"),  # half of it, from its start
        ([_said(545, 569, text="Quiz")], "Quiz"),  # five minutes before it go to the answer
        ([_said(500, 600, text="Quiz"), _said(540, 600, choice="Gym")], "Gym"),  # the latest wins
        ([_said(540, 600, text="Quiz"), _said(900, 940, text="Elsewhere")], "Quiz"),
        ([_said(0, 400, text="Before the day")], None),
    ],
)
def test_his_answer_wins_over_the_time_it_covers(
    answers: list[DoingAnswer], expected_title: str | None,
) -> None:
    """Title, ``stated``, ``high``, confirmed, no question; time it does not cover is untouched."""
    span = next(s for s in _served_spans(answers=answers) if s["start_ms"] == 540 * MIN)
    assert span["title"] == expected_title
    if expected_title is not None:
        assert (span["basis"], span["confidence"], span["confirmed"], span["question"]) == (
            "stated", "high", True, None,
        )
        assert span["category"] == "study"  # what the span was; he did not change it
    else:
        assert (span["basis"], span["confirmed"]) == ("observed", False)


def test_an_answer_is_carved_in_by_its_time_and_the_rest_stays_around_it() -> None:
    """Inside one long stretch: the time before and after stays what it was; a sliver goes."""
    run = _run(1020, [_run_span("a", 480, 1020)])
    spans = [s for s in _served_spans(run, [_said(600, 1015, text="Lay down")])
             if s["basis"] != "planned"]
    assert [(s["id"], s["start_ms"] // MIN, s["end_ms"] // MIN, s["title"]) for s in spans][:2] == [
        ("a", 480, 600, "Study"), (f"doing-{600 * MIN}", 600, 1020, "Lay down"),
    ]
    assert (spans[1]["confirmed"], spans[1]["basis"]) == (True, "stated")
    assert (bool(spans[0]["item_ids"]), spans[0]["confirmed"]) == (True, False)


def test_his_answer_lands_on_a_run_span_and_takes_its_question_away() -> None:
    """A run's span with a question, answered: the words are his and the question is gone."""
    asked = {"id": "q1", "text": "What?", "options": ["Gym"]}
    run = _run(660, [_run_span("a", 480, 660, question=asked)])
    [span, *_] = _served_spans(run, [_said(480, 660, choice="Gym")])
    assert (span["id"], span["title"], span["question"], span["confirmed"]) == (
        "a", "Gym", None, True,
    )


def test_an_unknown_stretch_he_answered_is_a_thing_he_did() -> None:
    """The kind follows his word: no longer unknown."""
    span = next(s for s in _served_spans(answers=[_said(480, 540, text="Slept in")])
                if s["start_ms"] == 480 * MIN)
    assert (span["kind"], span["title"], span["category"]) == ("doing", "Slept in", "unknown")


def test_the_plan_and_commitments_after_now_are_planned_spans() -> None:
    """Plan items and commitments (not the calendar's, not before now), kind planned."""
    planned = [s for s in _served_spans() if s["basis"] == "planned"]
    assert [(s["id"], s["start_ms"] // MIN, s["title"]) for s in planned] == [
        ("p2", 1100, "Dinner"), ("c1", 1200, "Dentist"),
    ]
    assert planned[0] == {
        "id": "p2", "lane": "doing", "kind": "planned", "start_ms": 1100 * MIN,
        "end_ms": 1160 * MIN, "title": "Dinner", "detail": None, "category": "unknown",
        "basis": "planned", "confidence": "high", "confirmed": False, "clues": [],
        "question": None, "place_id": None, "item_ids": [],
    }


def test_an_answer_does_not_touch_a_planned_span() -> None:
    """Answers are about what happened."""
    spans = _served_spans(answers=[_said(1100, 1160, text="Skipped")])
    assert next(s for s in spans if s["id"] == "p2")["title"] == "Dinner"


def test_served_spans_are_by_start_and_every_one_is_of_the_doing_lane() -> None:
    """Sorted, ids unique, closed vocabularies kept."""
    spans = _served_spans(_run(630, [_run_span("a", 480, 630)]), [_said(540, 560, text="x")])
    keys = [(s["start_ms"], s["id"]) for s in spans]
    assert keys == sorted(keys)
    assert len({s["id"] for s in spans}) == len(spans)
    for s in spans:
        assert s["lane"] == "doing"
        assert s["category"] in CATEGORIES
        assert s["basis"] in BASES
        assert s["confidence"] in CONFIDENCES


@pytest.mark.parametrize(
    "broken",
    [
        {"category": "napping"}, {"basis": "planned"}, {"confidence": "sure"}, {"kind": "planned"},
        {"clues": [{"kind": "tea leaves", "at_ms": 0, "text": "t"}]}, {"start_ms": None},
    ],
)
def test_a_run_that_breaks_the_vocabularies_is_not_served(broken: dict[str, Any]) -> None:
    """The lane refuses what the timeline cannot draw; ``read_doing`` then falls back."""
    items = _world_items()
    facts = fact_spans(items)
    run = _run(630, [_run_span("a", 480, 630, **broken)])
    with pytest.raises((ValueError, TypeError)):
        serve(cut(facts, TODAY), facts, run, [], items, NOW * MIN)


# --- coverage --------------------------------------------------------------------------------


def test_coverage_counts_the_waking_time_that_says_something() -> None:
    """Non-planned spans that say what he did, his answer included; a place alone does not."""
    items = _world_items()
    facts = fact_spans(items)
    served = serve(cut(facts, TODAY), facts, None, [], items, NOW * MIN)
    # only the work block (600-660) says what he did; the UVic stay around it is a colour
    assert coverage(served, TODAY) == {
        "waking_start_ms": 480 * MIN, "waking_end_ms": NOW * MIN, "waking_ms": 540 * MIN,
        "filled_ms": 60 * MIN,
    }
    cafe = [_said(720, 1020, text="Café")]
    answered = serve(cut(facts, TODAY), facts, None, cafe, items, NOW * MIN)
    assert coverage(answered, TODAY)["filled_ms"] == 60 * MIN + 300 * MIN  # his word fills it too
    assert coverage(served, Waking(600 * MIN, 660 * MIN))["filled_ms"] == 60 * MIN  # clipped


def test_planned_time_never_fills_the_day() -> None:
    """A plan is not a thing that happened."""
    plan = _item("p", "event", 540, 600, source="plan", title="x", place=None, private=False)
    spans = serve([], [], None, [], [plan], 500 * MIN)
    assert [s["basis"] for s in spans] == ["planned"]
    assert coverage(spans, Waking(480 * MIN, 1080 * MIN))["filled_ms"] == 0


# --- the tables ------------------------------------------------------------------------------


def test_the_newest_run_of_a_day_is_the_one_read_and_the_older_ones_stay(tmp_path: Path) -> None:
    """Append-only: latest per day wins, a draft is a run, another day is its own."""
    db = tmp_path / "memory.db"
    first = append_doing_run(db, day="2026-10-07", draft=True, covers_until_ms=100, model="haiku",
                             inputs_digest="a", spans=[{"id": "x"}])
    second = append_doing_run(db, day="2026-10-07", draft=False, covers_until_ms=200, model=None,
                              inputs_digest="b", spans=[{"id": "y", "title": "日本語"}])
    other = append_doing_run(db, day="2026-10-08", draft=True, covers_until_ms=300, model="m",
                             inputs_digest="c", spans=[])
    for run_id, day in ((first, "2026-10-07"), (second, "2026-10-07"), (other, "2026-10-08")):
        assert re.fullmatch(rf"doing-run:{day}:[0-9a-f]{{32}}", run_id)
    with closing(open_memory_db(db)) as conn:
        latest = latest_doing_run(conn, "2026-10-07")
        assert latest is not None
        assert (latest.id, latest.draft, latest.covers_until_ms, latest.model) == (
            second, False, 200, None,
        )
        assert (latest.inputs_digest, latest.spans) == ("b", [{"id": "y", "title": "日本語"}])
        assert datetime.fromisoformat(latest.written_at).tzinfo is not None
        draft = latest_doing_run(conn, "2026-10-08")
        assert draft is not None
        assert draft.draft is True
        assert latest_doing_run(conn, "2026-10-09") is None
        assert conn.execute("SELECT count(*) FROM doing_runs").fetchone()[0] == 3
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = 'doing_runs_day'",
        ).fetchone()
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1  # no schema bump


def _answer(db: Path, day: str, at: tuple[float, float] | None, start: int, **words: str) -> str:
    return append_doing_answer(
        db, day=day, span_id=f"doing-{start}", start_ms=start, end_ms=start + 10,
        choice=words.get("choice"), text=words.get("text"), lat=None if at is None else at[0],
        lng=None if at is None else at[1], place_id=None if at is None else "place:p",
        origin="timeline",
    )


def test_answers_are_appended_and_read_by_day_oldest_first(tmp_path: Path) -> None:
    """Never rewritten: the same stretch answered twice is two rows, the later wins by order."""
    db = tmp_path / "memory.db"
    one = _answer(db, "2026-10-07", (48.4, -123.3), 100, text="Class")
    two = _answer(db, "2026-10-07", None, 100, choice="Gym")
    _answer(db, "2026-10-08", None, 100, text="Other day")
    assert re.fullmatch(r"doing-answer:2026-10-07:[0-9a-f]{32}", one)
    with closing(open_memory_db(db)) as conn:
        got = doing_answers(conn, "2026-10-07")
        assert [a.id for a in got] == [one, two]
        assert got[0][2:11] == (
            "doing-100", 100, 110, None, "Class", 48.4, -123.3, "place:p", "timeline",
        )
        assert (got[1].choice, got[1].text, got[1].lat) == ("Gym", None, None)
        assert doing_answers(conn, "2026-10-09") == []
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = 'doing_answers_day'",
        ).fetchone()


def test_answers_near_a_point_are_the_newest_within_the_radius_before_a_time(
    tmp_path: Path,
) -> None:
    """For the next writer: where, how far, and only what began before the stretch it words."""
    db = tmp_path / "memory.db"
    here = (48.4620, -123.3120)
    _answer(db, "2026-10-01", here, 100, text="oldest here")
    _answer(db, "2026-10-02", (48.4622, -123.3121), 200, text="close by")  # about 25 m
    _answer(db, "2026-10-03", (48.4700, -123.3120), 300, text="900 m north")
    _answer(db, "2026-10-04", None, 400, text="nowhere")
    _answer(db, "2026-10-05", here, 5000, text="later than the stretch")
    _answer(db, "2026-10-06", here, 600, text="newest here")
    with closing(open_memory_db(db)) as conn:
        got = doing_answers_near(conn, here, 120.0, 1000)
        assert [a.text for a in got] == ["newest here", "close by", "oldest here"]
        assert [a.text for a in doing_answers_near(conn, here, 120.0, 1000, limit=2)] == [
            "newest here", "close by",
        ]
        assert [a.text for a in doing_answers_near(conn, here, 2000.0, 1000)][:2] == [
            "newest here", "900 m north",
        ]
        assert doing_answers_near(conn, here, 120.0, 100) == []  # nothing began before 100


# --- a store or a run that cannot be read ----------------------------------------------------


def _read(memory_db: Path | None, items: list[dict[str, Any]]) -> Any:  # noqa: ANN401 — a Doing
    return read_doing(memory_db, "2026-10-07", items, DAY_START, DAY_END, DAY_END + MIN)


def test_a_missing_or_unreadable_store_leaves_the_lane_program_cut(tmp_path: Path) -> None:
    """No file, a file that is not a database: the same lane as with no run, and no failure."""
    items = [_stay("s1", 400, 900)]
    plain = _read(None, items)
    assert plain.served
    assert all(s["basis"] == "observed" for s in plain.served)
    assert _read(tmp_path / "absent.db", items) == plain
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"this is not a database" * 100)
    assert _read(broken, items) == plain


def test_a_stored_run_that_cannot_be_served_leaves_the_lane_program_cut(tmp_path: Path) -> None:
    """A category outside the list: the run is passed over, his answers still count."""
    db = tmp_path / "memory.db"
    append_doing_run(db, day="2026-10-07", draft=False, covers_until_ms=900 * MIN, model="m",
                     inputs_digest="x", spans=[_run_span("a", 400, 900, category="napping")])
    append_doing_answer(
        db, day="2026-10-07", span_id="x", start_ms=400 * MIN, end_ms=900 * MIN, choice=None,
        text="Studied", lat=None, lng=None, place_id=None, origin="timeline",
    )
    items = [_stay("s1", 400, 900)]
    served = _read(db, items).served
    assert [(s["id"], s["title"], s["basis"]) for s in served if s["start_ms"] == 400 * MIN] == [
        (f"doing-{400 * MIN}", "Studied", "stated"),
    ]


def test_a_stored_run_is_served_through_the_reader(tmp_path: Path) -> None:
    """The end-to-end of the reader: table row to the lane."""
    db = tmp_path / "memory.db"
    append_doing_run(db, day="2026-10-07", draft=False, covers_until_ms=900 * MIN, model="m",
                     inputs_digest="x", spans=[_run_span("a", 360, 900)])
    doing = _read(db, [_stay("s1", 400, 900)])
    assert [(s["id"], s["title"]) for s in doing.served] == [
        ("a", "Study"), (f"doing-{900 * MIN}", None),
    ]
    assert doing.coverage["filled_ms"] == (900 - 360) * MIN
    assert [s["id"] for s in doing.spans] == ["a", "s1", f"doing-{900 * MIN}"]


# --- GET /inherent/day ------------------------------------------------------------------------


def _fold(src: LedgerSources) -> list[dict[str, Any]]:
    start, end = day_window(DAY, VANCOUVER)
    with closing(open_runtime_event_log(src.event_log)) as conn:
        return fold_day(conn, start, end, NOW_MS, DaySources(
            VANCOUVER, src.timesink, src.memory_db,
        )).items


def test_the_day_serves_spans_and_coverage_and_its_items_are_unchanged(tmp_path: Path) -> None:
    """Fact spans and doing spans together by start, coverage of the waking window."""
    src = ledger_world(tmp_path)
    stays_of(src)
    day = served_day(src)
    assert day["items"] == _fold(src)
    spans = day["spans"]
    keys = [(s["start_ms"], s["id"]) for s in spans]
    assert keys == sorted(keys)
    facts = [s for s in spans if s["lane"] != "doing"]
    assert {s["id"] for s in facts} >= {f"v{n}" for n in range(1, 8)}
    assert {(s["lane"], s["kind"]) for s in facts} >= {("place", "stay"), ("use", "work")}
    doing = [s for s in spans if s["lane"] == "doing"]
    assert doing[0]["start_ms"] == int(ledger_at(7, 6).timestamp()) * 1000  # waking from 06:00
    assert doing[-1]["end_ms"] == day_window(DAY, VANCOUVER)[1]
    waking = doing[-1]["end_ms"] - doing[0]["start_ms"]
    assert day["coverage"] == {
        "waking_start_ms": doing[0]["start_ms"], "waking_end_ms": doing[-1]["end_ms"],
        "waking_ms": waking,
        "filled_ms": 45 * MIN,  # only the Mac's work block (13:00-13:45) says what he did
    }
    worked = [s for s in doing if s["category"] != "unknown"]
    assert [(s["title"], s["category"]) for s in worked] == [("Chrome", "work")] * 2
    assert all(s["title"] is None for s in doing if s not in worked)  # no stay names a segment
    assert sum(s["end_ms"] - s["start_ms"] for s in doing) == waking


def test_kept_places_colour_the_stays_but_do_not_fill_the_day(tmp_path: Path) -> None:
    """A kept place gives its stays a category and itself; where he was is not what he did."""
    src = ledger_world(tmp_path)
    stays_of(src)
    places.save(src.memory_db, name="UVic", lat=1.0, lng=2.0, role="school", origin="user")
    day = served_day(src)
    doing = [s for s in day["spans"] if s["lane"] == "doing"]
    study = [s for s in doing if s["category"] == "study"]
    assert len(study) == 6  # the seventh stay is the Mac's work block, which names its segment
    assert len({s["place_id"] for s in study}) == 1
    assert study[0]["place_id"] is not None
    assert all(s["title"] is None for s in study)
    assert all(s["kind"] == "unknown" for s in study)
    assert day["coverage"]["filled_ms"] == 45 * MIN


def test_a_memory_db_made_before_the_tables_gets_them_without_a_version_bump(
    tmp_path: Path,
) -> None:
    """Additive: the day opens a store that lacks the tables and creates them empty."""
    src = ledger_world(tmp_path)
    stays_of(src)
    with closing(open_memory_db(src.memory_db)) as conn, conn:
        conn.execute("DROP TABLE doing_runs")
        conn.execute("DROP TABLE doing_answers")
    assert served_day(src)["spans"]
    with closing(open_memory_db(src.memory_db)) as conn:
        assert conn.execute("SELECT count(*) FROM doing_runs").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM doing_answers").fetchone()[0] == 0
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1


# --- POST /inherent/day/answer ----------------------------------------------------------------


def _span_at(src: LedgerSources, hour: int, minute: int = 0) -> dict[str, Any]:
    start = int(ledger_at(7, hour, minute).timestamp()) * 1000
    return next(s for s in served_day(src)["spans"] if s["id"] == f"doing-{start}")


@pytest.fixture
def world(tmp_path: Path) -> tuple[LedgerSources, Any]:
    """The stays of 10-07 and the route wired as the runtime wires it."""
    src = ledger_world(tmp_path)
    stays_of(src)
    answers = DoingAnswers(
        src.event_log, DaySources(VANCOUVER, src.timesink, src.memory_db),
        clock=lambda: NOW_MS / 1000,
    )
    return src, peers_with(tmp_path, doing_answer=functools.partial(_answer_doing, answers))


def _rows(src: LedgerSources) -> list[tuple[Any, ...]]:
    with closing(open_memory_db(src.memory_db)) as conn:
        return conn.execute(
            "SELECT day, span_id, start_ms, end_ms, choice, text, lat, lng, place_id, origin "
            "FROM doing_answers ORDER BY rowid",
        ).fetchall()


def test_his_words_answer_a_span_and_the_day_shows_them_afterwards(world: tuple[Any, Any]) -> None:
    """200 with the span as now served; the row has the span's own times and the stay's place."""
    src, peers = world
    span = _span_at(src, 9)
    assert span["title"] is None  # a stay says where, not what
    said = {"day": DAY_KEY, "span_id": span["id"], "text": "  Dentist  "}
    reply = peers.local.post(ANSWER, json=said)
    assert reply.status_code == 200
    body = reply.json()
    assert body["ok"] is True
    assert body["span"] == _span_at(src, 9)  # the page serves what the route said
    assert (body["span"]["title"], body["span"]["basis"], body["span"]["confidence"]) == (
        "Dentist", "stated", "high",
    )
    assert (body["span"]["confirmed"], body["span"]["question"]) == (True, None)
    assert body["span"]["start_ms"] == span["start_ms"]
    assert _rows(src) == [
        (DAY_KEY, span["id"], span["start_ms"], span["end_ms"], None, "Dentist", 1.0, 2.0, None,
         "timeline"),
    ]
    neighbours = [s for s in served_day(src)["spans"] if s["lane"] == "doing" and s["confirmed"]]
    assert [s["id"] for s in neighbours] == [span["id"]]  # nothing else took his word


def test_the_row_names_the_kept_place_of_the_stay(world: tuple[Any, Any]) -> None:
    """Latitude, longitude and place id come from the primary place span when it is a stay."""
    src, peers = world
    place = places.save(src.memory_db, name="UVic", lat=1.0, lng=2.0, role="school", origin="user")
    span = _span_at(src, 8)
    assert (span["title"], span["category"], span["place_id"]) == (None, "study", place.place.id)
    said = {"day": DAY_KEY, "span_id": span["id"], "text": "Lab"}
    assert peers.local.post(ANSWER, json=said).status_code == 200
    assert _rows(src)[0][6:9] == (1.0, 2.0, place.place.id)


def test_an_answer_about_a_stretch_with_no_stay_writes_no_place(world: tuple[Any, Any]) -> None:
    """The gap between two stays has no place span: the row has no coordinates."""
    src, peers = world
    gap = _span_at(src, 7, 30)
    assert gap["kind"] == "unknown"
    reply = peers.local.post(ANSWER, json={"day": DAY_KEY, "span_id": gap["id"], "text": "Walked"})
    assert reply.status_code == 200
    assert _rows(src)[0][6:9] == (None, None, None)
    assert reply.json()["span"]["kind"] == "doing"


def test_a_stretch_he_names_is_carved_in_and_the_rest_of_the_span_stays(
    world: tuple[Any, Any],
) -> None:
    """``start_ms``, ``end_ms`` and ``text``: 200 with the span that now holds his words."""
    src, peers = world
    span = _span_at(src, 9)
    first, last = span["start_ms"], span["start_ms"] + 20 * MIN
    assert span["end_ms"] - last >= 10 * MIN  # the rest is long enough to stay its own
    said = {"day": DAY_KEY, "start_ms": first, "end_ms": last, "text": " Lay down "}
    reply = peers.local.post(ANSWER, json=said)
    assert reply.status_code == 200
    held = reply.json()["span"]
    assert (held["id"], held["start_ms"], held["end_ms"], held["title"]) == (
        span["id"], first, last, "Lay down",
    )
    assert (held["basis"], held["confirmed"]) == ("stated", True)
    rest = next(s for s in served_day(src)["spans"] if s["id"] == f"doing-{last}")
    assert (rest["end_ms"], rest["confirmed"], rest["title"]) == (span["end_ms"], False, None)
    assert _rows(src) == [
        (DAY_KEY, "", first, last, None, "Lay down", 1.0, 2.0, None, "timeline"),
    ]


def test_a_stretch_that_is_not_one_is_422_and_nothing_is_kept(world: tuple[Any, Any]) -> None:
    """Mixed with a span or a choice, reversed, half given, outside the day, or after now."""
    src, peers = world
    span = _span_at(src, 9)
    first, last = span["start_ms"], span["end_ms"]
    day_start, day_end = day_window(DAY, VANCOUVER)
    for body in (
        {"start_ms": first, "end_ms": last, "text": "x", "span_id": span["id"]},
        {"start_ms": first, "end_ms": last, "choice": "x"},
        {"start_ms": last, "end_ms": first, "text": "x"},
        {"start_ms": first, "text": "x"},
        {"start_ms": first, "end_ms": last},
        {"start_ms": day_start - 60 * MIN, "end_ms": day_start + 60 * MIN, "text": "x"},
        {"start_ms": NOW_MS + 60 * MIN, "end_ms": day_end, "text": "x"},
        {"start_ms": "1", "end_ms": last, "text": "x"},
    ):
        reply = peers.local.post(ANSWER, json={"day": DAY_KEY, **body})
        assert reply.status_code == 422, body
    assert _rows(src) == []


def test_a_choice_must_be_an_option_of_the_spans_question(world: tuple[Any, Any]) -> None:
    """A run's question with its options: a listed one is kept as ``choice``, others are 422."""
    src, peers = world
    span = _span_at(src, 9)
    asked = {"id": "q1", "text": "What was this?", "options": ["Dentist", "Lunch"]}
    append_doing_run(
        src.memory_db, day=DAY_KEY, draft=False, covers_until_ms=span["end_ms"], model="m",
        inputs_digest="x", spans=[{
            "id": span["id"], "start_ms": span["start_ms"], "end_ms": span["end_ms"],
            "kind": "unknown", "title": None, "detail": None, "category": "unknown",
            "basis": "inferred", "confidence": "low", "clues": [], "question": asked,
            "place_id": None,
        }],
    )
    assert _span_at(src, 9)["question"] == asked
    bad = peers.local.post(ANSWER, json={"day": DAY_KEY, "span_id": span["id"], "choice": "Nap"})
    assert bad.status_code == 422
    assert _rows(src) == []
    good = peers.local.post(ANSWER, json={"day": DAY_KEY, "span_id": span["id"], "choice": "Lunch"})
    assert good.status_code == 200
    assert (good.json()["span"]["title"], good.json()["span"]["question"]) == ("Lunch", None)
    assert _rows(src)[0][4:6] == ("Lunch", None)
    pick = {"day": DAY_KEY, "span_id": span["id"], "choice": "Lunch"}
    again = peers.local.post(ANSWER, json=pick)
    assert again.status_code == 422  # answered: the question is gone
    assert peers.local.post(
        ANSWER, json={"day": DAY_KEY, "span_id": span["id"], "text": "Changed my mind"},
    ).json()["span"]["title"] == "Changed my mind"  # words can still be given: the latest wins


def test_a_choice_for_a_span_with_no_question_is_422(world: tuple[Any, Any]) -> None:
    """There is nothing to choose from."""
    src, peers = world
    reply = peers.local.post(
        ANSWER, json={"day": DAY_KEY, "span_id": _span_at(src, 9)["id"], "choice": "Dentist"},
    )
    assert reply.status_code == 422
    assert _rows(src) == []


def test_a_span_that_is_not_a_served_unplanned_doing_span_is_404(world: tuple[Any, Any]) -> None:
    """A fact span, a stay's item id, a made-up id, the right id on another day: nothing written."""
    src, peers = world
    span = _span_at(src, 9)
    for span_id, day in (
        ("v3", DAY_KEY), ("doing-1", DAY_KEY), ("nope", DAY_KEY), (span["id"], "2026-10-06"),
        (span["id"], "2026-10-08"),
    ):
        reply = peers.local.post(ANSWER, json={"day": day, "span_id": span_id, "text": "x"})
        assert reply.status_code == 404, (span_id, day)
    assert _rows(src) == []


def test_today_is_cut_at_now_and_its_plan_after_now_is_served_but_not_answerable(
    world: tuple[Any, Any],
) -> None:
    """The waking window of today ends at the clock; what is planned after it cannot be answered."""
    src, peers = world
    today = date(2026, 10, 8)  # NOW_MS is 15:00 of it
    with closing(open_event_log(src.event_log)) as conn:
        plan.draft(conn, today, [
            plan.PlanItem("p1", "Lunch", "fixed", "his", start="12:00", minutes=60),
            plan.PlanItem("p2", "Dinner", "fixed", "his", start="18:00", minutes=60),
        ], "digest", "model")
    day = served_day(src, today)
    spans = day["spans"]
    assert day["coverage"]["waking_end_ms"] == NOW_MS
    happened = [s for s in spans if s["lane"] == "doing" and s["basis"] != "planned"]
    assert max(s["end_ms"] for s in happened) == NOW_MS
    planned = [s for s in spans if s["basis"] == "planned"]
    assert [(s["id"], s["kind"], s["title"], s["confirmed"]) for s in planned] == [
        ("plan-2026-10-08-p2", "planned", "Dinner", False),
    ]
    for span_id in ("plan-2026-10-08-p2", "plan-2026-10-08-p1"):
        said = {"day": "2026-10-08", "span_id": span_id, "text": "x"}
        reply = peers.local.post(ANSWER, json=said)
        assert reply.status_code == 404
    assert _rows(src) == []


@pytest.mark.parametrize(
    "body",
    [
        {"span_id": "x", "text": "t"},
        {"day": DAY_KEY, "text": "t"},
        {"day": DAY_KEY, "span_id": "x"},  # neither
        {"day": DAY_KEY, "span_id": "x", "choice": "a", "text": "t"},  # both
        {"day": "today", "span_id": "x", "text": "t"},
        {"day": "2026-13-01", "span_id": "x", "text": "t"},
        {"day": "2026-10-7", "span_id": "x", "text": "t"},
        {"day": DAY_KEY, "span_id": "", "text": "t"},
        {"day": DAY_KEY, "span_id": "a b", "text": "t"},
        {"day": DAY_KEY, "span_id": "x" * 257, "text": "t"},
        {"day": DAY_KEY, "span_id": 3, "text": "t"},
        {"day": DAY_KEY, "span_id": "x", "text": ""},
        {"day": DAY_KEY, "span_id": "x", "text": "   "},
        {"day": DAY_KEY, "span_id": "x", "text": "t" * 201},
        {"day": DAY_KEY, "span_id": "x", "text": 7},
        {"day": DAY_KEY, "span_id": "x", "choice": ""},
        {"day": DAY_KEY, "span_id": "x", "choice": 7},
        {"day": DAY_KEY, "span_id": "x", "choice": None, "text": None},
    ],
)
def test_a_body_of_the_wrong_shape_is_a_422_and_writes_nothing(
    world: tuple[Any, Any], body: dict[str, Any],
) -> None:
    """Shape is checked before the day is read."""
    src, peers = world
    assert peers.local.post(ANSWER, json=body).status_code == 422
    assert _rows(src) == []


def test_the_text_is_trimmed_and_two_hundred_characters_fit(world: tuple[Any, Any]) -> None:
    """The bounds are on the trimmed text."""
    src, peers = world
    span_id = _span_at(src, 9)["id"]
    said = {"day": DAY_KEY, "span_id": span_id, "text": f" {'t' * 200}\n"}
    ok = peers.local.post(ANSWER, json=said)
    assert ok.status_code == 200
    assert _rows(src)[0][5] == "t" * 200


def test_the_answer_route_is_for_the_local_key_and_paired_devices_alone(
    world: tuple[Any, Any],
) -> None:
    """As the highlight route is guarded."""
    src, peers = world
    body = {"day": DAY_KEY, "span_id": _span_at(src, 9)["id"], "text": "Dentist"}
    refused_without_credentials(peers, ANSWER, body)
    assert _rows(src) == []
    assert peers.paired.post(ANSWER, json=body).status_code == 200


def test_a_brain_without_a_memory_store_has_no_answer_route(tmp_path: Path) -> None:
    """Not wired: the route is not there."""
    peers = peers_with(tmp_path)
    body = {"day": DAY_KEY, "span_id": "doing-1", "text": "x"}
    assert peers.local.post(ANSWER, json=body).status_code == 404


def test_the_day_over_http_carries_spans_and_coverage_as_json(tmp_path: Path) -> None:
    """``GET /inherent/day`` serialises the new fields."""
    src = ledger_world(tmp_path)
    stays_of(src)

    async def day_read(day: Any) -> dict[str, Any]:  # noqa: ANN401 — the route's argument
        return _read_day(src.event_log, VANCOUVER, src.memory_db, src.timesink, day, NOW_MS)

    peers = peers_with(tmp_path, day_read=day_read)
    got = peers.local.get(f"/inherent/day?date={DAY_KEY}")
    assert got.status_code == 200
    body = got.json()
    assert set(body["coverage"]) == {"waking_start_ms", "waking_end_ms", "waking_ms", "filled_ms"}
    assert {s["lane"] for s in body["spans"]} == {"doing", "place", "use"}
    assert body["items"] == served_day(src)["items"]
