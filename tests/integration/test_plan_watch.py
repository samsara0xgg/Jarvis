"""ADR 0289: the plan watcher's pure core, over invented days (no key, no network, no model).

- ``apply_changes``: add, edit and drop, a fixed item he may change, an edit that clears the leg,
  ops that do not read skipped with a note;
- each notice kind fires at its threshold and not a minute before, and only once;
- ``parse_decision`` turns anything malformed into silence;
- ``facts_at`` reads only what the log held at that minute;
- a day with a plan draws its items on the day line;
- the replay script parses a plan file and walks a day with a stub decider.
"""

from __future__ import annotations

import json
from contextlib import closing
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest

from jarvis.decision.day_plan import build_decide_prompt, parse_decision
from jarvis.decision.plan_watch import notice
from jarvis.state import commitments, plan
from jarvis.state.day_line import DaySources, day_window, fold_day
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.state.places import Place
from jarvis.state.plan_facts import DayFacts, Here, Move, NotedCommitment, Stay, Wake, facts_at

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
        "item_id": item_id, "title": "Write the report", "firmness": "movable", "source": "todo",
        "start": "09:00", "minutes": 60,
    }
    return plan.PlanItem(**(base | fields))


def _plan(*items: plan.PlanItem) -> plan.Plan:
    return plan.Plan(DAY, items)


# --- apply_changes -------------------------------------------------------------------------


def test_apply_changes_adds_edits_and_drops_and_a_fixed_item_may_change() -> None:
    """Each op does its part; a new id is made; moving or re-placing an item clears its leg."""
    leg = {"from": "Home", "leave_at": "08:20"}
    items = [_item("p1", firmness="fixed", source="commitment:c1", route=leg), _item("p2")]
    new, notes = plan.apply_changes(items, [
        {"op": "add", "item": {"title": "Walk", "start": "08:00", "minutes": 30}},
        {"op": "edit", "id": "p2", "title": "Write", "private": True},
        {"op": "edit", "id": "p1", "start": "09:30"},
        {"op": "drop", "id": "p2"},
    ])
    assert notes == []
    assert [(i.item_id, i.title, i.start) for i in new] == [
        ("p3", "Walk", "08:00"), ("p1", "Write the report", "09:30"),
    ]
    assert new[0].firmness == "movable"
    assert new[1].route is None  # the old leg no longer fits the new time
    kept, _ = plan.apply_changes(items, [{"op": "edit", "id": "p1", "why": "because"}])
    assert kept[0].route == leg  # a change that moves nothing keeps the leg


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
    """The thresholds of ADR 0289: not_up 30 min, outing_soon 25 min, left_late 3 min, ..."""
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


def test_woke_and_new_commitment_and_no_wake_before_noon() -> None:
    """A wake is noticed; a commitment already in the plan is not new; a late first item is not."""
    mine = NotedCommitment("c1", "Dentist", "14:00", "", private=False, noted_at_ms=1)
    other = NotedCommitment("c2", "Call mum", "18:00", "", private=False, noted_at_ms=1)
    facts = DayFacts(0, Wake("mac_active", _ms("07:10")), None, (), (), (), (mine, other))
    held = _plan(_item("p1", source="commitment:c1", firmness="fixed", start="14:00"))
    got = {n.key: n for n in notice(held, facts, _at("07:30"), set())}
    assert set(got) == {"woke", "new_commitment:c2"}
    assert got["woke"].facts == {"signal": "mac_active", "at": "07:10"}
    late = _plan(_item("p1", start="13:00"))
    assert notice(late, DayFacts(0, None, None, (), (), (), ()), _at("15:00"), set()) == []


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
    assert (body["task"], body["now"], body["notice"]["key"]) == ("notice", "07:30", "woke")
    assert body["told_today"] == [{"at": "07:10", "say": "Heads up."}]
    assert body["plan"][0]["private"] is True
    assert body["facts"]["wake"] == {"kind": "focus_off", "at": "07:00"}


# --- the facts, as of a minute -------------------------------------------------------------


def _log(path: Path) -> Any:  # noqa: ANN401
    return open_event_log(path)


def test_facts_at_reads_only_what_the_log_held_at_that_minute(tmp_path: Path) -> None:
    """Replaying 08:05, 08:20 and 09:00 of one day sees the stay, the wake and the commitment."""
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
        dentist = commitments.Commitment(
            "c1", "Dentist", _at("14:00").isoformat(), "said", confirmed=True, private=False,
        )
        emit_event(
            conn, type="commitment.noted", payload=dentist.payload(), ts_epoch_ms=_ms("06:00"),
            actor="jarvis_runtime",
        )
        before = facts_at(conn, DAY, _ms("08:05"), ZONE, [HOME])
        woke = facts_at(conn, DAY, _ms("08:20"), ZONE, [HOME])
        later = facts_at(conn, DAY, _ms("09:00"), ZONE, [HOME])
    assert before.wake is None
    assert before.here is not None
    assert (before.here.place, before.here.since_ms) == ("Home", _ms("07:45"))
    assert woke.wake == Wake("focus_off", _ms("08:10"))
    assert later.wake == woke.wake  # the earliest stays the first
    assert [c.title for c in later.commitments] == ["Dentist"]
    assert before.payload(ZONE)["here"]["since"] == "07:45"


# --- the day line --------------------------------------------------------------------------


def test_a_day_with_a_plan_draws_its_items_except_the_commitments_drawn_already(
    tmp_path: Path,
) -> None:
    """Plan items are `event` rows with source plan; a commitment's stands for itself."""
    held = [
        _item("p1", place="Library", private=True),
        _item("p2", start=None, window_from="15:30", window_to="17:30", minutes=45),
        _item("p3", start=None),
        _item("p4", source="commitment:c1", firmness="fixed", start="14:00"),
    ]
    with closing(_log(tmp_path / "events.db")) as conn:
        plan.draft(conn, DAY, held, "d", "m")
        first, last = day_window(DAY, ZONE)
        line = fold_day(conn, first, last, _ms("09:30"), DaySources(ZONE))
        other = fold_day(conn, *day_window(DAY + timedelta(days=1), ZONE), 0, DaySources(ZONE))
    rows = {r["id"]: r for r in line.items if r.get("source") == "plan"}
    assert set(rows) == {"plan-2026-10-16-p1", "plan-2026-10-16-p2"}
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
