"""ADR 0291: the plan watcher's pure core, over invented days (no key, no network, no model).

- ``apply_changes``: add, edit, move and drop, the list order kept (never sorted by time), done,
  a fixed item he may change, an edit that clears the leg, ops that do not read skipped with a note;
- ``budget``: the tasks left against the time left, legs counted, what is past not counted;
- ``progress``: every 2 hours after waking and when a timed item ends, never inside one;
- each notice kind fires at its threshold and not a minute before, and only once;
- ``parse_decision`` turns anything malformed into silence;
- ``facts_at`` reads only what the log held at that minute;
- a day with a plan draws its items on the day line;
- the replay script parses a plan file and walks a day with a stub decider.
"""

from __future__ import annotations

import json
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest

from jarvis.decision.day_plan import (
    build_decide_prompt,
    build_progress_prompt,
    parse_decision,
    parse_progress,
)
from jarvis.decision.plan_watch import budget, notice
from jarvis.runtime.now_block import plan_line
from jarvis.state import plan, things
from jarvis.state.day_line import DaySources, day_window, fold_day
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.state.places import Place
from jarvis.state.plan_budget import DAY_ENDS
from jarvis.state.plan_facts import (
    Commit,
    DayFacts,
    Here,
    Move,
    NotedThing,
    Stay,
    Wake,
    evidence,
    facts_at,
)

if TYPE_CHECKING:
    from pathlib import Path

ZONE = ZoneInfo("America/Vancouver")
DAY = date(2026, 10, 16)
HOME = Place("pl1", "Home", "home", 48.46, -123.31, 100, None, "test", "", "")


def _at(clock: str) -> datetime:
    return datetime.fromisoformat(f"{DAY.isoformat()}T{clock}:00").replace(tzinfo=ZONE)


def _ms(clock: str) -> int:
    return int(_at(clock).timestamp() * 1000)


def _item(item_id: str = "p1", **fields: Any) -> plan.PlanItem:  # noqa: ANN401
    base: dict[str, Any] = {
        "item_id": item_id, "title": "Write the report", "firmness": "movable", "source": "goal",
        "start": "09:00", "minutes": 60,
    }
    return plan.PlanItem(**(base | fields))


def _plan(*items: plan.PlanItem) -> plan.Plan:
    return plan.Plan(DAY, items)


# --- apply_changes -------------------------------------------------------------------------


def test_apply_changes_adds_edits_and_drops_and_a_fixed_item_may_change() -> None:
    """Each op does its part; a new id is made; moving or re-placing an item clears its leg."""
    leg = {"from": "Home", "leave_at": "08:20"}
    items = [_item("p1", firmness="fixed", source="item:thing-c1", route=leg), _item("p2")]
    new, notes = plan.apply_changes(items, [
        {"op": "add", "item": {"title": "Walk", "start": "08:00", "minutes": 30}},
        {"op": "edit", "id": "p2", "title": "Write", "private": True},
        {"op": "edit", "id": "p1", "start": "09:30"},
        {"op": "drop", "id": "p2"},
    ])
    assert notes == []
    assert [(i.item_id, i.title, i.start) for i in new] == [
        ("p1", "Write the report", "09:30"), ("p3", "Walk", "08:00"),
    ]  # the order is the plan's: nothing sorts by time
    assert new[1].firmness == "movable"
    assert new[0].route is None  # the old leg no longer fits the new time
    kept, _ = plan.apply_changes(items, [{"op": "edit", "id": "p1", "why": "because"}])
    assert kept[0].route == leg  # a change that moves nothing keeps the leg


def test_apply_changes_adds_after_moves_and_marks_done_keeping_the_order() -> None:
    """Add after an id, move after an id or first, edit done; an old payload reads as not done."""
    items = [_item("p1", start=None), _item("p2", start=None), _item("p3", start="14:00")]
    new, notes = plan.apply_changes(items, [
        {"op": "add", "item": {"title": "Call", "minutes": 10}, "after": "p1"},
        {"op": "move", "id": "p3", "after": None},
        {"op": "move", "id": "p1", "after": "p2"},
        {"op": "edit", "id": "p4", "done": True},
    ])
    assert notes == []
    assert [i.item_id for i in new] == ["p3", "p4", "p2", "p1"]
    assert [i.done for i in new] == [False, True, False, False]
    assert (new[1].title, new[1].start, new[1].source) == ("Call", None, "his")
    assert plan.diff(items, new) == "+ Call; order changed"
    assert plan.diff(items[:1], [replace(items[0], done=True)]) == "Write the report done"
    appended, _ = plan.apply_changes(items, [{"op": "add", "item": {"title": "Last"}}])
    assert appended[-1].title == "Last"
    old = items[0].payload()
    del old["done"]
    assert plan.PlanItem.from_payload(old).done is False
    for bad in (
        {"op": "move", "id": "p1"},
        {"op": "move", "id": "p1", "after": "p1"},
        {"op": "move", "id": "p1", "after": "nope"},
        {"op": "add", "item": {"title": "x"}, "after": "nope"},
        {"op": "edit", "id": "p1", "done": "yes"},
    ):
        assert plan.apply_changes(items, [bad])[0] == items
        assert len(plan.apply_changes(items, [bad])[1]) == 1, bad


@pytest.mark.parametrize(
    "op",
    [
        {"op": "edit", "id": "nope", "title": "x"},
        {"op": "edit", "id": "p1", "route": {}},
        {"op": "edit", "id": "p1", "item_id": "p9"},
        {"op": "edit", "id": "p1", "start": "25:00"},
        {"op": "edit", "id": "p1", "minutes": "60"},
        {"op": "add", "item": {"start": "08:00"}},
        {"op": "add", "item": {"title": "x", "firmness": "soft"}},
        {"op": "move", "id": "p1"},
        {"op": "move", "id": "p1", "after": "nope"},
        "drop p1",
    ],
)
def test_apply_changes_skips_an_op_that_does_not_read_and_says_so(op: object) -> None:
    """A bad op leaves the items as they were and is noted; the good ops still apply."""
    items = [_item("p1")]
    new, notes = plan.apply_changes(items, [op, {"op": "drop", "id": "p1"}])
    assert new == []
    assert len(notes) == 1
    assert plan.apply_changes(items, "drop everything") == (items, ["changes is not a list"])


# --- the notices ---------------------------------------------------------------------------

LEG = {"from": "Home", "leave_at": "09:20", "line": "28", "arrive_at": "09:43"}


def _stay(place: str, since: str, until: str | None = None) -> Stay:
    return Stay(_ms(since), _ms(until) if until else None, place, place, "", 48.46, -123.31)


def _here(place: str, since: str) -> Here:
    return Here(place, place, "", 48.46, -123.31, _ms(since), None)


# (kind, items, facts, the minute it fires, the minute before)
CASES: dict[str, tuple[list[plan.PlanItem], dict[str, Any], str, str]] = {
    "not_up": ([_item(start="08:30")], {}, "09:00", "08:59"),
    "outing_soon": (
        [_item(start="10:00", place="Library", route=LEG)], {}, "08:55", "08:54",
    ),
    "left_late": (
        [_item(start="10:00", place="Library", route=LEG)],
        {"wake": Wake("focus_off", _ms("07:00")), "here": _here("Home", "07:30")},
        "09:23", "09:22",
    ),
    "item_due": ([_item(start="10:00")], {"wake": Wake("alarm_stopped", 1)}, "10:00", "09:59"),
    "stayed_longer": (
        [_item(start="10:00", place="Library"), _item("p2", start="11:45", place="Gym")],
        {"wake": Wake("alarm_stopped", 1), "here": _here("Library", "10:05")}, "11:15", "11:14",
    ),
    "left_early": (
        [_item(start="10:00", place="Library")],
        {"wake": Wake("alarm_stopped", 1), "moves": (Move(_ms("10:05"), None, "walking"),)},
        "10:15", "10:14",
    ),
}


@pytest.mark.parametrize("kind", list(CASES))
def test_a_notice_fires_at_its_threshold_and_not_a_minute_before(kind: str) -> None:
    """The thresholds of ADR 0291: not_up 30 min, outing_soon 25 min, left_late 3 min, ..."""
    items, kwargs, fires, before = CASES[kind]
    facts = DayFacts(
        0, kwargs.get("wake"), kwargs.get("here"), kwargs.get("stays", ()),
        kwargs.get("moves", ()), (), (),
    )
    held = _plan(*items)
    keys = {n.kind: n.key for n in notice(held, facts, _at(fires), set())}
    assert kind in keys
    assert kind not in {n.kind for n in notice(held, facts, _at(before), set())}
    assert notice(held, facts, _at(fires), set(keys.values())) == []  # noticed once


def test_item_due_asks_only_of_a_movable_item_and_carries_what_he_is_doing() -> None:
    """A fixed item is not due-noticed; the notice carries the work state's activities."""
    facts = DayFacts(0, Wake("focus_off", 1), None, (), (), ("reviewing a PR",), ())
    held = _plan(_item("p1", start="10:00"), _item("p2", firmness="fixed", start="10:00"))
    [one] = [n for n in notice(held, facts, _at("10:05"), set()) if n.kind == "item_due"]
    assert (one.key, one.facts["doing"]) == ("item_due:p1", ["reviewing a PR"])


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("item:thing-c1", id="item"),
        pytest.param("commitment:commitment-c1", id="item-of-a-plan-before-the-ledger"),
    ],
)
def test_woke_and_new_thing_and_no_wake_before_noon(source: str) -> None:
    """A wake is noticed; a thing already in the plan is not new; a late first item is not."""
    mine = NotedThing("thing-c1", "Dentist", "14:00", "", private=False, noted_at_ms=1)
    other = NotedThing("thing-c2", "Call mum", "18:00", "", private=False, noted_at_ms=1)
    facts = DayFacts(0, Wake("mac_active", _ms("07:10")), None, (), (), (), (mine, other))
    held = _plan(_item("p1", source=source, firmness="fixed", start="14:00"))
    got = {n.key: n for n in notice(held, facts, _at("07:30"), set())}
    assert set(got) == {"woke", "new_commitment:thing-c2"}
    assert got["woke"].facts == {"signal": "mac_active", "at": "07:10"}
    assert got["new_commitment:thing-c2"].facts == {
        "title": "Call mum", "start": "18:00", "place": "", "private": False,
    }
    late = _plan(_item("p1", start="13:00"))
    assert notice(late, DayFacts(0, None, None, (), (), (), ()), _at("15:00"), set()) == []


# --- the budget and the progress notice -----------------------------------------------------


def _task(item_id: str, minutes: int = 60, **fields: Any) -> plan.PlanItem:  # noqa: ANN401
    return _item(item_id, title=f"Task {item_id}", start=None, minutes=minutes, **fields)


MEETING = _item(
    "p9", title="Group meeting", firmness="fixed", start="14:00", minutes=60,
    route={"from": "Home", "leave_at": "13:20", "arrive_at": "13:55"},
)
PAST = _item("p8", title="Standup", firmness="fixed", start="08:00", minutes=60)


def test_budget_sets_the_tasks_left_against_the_free_time_with_legs_counted() -> None:
    """Tasks not done in order; the fixed time ahead (item and leg) comes out, what is past not."""
    held = _plan(_task("p1", 60), _task("p2", 30, done=True), _task("p3", 90), MEETING, PAST)
    got = budget(held, _at("10:00"), day_ends=time(18, 0))
    assert [t.item_id for t in got.tasks] == ["p1", "p3"]
    assert (got.remaining_min, got.fixed_min, got.free_min) == (150, 95, 385)  # 480 - 60 - 35
    assert (got.fits, got.next_task, got.next_timed) == (True, got.tasks[0], MEETING)
    tight = budget(held, _at("10:00"), day_ends=time(12, 0))
    assert (tight.free_min, tight.fits) == (25, False)  # 120 - 95
    underway = budget(held, _at("14:30"), day_ends=time(18, 0))
    assert (underway.fixed_min, underway.free_min, underway.next_timed) == (30, 180, None)
    skipped = budget(
        _plan(_task("p1"), replace(MEETING, done=True)), _at("10:00"), day_ends=time(18, 0),
    )
    assert (skipped.fixed_min, skipped.free_min) == (0, 480)  # a meeting marked done is not ahead
    over = budget(held, _at("13:00"), day_ends=time(14, 30))
    assert (over.free_min, over.fits) == (0, False)  # 90 - 95 is not a negative amount of time
    finished = budget(_plan(_task("p1", done=True), MEETING), _at("10:00"), day_ends=time(18, 0))
    assert (finished.next_task, finished.remaining_min, finished.fits) == (None, 0, True)


def test_progress_comes_every_two_hours_and_after_a_timed_item_never_inside_one() -> None:
    """Each key once; the 11:00 checkpoint falls inside the 11:00 meeting and its end covers it."""
    meeting = _item("p9", title="Group meeting", firmness="fixed", start="11:00", minutes=60)
    held = _plan(_task("p1"), _task("p2", 30), meeting)
    facts = DayFacts(0, Wake("focus_off", _ms("07:00")), None, (), (), ("reviewing a PR",), (),
                     (Commit(_ms("08:10"), "jarvis", "fix the thing"),))
    seen: set[str] = set()
    fired: list[tuple[str, str]] = []
    at = _at("06:00")
    while at < _at("16:00"):
        for one in notice(held, facts, at, seen):
            seen.add(one.key)
            if one.kind != "woke":
                assert one.kind == "progress"  # an untimed task is never item_due
                fired.append((f"{at:%H:%M}", one.key))
        at += timedelta(minutes=30)
    assert fired == [
        ("09:00", "progress:1"), ("12:00", "progress:after:p9"), ("13:00", "progress:3"),
        ("15:00", "progress:4"),
    ]
    first = notice(held, facts, _at("09:00"), set())[-1]
    assert first.facts["tasks"] == [
        {"id": "p1", "title": "Task p1", "minutes": 60, "done": False},
        {"id": "p2", "title": "Task p2", "minutes": 30, "done": False},
    ]
    assert (first.facts["remaining_min"], first.facts["free_min"], first.facts["fits"]) == (
        90, 810, True,  # 09:00 to 23:30 is 870, less the 60-minute meeting
    )
    assert first.facts["next_task"] == {"id": "p1", "title": "Task p1", "minutes": 60}
    assert first.facts["doing"] == ["reviewing a PR"]
    assert first.facts["commits"] == [{"at": "08:10", "repo": "jarvis", "subject": "fix the thing"}]
    after = [n for n in notice(held, facts, _at("12:00"), set()) if n.kind == "progress"]
    assert [(n.key, n.facts["after"]["title"]) for n in after] == [
        ("progress:after:p9", "Group meeting"),
    ]


def test_progress_needs_a_wake_and_a_task_not_done() -> None:
    """Before any sign of waking, or with every task done, there is nothing to check."""
    asleep = DayFacts(0, None, None, (), (), (), ())
    awake = DayFacts(0, Wake("focus_off", _ms("07:00")), None, (), (), (), ())

    def kinds(held: plan.Plan, facts: DayFacts) -> set[str]:
        return {n.kind for n in notice(held, facts, _at("09:00"), set())}

    assert "progress" not in kinds(_plan(_task("p1")), asleep)
    assert "progress" in kinds(_plan(_task("p1")), awake)
    assert "progress" not in kinds(_plan(_task("p1", done=True), MEETING), awake)


def test_no_progress_check_at_or_after_the_days_end_or_with_no_time_left() -> None:
    """The 17:00 checkpoint comes; 18:00, 19:00 and the small hours do not; a booked day none."""
    awake = DayFacts(0, Wake("focus_off", _ms("07:00")), None, (), (), (), ())
    held = _plan(_task("p1"))

    def keys(plan_: plan.Plan, clock: str, ends: time) -> list[str]:
        return [n.key for n in notice(plan_, awake, _at(clock), set(), day_ends=ends)
                if n.kind == "progress"]

    assert keys(held, "17:00", time(18, 0)) == ["progress:5"]
    assert [keys(held, c, time(18, 0)) for c in ("18:00", "19:00", "23:25")] == [[], [], []]
    assert [keys(held, c, DAY_ENDS) for c in ("23:30", "01:25", "03:25")] == [[], [], []]
    booked = _plan(_task("p1"), _item("p9", firmness="fixed", start="13:00", minutes=600))
    assert keys(booked, "11:00", time(14, 0)) == []  # 13:00 to 14:00 is spoken for, and more


def _work(conn: Any, at: str, *texts: str) -> None:  # noqa: ANN401
    acts = [{"text": t} for t in texts]
    emit_event(conn, type="work_state.revised", ts_epoch_ms=_ms(at), payload={
        "item": {"activities": acts}, "trigger": "auto"})


def _said(conn: Any, kind: str, at: str, words: str) -> None:  # noqa: ANN401
    emit_event(conn, type=kind, ts_epoch_ms=_ms(at), payload={"transcript": words, "turn_id": "t"})


def test_evidence_is_what_he_did_since_the_previous_check_each_activity_once(
    tmp_path: Path,
) -> None:
    """Each window sees its own events; an activity shows at its first time; repeats fold."""
    with closing(_log(tmp_path / "events.db")) as conn:
        _work(conn, "06:00", "asleep at the desk")  # before the first window
        _work(conn, "08:00", "editing the report", "reading mail")
        _work(conn, "08:30", "editing the report", "  writing   tests ")
        _work(conn, "09:30", "writing tests")
        _work(conn, "11:30", "after the window")
        _said(conn, "utterance.received", "08:10", "sent the report")
        _said(conn, "surface.user_intent", "08:10", "sent the report")  # the same turn, twice
        _said(conn, "surface.user_intent", "09:40", "x" * 300)
        emit_event(conn, type="project.commit_seen", ts_epoch_ms=_ms("08:20"), payload={
            "repo_path": "/p/jarvis", "commit_sha": "a1", "subject": "early",
            "committed_at_ms": _ms("08:20"), "actor": "observer"})
        emit_event(conn, type="project.commit_seen", ts_epoch_ms=_ms("09:50"), payload={
            "repo_path": "/p/jarvis", "commit_sha": "b2", "subject": "late",
            "committed_at_ms": _ms("09:50"), "actor": "observer"})
        first = evidence(conn, _ms("07:00"), _ms("09:00"), ZONE)
        second = evidence(conn, _ms("09:00"), _ms("11:00"), ZONE)
    assert first["screen"] == [
        {"at": "08:00", "text": "editing the report"}, {"at": "08:00", "text": "reading mail"},
        {"at": "08:30", "text": "writing tests"},
    ]
    assert first["words"] == [{"at": "08:10", "text": "sent the report"}]
    assert [c["subject"] for c in first["commits"]] == ["early"]
    assert (first["since"], first["since_ms"], first["until_ms"]) == ("07:00", _ms("07:00"),
                                                                      _ms("09:00"))
    assert second["screen"] == [{"at": "09:30", "text": "writing tests"}]
    assert [(w["at"], len(w["text"])) for w in second["words"]] == [("09:40", 200)]  # clipped
    assert [c["subject"] for c in second["commits"]] == ["late"]


def test_evidence_keeps_the_latest_forty_activities_and_twenty_words(tmp_path: Path) -> None:
    """Over the cap the oldest go; what stays keeps its order."""
    with closing(_log(tmp_path / "events.db")) as conn:
        for n in range(45):
            _work(conn, f"08:{n:02d}", f"activity {n}")
        for n in range(25):
            _said(conn, "utterance.received", f"09:{n:02d}", f"words {n}")
        got = evidence(conn, _ms("07:00"), _ms("10:00"), ZONE)
    assert (got["screen"][0]["text"], got["screen"][-1]["text"], len(got["screen"])) == (
        "activity 5", "activity 44", 40,
    )
    assert (got["words"][0]["text"], got["words"][-1]["text"], len(got["words"])) == (
        "words 5", "words 24", 20,
    )


# --- the decision --------------------------------------------------------------------------


def test_parse_decision_keeps_a_good_answer_and_turns_a_bad_one_to_silence() -> None:
    """Untrusted JSON: anything that does not read is silent, with a note."""
    held = _plan(_item("p1"))
    move = {"op": "edit", "id": "p1", "start": "10:00"}
    fenced = "```json\n" + json.dumps({"act": "tell", "say": "Leave soon.", "why": "w"}) + "\n```"
    assert parse_decision(fenced, held).say == "Leave soon."
    adjusted = parse_decision(
        json.dumps({"act": "adjust", "say": "Moved it.", "changes": [move]}), held,
    )
    assert (adjusted.act, adjusted.changes) == ("adjust", (move,))
    told = parse_decision(json.dumps({"act": "tell", "say": "x", "changes": [move]}), held)
    assert (told.act, told.changes) == ("tell", ())
    for bad in (
        "not json",
        '["tell"]',
        json.dumps({"act": "shout", "say": "x"}),
        json.dumps({"act": "tell"}),
        json.dumps({"act": "tell", "say": "x" * 400}),
        json.dumps({"act": "adjust", "say": "x"}),
        json.dumps({"act": "adjust", "say": "x", "changes": [{"op": "drop", "id": "p9"}]}),
        json.dumps({"act": "adjust", "say": "x", "changes": [move, {"op": "drop", "id": "p9"}]}),
    ):
        got = parse_decision(bad, held)
        assert (got.act, got.say, got.changes) == ("silent", "", ()), bad
        assert got.notes, bad


def test_parse_progress_keeps_what_reads_and_drops_the_rest_with_a_note() -> None:
    """Untrusted: unknown, timed and finished ids, and minutes out of range are dropped."""
    held = _plan(
        _task("p1", 60), _task("p2", 40), _task("p3", 30, done=True), _task("p4", 90), _task("p5"),
        MEETING,
    )
    ok = parse_progress(
        json.dumps({"done": ["p1", "p1"], "left": {"p2": 15, "p4": 90}, "why": "he said so"}), held,
    )
    assert (ok.done, ok.left, ok.why, ok.notes) == (("p1",), {"p2": 15}, "he said so", ())
    assert ok.changes() == [
        {"op": "edit", "id": "p1", "done": True}, {"op": "edit", "id": "p2", "minutes": 15},
    ]  # p4 left as many minutes as planned: nothing to change
    bad = parse_progress(json.dumps({
        "done": ["p9", "p3", "x", 7],  # a timed item, a finished one, unknown, not an id
        "left": {"p1": 0, "p2": -5, "p4": 120, "p5": "ten", "p9": 10, "p3": 5},
    }), held)
    assert (bad.done, bad.left, len(bad.notes)) == ((), {}, 10)
    timed = parse_progress(json.dumps({"done": ["p9"], "left": {"p9": 10, "p1": 20, "p2": 10}}),
                           held)
    assert (timed.done, timed.left) == ((), {"p1": 20, "p2": 10})
    assert any("p9" in n for n in timed.notes)
    both = parse_progress(json.dumps({"done": ["p1"], "left": {"p1": 10}}), held)
    assert (both.done, both.left, len(both.notes)) == (("p1",), {}, 1)  # done wins
    for text in ("not json", '["p1"]', json.dumps({"done": "p1", "left": ["p2"]})):
        got = parse_progress(text, held)
        assert (got.done, got.left, got.changes()) == ((), {}, []), text
        assert got.notes, text


def test_the_progress_prompt_lists_the_tasks_in_order_and_the_evidence() -> None:
    """Timed items are not in the list; the evidence carries its span and the three kinds."""
    held = _plan(_task("p1"), MEETING, _task("p2", 30, done=True))
    facts = DayFacts(0, Wake("focus_off", _ms("07:00")), None, (), (), (), ())
    [one] = [n for n in notice(held, facts, _at("09:00"), set()) if n.kind == "progress"]
    one = replace(one, facts=one.facts | {"evidence": {
        "since": "07:00", "since_ms": 1, "until_ms": 2, "screen": [{"at": "08:00", "text": "x"}],
        "words": [], "commits": [],
    }})
    _, user = build_progress_prompt(held, one, _at("09:00"), zone=ZONE)
    body = json.loads(user)
    assert (body["task"], body["now"]) == ("progress", "09:00")
    assert [t["id"] for t in body["tasks"]] == ["p1", "p2"]
    assert body["tasks"][1] == {"id": "p2", "title": "Task p2", "minutes": 30, "done": True}
    assert body["evidence"] == {
        "screen": [{"at": "08:00", "text": "x"}], "words": [], "commits": [], "since": "07:00",
    }


def test_the_decide_prompt_carries_the_notice_the_plan_and_what_she_told() -> None:
    """The skill is the system prompt; the user message is JSON; silent decisions are not 'told'."""
    held = _plan(_item("p1", private=True))
    facts = DayFacts(0, Wake("focus_off", _ms("07:00")), _here("Home", "07:00"), (), (), (), ())
    [one] = notice(held, facts, _at("07:30"), set())
    told = [
        plan.Decided("a", "tell", "Heads up.", "", "m", _ms("07:10")),
        plan.Decided("b", "silent", "", "", "m", _ms("07:20")),
    ]
    system, user = build_decide_prompt(held, one, facts, _at("07:30"), told, zone=ZONE)
    body = json.loads(user)
    assert "## Task: notice" in system
    assert (body["task"], body["now"], body["notice"]["key"]) == ("notice", "7:30 AM", "woke")
    assert body["told_today"] == [{"at": "7:10 AM", "say": "Heads up."}]
    assert body["plan"][0]["private"] is True
    assert body["facts"]["wake"] == {"kind": "focus_off", "at": "07:00"}


# --- the facts, as of a minute -------------------------------------------------------------


def _log(path: Path) -> Any:  # noqa: ANN401
    return open_event_log(path)


def test_facts_at_reads_only_what_the_log_held_at_that_minute(tmp_path: Path) -> None:
    """Replaying 08:05, 08:20 and 09:00 of one day sees the stay, the wake and the thing."""
    with closing(_log(tmp_path / "events.db")) as conn:
        emit_event(
            conn, type="phone.visit_observed", ts_epoch_ms=_ms("07:50"), ingestion_node="phone",
            payload={"lat": 48.46, "lng": -123.31, "accuracy_m": 30.0,
                     "arrived_at_ms": _ms("07:45")},
        )
        emit_event(
            conn, type="phone.state_observed", ts_epoch_ms=_ms("08:10"), ingestion_node="phone",
            payload={"signal": "focus", "value": "off"},
        )
        emit_event(
            conn, type="phone.state_observed", ts_epoch_ms=_ms("03:00"), ingestion_node="phone",
            payload={"signal": "alarm", "value": "stopped"},  # the night: not a wake
        )
        emit_event(
            conn, type="utterance.received", ts_epoch_ms=_ms("08:30"),
            payload={"transcript": "morning", "turn_id": "t1"},
        )
        things.add(
            conn, things.Thing(things.new_id(), "Dentist", "go", start_at=_at("14:00").isoformat()),
            by="her", evidence="said so", zone=ZONE, now=_at("06:00"),
        )
        before = facts_at(conn, DAY, _ms("08:05"), ZONE, [HOME])
        woke = facts_at(conn, DAY, _ms("08:20"), ZONE, [HOME])
        later = facts_at(conn, DAY, _ms("09:00"), ZONE, [HOME])
    assert before.wake is None
    assert before.here is not None
    assert (before.here.place, before.here.since_ms) == ("Home", _ms("07:45"))
    assert woke.wake == Wake("focus_off", _ms("08:10"))
    assert later.wake == woke.wake  # the earliest stays the first
    assert [c.title for c in later.things] == ["Dentist"]
    assert before.payload(ZONE)["here"]["since"] == "07:45"


def _on(days: int, clock: str) -> str:
    return (_at(clock) + timedelta(days=days)).isoformat()


def test_the_things_noted_today_are_those_added_for_today_that_still_stand(
    tmp_path: Path,
) -> None:
    """ADR 0299: a thing added today for today, by anyone but the carry-in, unless it left."""
    stamp: dict[str, Any] = {"evidence": "said so", "zone": ZONE}
    with closing(_log(tmp_path / "events.db")) as conn:
        def add(
            title: str, at: str, kind: str = "go", by: str = "her", **fields: Any,  # noqa: ANN401
        ) -> str:
            one = things.Thing(things.new_id(), title, kind, **fields)
            return things.add(conn, one, by=by, now=_at(at), **stamp)[0].thing_id

        add("Dentist", "06:00", start_at=_on(0, "14:00"), place="Main St")
        add("Holiday", "06:05", on_day=DAY.isoformat())
        add("Send form", "06:10", "do", "mail", due=DAY.isoformat())
        klass = add("Class", "06:15", start_at=_on(0, "19:00"), weekly_until=_on(7, "19:00")[:10])
        add("Seminar", "06:20", start_at=_on(1, "10:00"))
        add("Old news", "06:25", by="carry_in", start_at=_on(0, "15:00"))
        gone = add("Cancelled", "06:30", start_at=_on(0, "15:30"))
        paid = add("Paid", "06:35", "do", due=DAY.isoformat())
        away = add("Moved away", "06:40", start_at=_on(0, "16:00"))
        here = add("Moved here", "06:45", start_at=_on(1, "09:00"))
        things.close(conn, gone, "cancelled", by="her", now=_at("07:00"), **stamp)
        things.close(conn, paid, "paid", by="her", now=_at("07:05"), **stamp)
        things.move(conn, away, start_at=_on(1, "16:00"), by="her", now=_at("07:30"), **stamp)
        things.move(conn, here, start_at=_on(0, "17:00"), by="her", now=_at("07:35"), **stamp)
        # Next week's class is off; today's is not.
        things.close(
            conn, klass, "cancelled", occurrence=_on(7, "19:00")[:10], by="her", now=_at("08:00"),
            **stamp,
        )
        early = facts_at(conn, DAY, _ms("06:50"), ZONE, [HOME])
        late = facts_at(conn, DAY, _ms("09:00"), ZONE, [HOME])
    assert [(t.title, t.start) for t in early.things] == [
        ("Dentist", "14:00"), ("Holiday", ""), ("Send form", ""), ("Class", "19:00"),
        ("Cancelled", "15:30"), ("Paid", ""), ("Moved away", "16:00"),
    ]
    assert [(t.title, t.start) for t in late.things] == [
        ("Dentist", "14:00"), ("Holiday", ""), ("Send form", ""), ("Class", "19:00"),
        ("Moved here", "17:00"),
    ]
    assert late.payload(ZONE)["things_noted_today"][:2] == [
        {"title": "Dentist", "start": "14:00", "place": "Main St", "noted_at": "06:00"},
        {"title": "Holiday", "start": None, "place": "", "noted_at": "06:05"},
    ]


def _mac_state(kind: str, at: str) -> dict[str, Any]:
    """A TimeSink observation whose latest state is ``kind`` at ``at`` (the 10-17 shape)."""
    return {
        "actor": "observer", "status": "ok", "identity": "i", "span_high": 1,
        "span_latest_end": _at(at).isoformat(), "capture_high": 1,
        "capture_latest_seen": _at(at).isoformat(), "state_high": 1,
        "observed_at_ms": _ms(at), "state_latest": {"at": _at(at).isoformat(), "kind": kind},
    }


def test_unlocking_or_waking_the_mac_is_a_wake_sign_like_coming_back_from_idle(
    tmp_path: Path,
) -> None:
    """2026-10-17: his first sign was an unlock at 09:24 and the watcher missed it."""
    for kind in ("unlock", "wake", "active"):
        with closing(_log(tmp_path / f"{kind}.db")) as conn:
            emit_event(
                conn, type="timesink.state_observed", ts_epoch_ms=_ms("09:26"),
                payload=_mac_state(kind, "09:24"),
            )
            facts = facts_at(conn, DAY, _ms("09:30"), ZONE, [HOME])
        assert facts.wake == Wake("mac_active", _ms("09:24")), kind
    with closing(_log(tmp_path / "lock.db")) as conn:
        emit_event(
            conn, type="timesink.state_observed", ts_epoch_ms=_ms("09:26"),
            payload=_mac_state("lock", "09:24"),
        )
        assert facts_at(conn, DAY, _ms("09:30"), ZONE, [HOME]).wake is None


def test_facts_carry_todays_commits_by_repo_name_latest_twenty_up_to_that_minute(
    tmp_path: Path,
) -> None:
    """Yesterday's and later commits are out; the cap keeps the most recent 20."""

    def commit(sha: str, at: datetime, repo: str = "/Users/me/Projects/jarvis/") -> None:
        emit_event(
            conn, type="project.commit_seen", ts_epoch_ms=int(at.timestamp() * 1000),
            payload={"repo_path": repo, "commit_sha": sha, "subject": f"change {sha}",
                     "committed_at_ms": int(at.timestamp() * 1000), "actor": "observer"},
        )

    with closing(_log(tmp_path / "events.db")) as conn:
        commit("old", _at("08:00") - timedelta(days=1))
        for n in range(22):
            commit(f"c{n:02d}", _at("08:00") + timedelta(minutes=n))
        commit("later", _at("12:00"), "/Users/me/Projects/other")
        before = facts_at(conn, DAY, _ms("10:00"), ZONE, [])
        after = facts_at(conn, DAY, _ms("13:00"), ZONE, [])
    assert len(before.commits) == 20
    assert (before.commits[0].subject, before.commits[-1].subject) == ("change c02", "change c21")
    assert (before.commits[0].repo, before.commits[0].at_ms) == ("jarvis", _ms("08:02"))
    assert (after.commits[-1].repo, after.commits[-1].subject) == ("other", "change later")


# --- the line in her state block -----------------------------------------------------------


def test_the_plan_line_names_the_next_task_the_time_left_and_the_next_fixed_item(
    tmp_path: Path,
) -> None:
    """Settled or not; a private task or item is named by its time only; no plan: no line."""
    log = tmp_path / "events.db"
    held = [_task("p1", 60), _task("p2", 30), _item("p9", title="Dentist", start="14:00")]

    def line(clock: str = "10:00") -> str | None:
        return plan_line(log, ZONE, lambda: _at(clock), time(18, 0))

    with closing(_log(log)) as conn:
        assert line() is None
        plan.draft(conn, DAY, held, "d", "m")
        assert line() == (
            "Plan today (not settled yet): next task Task p1; 2 tasks left, 90 of 420 free"
            " minutes; next fixed 14:00 Dentist."
        )
        plan.settle(conn, DAY, "him")
        plan.revise(conn, DAY, [
            _task("p1", 60, done=True), _task("p2", 30, private=True),
            _item("p9", title="Dentist", start="14:00", private=True),
        ], "him", "x")
        assert line() == (
            "Plan today (settled): next task a private item; 1 task left, 30 of 420 free"
            " minutes; next fixed 14:00."
        )
        plan.revise(conn, DAY, [_task("p1", done=True), _item("p9", start="14:00")], "him", "x")
        assert line() == "Plan today (settled): no tasks left; next fixed 14:00 Write the report."
        assert line("15:00") == "Plan today (settled): nothing more."


# --- the day line --------------------------------------------------------------------------


def test_a_day_with_a_plan_draws_its_items_except_the_things_drawn_already(
    tmp_path: Path,
) -> None:
    """Plan items are `event` rows with source plan; a timed thing's stands for itself."""
    held = [
        _item("p1", place="Library", private=True),
        _item("p2", start=None, window_from="15:30", window_to="17:30", minutes=45),
        _item("p3", start=None),
        _item("p4", source="item:thing-c1", firmness="fixed", start="14:00"),
        _item("p5", source="commitment:commitment-c1", firmness="fixed", start="14:00"),
        _item("p6", source="item:thing-d1", title="Pay rent", start="16:00"),
    ]
    with closing(_log(tmp_path / "events.db")) as conn:
        for noted in (
            things.Thing("thing-c1", "Dentist", "go", start_at=_at("14:00").isoformat()),
            things.Thing("thing-d1", "Pay rent", "do", due=DAY.isoformat()),
        ):
            things.add(conn, noted, by="her", evidence="said so", zone=ZONE, now=_at("06:00"))
        plan.draft(conn, DAY, held, "d", "m")
        first, last = day_window(DAY, ZONE)
        line = fold_day(conn, first, last, _ms("09:30"), DaySources(ZONE))
        other = fold_day(conn, *day_window(DAY + timedelta(days=1), ZONE), 0, DaySources(ZONE))
    rows = {r["id"]: r for r in line.items if r.get("source") == "plan"}
    # The dentist stands for p4 and for p5 (a plan from before the ledger); a thing with only a
    # due date has no row of its own, so its plan item is drawn.
    assert set(rows) == {"plan-2026-10-16-p1", "plan-2026-10-16-p2", "plan-2026-10-16-p6"}
    one = rows["plan-2026-10-16-p1"]
    assert (one["kind"], one["lane"], one["place"], one["private"], one["state"]) == (
        "event", "you", "Library", True, "now",
    )
    assert (one["start_ms"], one["end_ms"]) == (_ms("09:00"), _ms("10:00"))
    assert rows["plan-2026-10-16-p2"]["start_ms"] == _ms("15:30")
    assert not [r for r in other.items if r.get("source") == "plan"]
    assert "plan" not in line.missing


# --- the replay script ---------------------------------------------------------------------


SAMPLE = {"date": "2026-10-16", "items": [
    {"item_id": "p1", "title": "Wake", "start": "08:00", "minutes": 30, "place": "Home",
     "firmness": "movable", "source": "his"},
    {"item_id": "p2", "title": "Library", "start": "10:00", "minutes": 60, "place": "Library",
     "firmness": "movable", "source": "goal",
     "route": {"from": "Home", "leave_at": "09:20", "line": "28", "arrive_at": "09:43"}},
]}


def test_the_replay_walks_a_day_with_a_stub_decider_and_writes_nothing(tmp_path: Path) -> None:
    """The plan file parses, the walk shows the notices, and the log is left as it was."""
    from scripts import plan_day_replay as script  # noqa: PLC0415 - the script is not a package

    log = tmp_path / "events.db"
    with closing(_log(log)) as conn:
        emit_event(
            conn, type="phone.state_observed", ts_epoch_ms=_ms("07:40"), ingestion_node="phone",
            payload={"signal": "focus", "value": "off"},
        )
        size = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(SAMPLE), encoding="utf-8")
    held = script.load_plan(path, DAY)
    assert [i.item_id for i in held.items] == ["p1", "p2"]
    with closing(script.open_read_only(log)) as conn:
        lines = script.replay(DAY, held, conn, [], ZONE, 5, script.stub_decide)
    assert [(line["time"], line["kind"]) for line in lines] == [
        ("07:40", "woke"), ("08:00", "item_due"), ("08:55", "outing_soon"),
        ("09:25", "left_late"), ("10:00", "item_due"),
    ]
    assert lines[0]["decision"]["say"] == script.STUB
    assert "- 08:55 outing_soon (Library): tell" in script.summary(DAY, lines)
    with closing(_log(log)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == size
    out = tmp_path / "out"
    assert script.main(
        ["--date", "2026-10-16", "--plan", str(path), "--event-log", str(log),
         "--memory-db", str(tmp_path / "none.db"), "--zone", "America/Vancouver",
         "--step-min", "30", "--out", str(out)],
    ) == 0
    assert (out / "2026-10-16.md").is_file()


def test_the_replay_shows_the_budget_on_progress_lines(tmp_path: Path) -> None:
    """Tasks plus a woken day: the 09:00 progress line and its summary carry the budget."""
    from scripts import plan_day_replay as script  # noqa: PLC0415 - the script is not a package

    log = tmp_path / "events.db"
    with closing(_log(log)) as conn:
        emit_event(
            conn, type="phone.state_observed", ts_epoch_ms=_ms("07:00"), ingestion_node="phone",
            payload={"signal": "focus", "value": "off"},
        )
    path = tmp_path / "plan.json"
    items = [_task("p1", 60), _task("p2", 30)]
    path.write_text(json.dumps({"date": DAY.isoformat(), "items": [i.payload() for i in items]}),
                    encoding="utf-8")
    with closing(script.open_read_only(log)) as conn:
        held = script.load_plan(path, DAY)
        lines = script.replay(DAY, held, conn, [], ZONE, 60, script.stub_decide)
    [first] = [line for line in lines if line["key"] == "progress:1"]
    assert first["budget"] == {"remaining_min": 90, "free_min": 870, "fits": True}  # 09:00 to 23:30
    assert "  - budget: 90 min of tasks, " in script.summary(DAY, lines)


def test_the_replay_prints_the_done_check_and_asks_the_decider_only_when_the_day_does_not_fit(
    tmp_path: Path,
) -> None:
    """The check marks p1 done, so 30 minutes fit the 45 left: silent, no decider; else it asks."""
    from jarvis.decision.day_plan import Decision, Progress  # noqa: PLC0415
    from scripts import plan_day_replay as script  # noqa: PLC0415 - the script is not a package

    log = tmp_path / "events.db"
    with closing(_log(log)) as conn:
        emit_event(
            conn, type="phone.state_observed", ts_epoch_ms=_ms("07:00"), ingestion_node="phone",
            payload={"signal": "focus", "value": "off"},
        )
        _work(conn, "08:00", "editing the report")
    held = _plan(_task("p1", 60), _task("p2", 30))
    asked: list[str] = []  # the progress notices the decider is asked about

    def decide(_system: str, user: str, _held: plan.Plan) -> Any:  # noqa: ANN401
        if json.loads(user)["notice"]["kind"] == "progress":
            asked.append(user)
        return Decision("tell", "x")

    def run(check: Any) -> dict[str, Any]:  # noqa: ANN401
        with closing(script.open_read_only(log)) as conn:
            lines = script.replay(DAY, held, conn, [], ZONE, 60, decide, time(9, 45), check)
        [line] = [one for one in lines if one["key"] == "progress:1"]
        return line

    line = run(lambda *_: Progress(("p1",), {"p2": 20}, "sent", ()))
    assert line["done_check"] == {"done": ["p1"], "left": {"p2": 20}, "why": "sent", "notes": []}
    assert (line["decision"]["act"], line["decision"]["why"], asked) == ("silent", "fits", [])
    assert line["budget"] == {"remaining_min": 20, "free_min": 45, "fits": True}
    assert line["facts"]["evidence"]["screen"] == [{"at": "08:00", "text": "editing the report"}]
    line = run(script.stub_check)
    assert line["done_check"]["done"] == []
    assert line["budget"]["fits"] is False
    assert line["decision"]["act"] == "tell"
    assert json.loads(asked[0])["notice"]["facts"]["fits"] is False
