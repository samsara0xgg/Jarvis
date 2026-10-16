"""ADR 0276 — the day's experience table: the fold and the store.

The fold is driven by synthetic rows (type, time, payload, device) of one local date and its
expected metrics; the store and the cards' reactions run over a real memory.db and a real
Event Log written through ``emit_event``. The schedule and the report are in
``test_day_experience_schedule``.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import date, datetime, time
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.state.day_experience import (
    SIGNALS,
    Row,
    compute_day,
    day_bounds_ms,
    fold_day,
    weekly,
)
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.state.job_ledger import add_card_feedback, snapshot_card
from jarvis.state.memory_db import (
    experience_days,
    latest_day_experience,
    open_memory_db,
    put_day_experience,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

DAY = date(2026, 10, 14)
START, END = day_bounds_ms(DAY)
S = 1000


def _at(seconds: float) -> int:
    """Epoch ms of ``seconds`` after the day's first midnight."""
    return START + int(seconds * S)


def _row(kind: str, seconds: float, node: str = "mac", **payload: Any) -> Row:  # noqa: ANN401
    return Row(kind, _at(seconds), payload, node)


def _spoken(turn: str, seconds: float, node: str = "mac", **extra: Any) -> Row:  # noqa: ANN401
    return _row(
        "utterance.received", seconds, node, transcript=f"words of {turn}", turn_id=turn, **extra,
    )


def _typed(turn: str, seconds: float, node: str = "mac", **extra: Any) -> Row:  # noqa: ANN401
    return _row(
        "surface.user_intent", seconds, node,
        transcript=f"words of {turn}", turn_id=turn, channel="cli_stdin", **extra,
    )


def _read(turn: str, seconds: float, **verdict: Any) -> Row:  # noqa: ANN401
    return _row("route.oneshot_read", seconds, turn_id=turn, **verdict)


def _fold(rows: Sequence[Row], reactions: Sequence[tuple[str, str]] = ()) -> dict[str, Any]:
    return fold_day(rows, DAY, reactions)


def test_an_empty_day_is_all_zero_and_the_planning_fields_are_null() -> None:
    """No rows: no division by zero, no spread, and the two future fields are explicit nulls."""
    metrics = _fold([])
    assert metrics["uses"] == {"total": 0, "spoken": 0, "typed": 0, "by_device": {}}
    assert metrics["right_first_time"]["share"] is None
    assert metrics["latency_voice_ms"] == {"n": 0, "median": None, "p90": None}
    assert metrics["interrupted_ratio"]["ratio"] is None
    assert metrics["labels"] == []
    assert (metrics["plan_settled"], metrics["calendar_missed"]) == (None, None)
    assert json.loads(json.dumps(metrics)) == metrics
    assert weekly([metrics]) == {"days": 1, "turns": 0, "most_used": None, "most_failed": None}


def test_uses_split_by_device_and_kind_and_leave_out_what_is_not_a_turn() -> None:
    """Card taps, ask-card answers, resumes, Live delegations, blanks and merges are no use."""
    metrics = _fold([
        _spoken("v1", 10),
        _spoken("v2", 20),
        _spoken("v3", 30, "phone"),
        _typed("t1", 40),
        _typed("t2", 50, "phone"),
        _row("surface.user_intent", 60, transcript="", turn_id="c1", channel="card",
            confirmation_decision={"decision": "accept"}),
        _row("surface.user_intent", 61, transcript="a: b", turn_id="c2", channel="clarify"),
        _row("surface.user_intent", 62, transcript="again", turn_id="c3", channel="plugin_resume"),
        _row("surface.user_intent", 63, transcript="do it", turn_id="c4", channel="gpt_live"),
        _row("utterance.received", 64, transcript="  ", turn_id="c5"),
        # v1 was merged into v2: one use, not two.
        _row("response.cancelled", 21, turn_id="v1", reason="superseded",
            interrupted_by_turn_id="v2"),
        # yesterday's turn answered today is not today's use
        Row("utterance.received", START - 5 * S, {"transcript": "x", "turn_id": "old"}, "mac"),
        _row("response.completed", 5, turn_id="old"),
    ])
    assert metrics["uses"] == {
        "total": 4, "spoken": 2, "typed": 2,
        "by_device": {"mac": {"spoken": 1, "typed": 1}, "phone": {"spoken": 1, "typed": 1}},
    }


@pytest.mark.parametrize(
    ("signal", "extra"),
    [
        ("guard_repeat", _row("turn.guard_fired", 12, turn_id="a", guard="repeat", tool="x")),
        ("guard_unbacked", _row("turn.guard_fired", 12, turn_id="a", guard="unbacked", said="s")),
        ("turn_failed", _row("turn.failed", 12, turn_id="a", exception_repr="boom")),
        ("response_failed", _row("response.failed", 12, turn_id="a", reason="x")),
        ("user_stop", _row("response.cancelled", 12, turn_id="a", reason="user_stop")),
        ("barge_in", _row("response.cancelled", 12, turn_id="a", reason="barge_in")),
        ("speech_dropped", _row("surface.speech_dropped", 12, turn_id="a", reason="x")),
    ],
)
def test_each_redo_signal_makes_the_turn_not_right_the_first_time(
    signal: str, extra: Row,
) -> None:
    """One signal on one of two turns: half are right first time, and the signal is named."""
    metrics = _fold([_spoken("a", 10), _spoken("b", 100), extra])
    right = metrics["right_first_time"]
    assert (right["turns"], right["clean"], right["share"]) == (2, 1, 0.5)
    assert right["signals"] == {name: int(name == signal) for name in SIGNALS}
    assert [(e["turn_id"], e["failed"]) for e in metrics["labels"]] == [("a", True), ("b", False)]


def test_a_cancel_that_is_not_the_users_does_not_count() -> None:
    """Shutdown and operator cancels, and a quiet drop of a merged turn, are not redo signals."""
    metrics = _fold([
        _spoken("a", 10),
        _row("response.cancelled", 11, turn_id="a", reason="shutdown"),
        _row("response.cancelled", 12, turn_id="a", reason="operator_request"),
    ])
    assert metrics["right_first_time"]["clean"] == 1


def test_an_ask_card_the_turn_raised_and_he_filled_in_counts_against_the_asking_turn() -> None:
    """Clarified marks the turn that asked (by clarification id), not the answer turn."""
    metrics = _fold([
        _spoken("a", 10),
        _row("clarification.requested", 11, clarification_id="k1", turn_id="a", question="?",
            fields=[]),
        _row("surface.user_intent", 20, transcript="x: y", turn_id="ans", channel="clarify"),
        _row("surface.clarified", 20, turn_id="ans", clarification_id="k1"),
    ])
    assert metrics["right_first_time"]["signals"]["clarified"] == 1
    assert metrics["labels"][0]["failed"] is True


@pytest.mark.parametrize(
    ("gap_s", "marked"), [(0.5, True), (119, True), (120, True), (121, False)],
)
def test_a_correction_line_within_120_seconds_marks_the_line_it_corrected(
    gap_s: float, *, marked: bool,
) -> None:
    """The following line's Jev relation is correction: the earlier one was not right."""
    metrics = _fold([
        _spoken("a", 10),
        _spoken("b", 10 + gap_s),
        _read("b", 10 + gap_s, relation="correction", relation_confidence=0.9, prev_turn_id="a"),
    ])
    assert metrics["right_first_time"]["signals"]["correction"] == int(marked)
    assert [e["failed"] for e in metrics["labels"]] == [marked, False]


def test_only_a_correction_relation_counts_not_a_supplement_or_a_new_line() -> None:
    """Supplement, new and unrelated lines leave the earlier turn clean."""
    metrics = _fold([
        _spoken("a", 10), _spoken("b", 20), _spoken("c", 30), _spoken("d", 40),
        _read("b", 20, relation="supplement", prev_turn_id="a"),
        _read("c", 30, relation="new", prev_turn_id="b"),
        _read("d", 40, relation="unrelated", prev_turn_id="c"),
    ])
    assert metrics["right_first_time"]["clean"] == 4


def test_a_correction_that_took_over_the_earlier_turn_marks_the_use_that_remains() -> None:
    """A merged into B (superseded) and B corrects A: the one use that remains, B, was redone."""
    metrics = _fold([
        _spoken("a", 10),
        _spoken("b", 15),
        _row("response.cancelled", 14, turn_id="a", reason="superseded",
             interrupted_by_turn_id="b"),
        _read("b", 15, relation="correction", prev_turn_id="a"),
    ])
    assert metrics["uses"]["total"] == 1
    assert [(e["turn_id"], e["failed"]) for e in metrics["labels"]] == [("b", True)]


def test_latency_runs_from_end_of_speech_or_send_to_the_first_sound_or_text() -> None:
    """Voice counts only where the device knew when he stopped; a missing time is left out."""
    rows = [
        _spoken("v1", 100, spoke_ago_ms=500),  # stopped at 99.5, sound at 101.0: 1500
        _row("surface.playback_started", 101, turn_id="v1"),
        _row("surface.playback_started", 105, turn_id="v1"),  # only the first counts
        _spoken("v2", 200, spoke_ago_ms=0),  # 3000
        _row("surface.playback_started", 203, turn_id="v2"),
        _spoken("v3", 300, spoke_ago_ms=1000),  # 5000
        _row("surface.playback_started", 304, turn_id="v3"),
        _spoken("v4", 400),  # no end of speech
        _row("surface.playback_started", 401, turn_id="v4"),
        _spoken("v5", 500, spoke_ago_ms=100),  # no sound at all
        _typed("t1", 600),
        _row("surface.response_chunk", 600.8, turn_id="t1", text="a"),
        _row("surface.response_chunk", 603, turn_id="t1", text="b"),
        _typed("t2", 700),
        _row("surface.response_chunk", 702, turn_id="t2", text="a"),
        _typed("t3", 800),  # no chunk
    ]
    metrics = _fold(rows)
    assert metrics["latency_voice_ms"] == {"n": 3, "median": 3000, "p90": 5000}
    assert metrics["latency_typed_ms"] == {"n": 2, "median": 1400, "p90": 2000}


def test_p90_is_nearest_rank() -> None:
    """Ten values: the ninth; the median of an even count is the mean of the middle two."""
    rows: list[Row] = []
    for i in range(1, 11):
        rows += [
            _typed(f"t{i}", i * 100),
            _row("surface.response_chunk", i * 100 + i, turn_id=f"t{i}", text="x"),
        ]
    spread = _fold(rows)["latency_typed_ms"]
    assert (spread["n"], spread["median"], spread["p90"]) == (10, 5500, 9000)


def test_interrupted_ratio_counts_only_barge_in_and_user_stop_over_started_playbacks() -> None:
    """Four playbacks, one barged in, one stopped, one failed over to another reason."""
    metrics = _fold([
        *(_row("surface.playback_started", 10 + i, turn_id=f"t{i}") for i in range(4)),
        _row("surface.playback_interrupted", 20, turn_id="t0", reason="barge_in"),
        _row("surface.playback_interrupted", 21, turn_id="t1", reason="user_stop"),
        _row("surface.playback_interrupted", 22, turn_id="t2", reason="foreground_superseded"),
        _row("surface.playback_started", END / S + 5, turn_id="tomorrow"),
    ])
    assert metrics["interrupted_ratio"] == {"started": 4, "interrupted": 2, "ratio": 0.5}


def test_answered_unaddressed_needs_an_answer_and_an_unrelated_verdict() -> None:
    """Unrelated and answered counts; unrelated and unanswered, or related, does not."""
    metrics = _fold([
        _spoken("a", 10), _spoken("b", 20), _spoken("c", 30), _spoken("d", 40),
        _read("b", 20, relation="unrelated", prev_turn_id="a"),
        _read("c", 30, relation="unrelated", prev_turn_id="b"),
        _read("d", 40, relation="new", prev_turn_id="c"),
        _row("response.completed", 21, turn_id="b"),
        _row("response.completed", 41, turn_id="d"),
    ])
    assert metrics["answered_unaddressed"] == 1


def test_claimed_not_done_counts_the_unbacked_guard() -> None:
    """Only guard unbacked; a repeat guard is a redo signal but not a claim."""
    metrics = _fold([
        _spoken("a", 10),
        _row("turn.guard_fired", 11, turn_id="a", guard="unbacked", said="done"),
        _row("turn.guard_fired", 12, turn_id="a", guard="repeat", tool="t"),
        _row("turn.guard_fired", 13, turn_id="zz", guard="unbacked", said="done"),
    ])
    assert metrics["claimed_not_done"] == 2


def test_proactive_outcomes_come_from_events_and_card_reactions() -> None:
    """Acknowledged reminders and boarded rides follow; a closed ask card or declined ride does not.

    Each card counts once, under the first of acted, dismissed, ignored.
    """
    metrics = _fold(
        [
            _row("reminder.acknowledged", 10, reminder_id="r1"),
            _row("ride.candidate_answered", 11, candidate_id="c1", answer="yes"),
            _row("ride.candidate_answered", 12, candidate_id="c2", answer="no"),
            _row("surface.dismissed", 13, turn_id="a", clarification_id="k"),
            _row("reminder.acknowledged", END / S + 10, reminder_id="r2"),  # tomorrow
        ],
        [
            ("n1", "right"), ("n1", "acted"), ("n1", "dismissed"),
            ("n2", "dismissed"),
            ("n3", "ignored"),
            ("n4", "right"), ("n4", "level:card"),
        ],
    )
    assert metrics["proactive"] == {"followed": 3, "dismissed": 3, "ignored": 1}


def test_hard_missed_counts_late_or_undelivered_reminders_and_unwarned_rides() -> None:
    """A reminder more than five minutes late, or not delivered; a ride past its stop unwarned."""
    metrics = _fold([
        _row("reminder.fired", 10, reminder_id="r1", fired_at=0, late_ms=299_999,
             delivered="speak"),
        _row("reminder.fired", 20, reminder_id="r2", fired_at=0, late_ms=300_001,
            delivered="card_sound"),
        _row("reminder.fired", 30, reminder_id="r3", fired_at=0, late_ms=0, delivered=""),
        # ride 1: alight set, never warned, passed the stop: missed
        _row("ride.started", 40, ride_id="1", route="7", source="tap"),
        _row("ride.alight_set", 41, ride_id="1", alight_stop="Main"),
        _row("ride.ended", 50, ride_id="1", reason="passed_stop"),
        # ride 2: warned in time
        _row("ride.started", 60, ride_id="2", route="7", source="tap", alight_stop="Main"),
        _row("ride.warned", 61, ride_id="2"),
        _row("ride.ended", 70, ride_id="2", reason="passed_stop"),
        # ride 3: no alight stop was ever set
        _row("ride.started", 80, ride_id="3", route="7", source="tap"),
        _row("ride.ended", 90, ride_id="3", reason="passed_stop"),
        # ride 4: left the vehicle
        _row("ride.started", 100, ride_id="4", route="7", source="tap", alight_stop="Main"),
        _row("ride.ended", 110, ride_id="4", reason="left_vehicle"),
        # ride 5 began yesterday and passed its stop today: counted on the day it ended
        Row("ride.started", START - 600 * S, {"ride_id": "5", "route": "7", "source": "tap",
                                              "alight_stop": "Main"}, "mac"),
        _row("ride.ended", 120, ride_id="5", reason="passed_stop"),
    ])
    assert metrics["hard_missed"] == {"reminders": 2, "rides_missed_alight": 2}


def test_labels_name_each_spoken_turn_by_its_verdict_and_the_week_ranks_them() -> None:
    """Typed turns carry no label; the week names the most used and the most failed."""
    def day(labels: Sequence[tuple[str | None, str | None, bool]]) -> dict[str, Any]:
        return {"labels": [
            {"turn_id": str(i), "tool_group": g, "intent": n, "failed": f}
            for i, (g, n, f) in enumerate(labels)
        ]}

    metrics = _fold([
        _spoken("a", 10), _typed("t", 20), _spoken("b", 30), _spoken("c", 40),
        _read("a", 10, tool_group="web_search", intent="none"),
        _read("b", 30, tool_group="none", intent="time"),
        _row("turn.failed", 31, turn_id="a", exception_repr="x"),
    ])
    assert metrics["labels"] == [
        {"turn_id": "a", "tool_group": "web_search", "intent": "none", "failed": True},
        {"turn_id": "b", "tool_group": "none", "intent": "time", "failed": False},
        {"turn_id": "c", "tool_group": None, "intent": None, "failed": False},
    ]
    summary = weekly([
        day([("web_search", "none", False)] * 3 + [("mail_read", "none", True)] * 2),
        day([("mail_read", "none", True), ("none", "none", False), (None, None, False)]),
        day([("none", "time", False)]),
    ])
    assert summary == {
        "days": 3,
        "turns": 9,
        "most_used": {"label": "web_search", "turns": 3},
        "most_failed": {"label": "mail_read", "failed": 3, "turns": 3},
    }
    assert weekly([day([("web_search", "none", False)])])["most_failed"] is None


# --- the store and the cards' reactions, over real files ----------------------------------


def _log(path: Path, rows: Sequence[tuple[str, datetime, dict[str, Any], str]]) -> None:
    with closing(open_event_log(path)) as conn:
        for kind, when, payload, node in rows:
            emit_event(
                conn, type=kind, payload=payload, ingestion_node=node,
                ts_epoch_ms=int(when.timestamp() * 1000),
                correlation={"turn_id": payload["turn_id"]} if "turn_id" in payload else None,
            )


def _noon(day: date, minute: int = 0) -> datetime:
    return datetime.combine(day, time(12, minute)).astimezone()


def test_the_store_replaces_a_date_and_reads_the_newest_rows_oldest_first(tmp_path: Path) -> None:
    """One row per local date; a second write of a date replaces it."""
    path = tmp_path / "memory.db"
    for day, uses in (("2026-10-12", 1), ("2026-10-13", 2), ("2026-10-14", 3), ("2026-10-13", 9)):
        put_day_experience(path, day, json.dumps({"uses": uses}))
    assert experience_days(path) == {"2026-10-12", "2026-10-13", "2026-10-14"}
    with closing(open_memory_db(path)) as conn:
        rows = latest_day_experience(conn, 2)
    assert [(day, json.loads(m)["uses"]) for day, m in rows] == [
        ("2026-10-13", 9), ("2026-10-14", 3),
    ]
    with closing(sqlite3.connect(":memory:")) as bare:
        assert latest_day_experience(bare, 7) == []  # no table yet: no rows


def test_a_cards_reactions_in_memory_db_reach_the_proactive_counts(tmp_path: Path) -> None:
    """A snapshot, one reaction and one unanswered card, read back through memory.db."""
    log, memory = tmp_path / "events.db", tmp_path / "memory.db"
    _log(log, [("utterance.received", _noon(DAY), {"transcript": "hi", "turn_id": "T1"}, "mac")])
    moment = _noon(DAY)
    snapshot_card(memory, kind="pop", card_id="c1", pack_json="{}", judge_id="j",
                  judge_version="1", level="card", reason="r", now=moment)
    add_card_feedback(memory, "c1", "acted", moment)
    snapshot_card(memory, kind="pop", card_id="c2", pack_json="{}", judge_id="j",
                  judge_version="1", level="card", reason="r", now=moment)
    add_card_feedback(memory, "c2", "right", moment)
    snapshot_card(memory, kind="wait", card_id="c3", pack_json="{}", judge_id="j",
                  judge_version="1", level="card", reason="r", now=moment)
    add_card_feedback(memory, "c3", "ignored", moment)
    with closing(open_event_log(log)) as conn:
        metrics = compute_day(conn, memory, DAY)
    assert metrics["proactive"] == {"followed": 1, "dismissed": 0, "ignored": 1}
