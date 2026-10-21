"""ADR 0139, 0140 — one Jev request per voice line, against a fake route.

``_FakeRoute`` is a ``SurrogateRoute`` whose ``post`` answers from a script instead of the
network, so what is asserted is what left for Jev (how many requests, which questions, the
state), what each reader took from the reply (the surface's control word, the decision
stage's instant function, the relation act, the ``route.tool_predicted`` event) and what the
dataset holds.
"""

from __future__ import annotations

import json
from concurrent.futures import Future
from contextlib import closing
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest
import yaml

from jarvis.decision import decide
from jarvis.decision.jev_oneshot import JevOneShot, oneshot_state
from jarvis.decision.surrogate_route import JevLog, Reply, SurrogateRoute, offered
from jarvis.runtime import _event_emitter, _jev_oneshot
from jarvis.runtime.config import RuntimeBootstrapError
from jarvis.state.event_log import (
    EventTypeRegistry,
    emit_event,
    iter_events_for_turn,
    open_event_log,
)
from tests.canary._helpers import repo_root
from tests.support.barge_in import LONG, SHORT, BargeInRig
from tests.support.clock import FakeClock
from tests.support.routine import drive_streaming_turn
from tests.support.spoken import (
    SpokenPeer,
    fixture_key,  # noqa: F401 - autouse fixture, shared with the other spoken-route tests
    spoken_intent,
    spoken_runtime,
)
from tests.support.surrogate import (
    LLM_ANSWER,
    TABLE,
    SurrogateJev,
    jev,  # noqa: F401 - the fixture
    logged_events,
    ran_tier0,
)
from tests.support.turn_context import build_decide_context
from tests.support.voice_words import LISTENING, LONG_LINE

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

    from jarvis.shared.clock import Clock

_WORDS = "do you happen to know the time"  # no Tier 0 row matches it
GROUPS = frozenset({"calendar_todo_read", "mail_read", "web_search", "comms_write"})


class _FakeRoute(SurrogateRoute):
    """Answers each question it is asked from ``script``; ``hang`` leaves futures open."""

    def __post_init__(self) -> None:
        super().__post_init__()
        self.sent: list[tuple[str, dict[str, Any], str, str | None]] = []
        self.futures: list[Future[Reply]] = []
        self.script: dict[str, tuple[str, float]] = {}
        self.hang = False

    def post(
        self, state: str, questions: dict[str, Any], use: str, ref: str | None = None,
    ) -> Future[Reply] | None:
        self.sent.append((state, questions, use, ref))
        future: Future[Reply] = Future()
        self.futures.append(future)
        if not self.hang:
            self.release(future, questions)
        return future

    def release(self, future: Future[Reply], questions: dict[str, Any]) -> None:
        answers = {
            key: {"choice": choice, "confidence": confidence}
            for key, (choice, confidence) in self.script.items()
            if key in questions
        }
        future.set_result(Reply({"answers": answers}, None, 0.0))


class _Lines:
    """A one-shot over the fake route, with the dataset in ``log_path``."""

    def __init__(
        self, tmp_path: Path, *, clock: Clock | None = None,
        **kwargs: Any,  # noqa: ANN401 - the keywords of JevOneShot
    ) -> None:
        """``clock`` is a stopped fake unless the test needs a deadline to pass for real."""
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.log_path = tmp_path / "decisions.jsonl"
        self.route = _FakeRoute(
            model="m", min_confidence=1.0, timeout_ms=800, clock=clock or FakeClock(),
            log=JevLog(self.log_path),
        )
        self.emitted: list[tuple[str, dict[str, Any], str]] = []  # route.tool_predicted
        self.read: list[tuple[str, dict[str, Any], str]] = []  # route.oneshot_read (ADR 0280)
        options: dict[str, Any] = {
            "words_at": 0.95, "relation_at": 0.98, "window_s": 30.0, "tool_at": 0.95,
            "tool_groups": GROUPS,
            "emit": self._emit,
            **kwargs,
        }
        self.oneshot = JevOneShot(self.route, offered(TABLE), **options)

    def _emit(self, kind: str, payload: dict[str, Any], turn: str) -> None:
        (self.read if kind == "route.oneshot_read" else self.emitted).append((kind, payload, turn))

    def say(self, **script: tuple[str, float]) -> None:
        self.route.script = script

    def lines(self, kind: str) -> list[dict[str, Any]]:
        if not self.log_path.exists():
            return []
        rows = [json.loads(line) for line in self.log_path.read_text("utf-8").splitlines()]
        return [row for row in rows if row["kind"] == kind]


def _run(rig: BargeInRig, frames: int = SHORT) -> None:
    try:
        rig.say(frames)
    finally:
        rig.close()


def test_one_request_serves_the_begin_and_the_control_word_check(tmp_path: Path) -> None:
    """The surface begins the line, then asks: both read one request, tagged oneshot."""
    lines = _Lines(tmp_path)
    lines.say(intent=("keep_going", 0.97), tool_group=("none", 0.99))
    rig = BargeInRig(
        tmp_path, LISTENING, recent_speech=lambda: "你今天有三项日程",
        ask_words=lines.oneshot.ask, begin_line=lines.oneshot.begin,
    )
    _run(rig)
    assert rig.output == ["gain 0.2", "gain 1.0"]
    assert rig.turns() == []
    [(state, questions, use, ref)] = lines.route.sent
    assert state.splitlines() == [
        "Assistant: 你今天有三项日程",
        "[The assistant was speaking when this was heard; hands-free mode on]",
        f"User: {LISTENING}",
    ]
    assert list(questions) == ["intent", "tool_group"]  # no earlier line: no relation question
    assert (use, ref is not None) == ("oneshot", True)
    criteria = questions["intent"]["criteria"]
    assert set(criteria) == {
        "keep_going", "stop", "wait", "dismiss", *(o.id for o in offered(TABLE)), "none",
    }


def test_a_long_line_is_sent_but_never_judged_as_a_control_word(tmp_path: Path) -> None:
    """Over max_chars the request still goes (the decision stage reads it); no word is acted on."""
    lines = _Lines(tmp_path)
    lines.say(intent=("keep_going", 0.99))
    rig = BargeInRig(
        tmp_path, LONG_LINE, ask_words=lines.oneshot.ask, begin_line=lines.oneshot.begin,
    )
    _run(rig, LONG)
    assert len(lines.route.sent) == 1
    assert rig.turns() == [LONG_LINE]


def test_a_line_the_regexes_absorbed_sends_nothing(tmp_path: Path) -> None:
    """Only a line the regexes call a turn is begun."""
    lines = _Lines(tmp_path)
    rig = BargeInRig(
        tmp_path, "嗯嗯。", ask_words=lines.oneshot.ask, begin_line=lines.oneshot.begin,
    )
    _run(rig)
    assert lines.route.sent == []


def test_no_answer_in_time_is_a_turn(tmp_path: Path) -> None:
    """The check waits for what is left of words_timeout_ms; none leaves the line a turn."""
    clock = FakeClock()
    lines = _Lines(tmp_path, words_timeout_ms=50, clock=clock)
    lines.route.hang = True
    rig = BargeInRig(
        tmp_path, LISTENING, ask_words=lines.oneshot.ask, begin_line=lines.oneshot.begin,
    )
    clock.pass_deadline_of(0.05, lambda: _run(rig))
    assert len(lines.route.sent) == 1
    assert rig.turns() == [LISTENING]


@pytest.mark.parametrize(
    ("relation", "confidence", "acts"),
    [
        ("supplement", 0.98, True),
        ("correction", 0.99, True),
        ("supplement", 0.979, False),
        ("correction", 0.9, False),
        ("new", 0.99, False),
        ("unrelated", 0.99, False),
    ],
)
def test_a_relation_acts_only_at_the_bar_and_only_as_supplement_or_correction(
    tmp_path: Path, relation: str, confidence: float, *, acts: bool,
) -> None:
    """The earlier turn is named to the seam at 0.98 and above; below it nothing happens."""
    lines = _Lines(tmp_path)
    acted: list[tuple[str, str, str]] = []

    def relate(earlier: str, turn: str, kind: str) -> str:
        acted.append((earlier, turn, kind))
        return "ok"

    lines.oneshot.relate = relate
    lines.say(relation=(relation, confidence))
    lines.oneshot.begin("T1", "帮我查明天的天气", "", False, False)  # noqa: FBT003
    assert "relation" not in lines.route.sent[0][1]
    lines.oneshot.begin("T2", "上海的", "", False, False)  # noqa: FBT003
    state, questions, _use, _ref = lines.route.sent[1]
    assert "relation" in questions
    assert "Previous user line (0 s earlier): 帮我查明天的天气" in state
    assert acted == ([("T1", "T2", relation)] if acts else [])
    assert [(row["relation"], row["outcome"]) for row in lines.lines("relation")] == (
        [(relation, "ok")] if acts else []
    )


def test_a_line_older_than_the_window_is_not_compared_and_relation_off_acts_on_nothing(
    tmp_path: Path,
) -> None:
    """Past window_s there is no relation question; with the bar unset the answer is ignored."""
    clock = FakeClock()
    late = _Lines(tmp_path / "late", clock=clock, window_s=30.0)
    late.say(relation=("supplement", 0.99))
    late.oneshot.begin("T1", "a", "", False, False)  # noqa: FBT003
    clock.advance(30.5)
    late.oneshot.begin("T2", "b", "", False, False)  # noqa: FBT003
    assert all("relation" not in sent[1] for sent in late.route.sent)
    near = _Lines(tmp_path / "near", clock=clock, window_s=30.0)  # a positive control
    near.say(relation=("supplement", 0.99))
    near.oneshot.begin("T1", "a", "", False, False)  # noqa: FBT003
    clock.advance(29.5)
    near.oneshot.begin("T2", "b", "", False, False)  # noqa: FBT003
    assert "relation" in near.route.sent[1][1]
    off = _Lines(tmp_path / "off", relation_at=None)
    acted: list[str] = []

    def relate(*_args: str) -> str:
        acted.append("acted")
        return "ok"

    off.oneshot.relate = relate
    off.say(relation=("correction", 0.99))
    off.oneshot.begin("T1", "a", "", False, False)  # noqa: FBT003
    off.oneshot.begin("T2", "b", "", False, False)  # noqa: FBT003
    assert acted == []


def test_a_relate_that_fails_is_noted_and_never_raises(tmp_path: Path) -> None:
    """The earlier answer is left alone; the dataset says error."""
    lines = _Lines(tmp_path)

    def broken(*_args: str) -> str:
        raise RuntimeError

    lines.oneshot.relate = broken
    lines.say(relation=("correction", 0.99))
    lines.oneshot.begin("T1", "a", "", False, False)  # noqa: FBT003
    lines.oneshot.begin("T2", "b", "", False, False)  # noqa: FBT003
    assert [row["outcome"] for row in lines.lines("relation")] == ["error"]


@pytest.mark.parametrize(
    ("group", "confidence", "kwargs", "written"),
    [
        ("web_search", 0.95, {}, True),
        ("calendar_todo_read", 0.99, {}, True),
        ("comms_write", 0.97, {}, True),
        ("web_search", 0.949, {}, False),
        ("records_notes", 0.99, {}, False),
        ("agents_night", 0.99, {}, False),
        ("none", 0.99, {}, False),
        ("web_search", 0.99, {"tool_at": None}, False),
    ],
)
def test_a_confident_group_with_a_line_is_written_once_the_turn_exists(
    tmp_path: Path, group: str, confidence: float, kwargs: dict[str, Any], *, written: bool,
) -> None:
    """Nothing is written before the decision stage has taken the line (the turn exists)."""
    lines = _Lines(tmp_path, **kwargs)
    lines.say(tool_group=(group, confidence))
    lines.oneshot.begin("T1", "明天天气怎么样", "", False, False)  # noqa: FBT003
    predicted = {"turn_id": "T1", "group": group, "confidence": confidence}
    assert lines.emitted == []
    assert lines.oneshot.take("T1") is not None
    assert lines.oneshot.take("T1") is None
    assert lines.emitted == (
        [("route.tool_predicted", predicted, "T1")] if written else []
    )


def test_a_reply_that_lands_after_the_turn_started_still_writes_the_event_once(
    tmp_path: Path,
) -> None:
    """Taken first, answered later: written when the reply lands, once."""
    lines = _Lines(tmp_path)
    lines.route.hang = True
    lines.say(tool_group=("mail_read", 0.97))
    lines.oneshot.begin("T1", "看一下我的邮件", "", False, False)  # noqa: FBT003
    assert lines.oneshot.take("T1") is not None
    assert lines.emitted == []
    lines.route.release(lines.route.futures[0], lines.route.sent[0][1])
    assert [e[1]["group"] for e in lines.emitted] == ["mail_read"]


def test_the_whole_verdict_is_written_once_the_turn_exists_whatever_its_confidence(
    tmp_path: Path,
) -> None:
    """ADR 0280: intent, relation (with the line it compared) and group, each with its confidence.

    The relation is absent for a first line.
    """
    lines = _Lines(tmp_path)
    lines.say(
        intent=("none", 0.4), relation=("correction", 0.3), tool_group=("web_search", 0.2),
    )
    lines.oneshot.begin("T1", "帮我查明天的天气", "", False, False)  # noqa: FBT003
    lines.oneshot.begin("T2", "不对 后天的", "", False, False)  # noqa: FBT003
    assert lines.read == []  # no turn yet
    assert lines.oneshot.take("T1") is not None
    assert lines.oneshot.take("T2") is not None
    assert lines.oneshot.take("T2") is None
    assert [(kind, turn) for kind, _payload, turn in lines.read] == [
        ("route.oneshot_read", "T1"), ("route.oneshot_read", "T2"),
    ]
    first, second = (payload for _kind, payload, _turn in lines.read)
    assert {k: v for k, v in first.items() if k != "latency_ms"} == {
        "turn_id": "T1", "intent": "none", "intent_confidence": 0.4,
        "tool_group": "web_search", "tool_group_confidence": 0.2,
    }
    assert second["relation"] == "correction"
    assert second["relation_confidence"] == 0.3
    assert second["prev_turn_id"] == "T1"
    assert lines.emitted == []  # 0.2 is under the tool bar: only the record is written


def test_a_reply_that_lands_after_the_turn_started_writes_the_verdict_when_it_lands(
    tmp_path: Path,
) -> None:
    """Taken first, answered later: the verdict is written once, when the reply lands."""
    lines = _Lines(tmp_path)
    lines.route.hang = True
    lines.say(intent=("time", 0.97))
    lines.oneshot.begin("T1", "几点了", "", False, False)  # noqa: FBT003
    assert lines.oneshot.take("T1") is not None
    assert lines.read == []
    lines.route.release(lines.route.futures[0], lines.route.sent[0][1])
    assert [payload["intent"] for _k, payload, _t in lines.read] == ["time"]


def test_a_failed_or_unreadable_reply_writes_no_verdict(tmp_path: Path) -> None:
    """No answer to read, nothing to record; a failing writer never breaks the turn."""
    lines = _Lines(tmp_path)
    lines.say()  # a reply with no answers at all
    lines.oneshot.begin("T1", "嗯这个", "", False, False)  # noqa: FBT003
    assert lines.oneshot.take("T1") is not None
    assert lines.read == []

    def broken(*_args: object) -> None:
        raise OSError

    failing = _Lines(tmp_path / "failing")
    failing.oneshot.emit = broken
    failing.say(intent=("none", 0.9), tool_group=("web_search", 0.99))
    failing.oneshot.begin("T1", "查一下", "", False, False)  # noqa: FBT003
    assert failing.oneshot.take("T1") is not None


def test_the_verdict_lands_in_the_event_log_on_the_turn_through_the_runtime_emitter(
    tmp_path: Path,
) -> None:
    """The runtime's own emitter accepts the payload and correlates it to the turn."""
    path = tmp_path / "events.db"
    open_event_log(path).close()
    lines = _Lines(tmp_path, emit=_event_emitter(path))
    lines.say(intent=("none", 0.9), tool_group=("mail_read", 0.8))
    lines.oneshot.begin("T9", "看一下我的邮件", "", False, False)  # noqa: FBT003
    assert lines.oneshot.take("T9") is not None
    with closing(open_event_log(path)) as conn:
        [event] = list(iter_events_for_turn(conn, "T9", ("route.oneshot_read",)))
    assert (event.payload["intent"], event.payload["tool_group"]) == ("none", "mail_read")


def test_the_verdict_event_is_registered_with_the_l3_owner() -> None:
    """The event the nightly fold reads is a registered L3 type with only turn_id required."""
    schema = EventTypeRegistry.get("route.oneshot_read")
    assert schema is not None
    assert (schema.owner_layer, schema.actor, schema.required_payload) == (
        "L3", "jarvis_runtime", ("turn_id",),
    )


def test_every_proposed_tool_is_noted_next_to_the_prediction(tmp_path: Path) -> None:
    """The dataset pairs the group and its confidence with the tool and arguments called."""
    lines = _Lines(tmp_path)
    lines.say(tool_group=("calendar_todo_read", 0.9))
    lines.oneshot.begin("T1", "今天有什么安排", "", False, False)  # noqa: FBT003
    lines.oneshot.note_tool("T1", "mcp__microsoft__get-calendar-view", {"top": 15})
    lines.oneshot.note_tool("T1", "kept", {})
    lines.oneshot.note_tool("T-typed", "kept", {})  # no voice line: nothing to pair
    assert [
        (row["ref"], row["tool"], row["args"], row["group"], row["confidence"])
        for row in lines.lines("tool")
    ] == [
        ("T1", "mcp__microsoft__get-calendar-view", '{"top": 15}', "calendar_todo_read", 0.9),
        ("T1", "kept", "{}", "calendar_todo_read", 0.9),
    ]
    assert lines.emitted == []  # 0.9 is under the bar: the line is not said, the tool is noted


def test_a_tool_noted_before_the_reply_has_no_group_yet(tmp_path: Path) -> None:
    """A slow Jev leaves the prediction empty, never a made-up one."""
    lines = _Lines(tmp_path)
    lines.route.hang = True
    lines.oneshot.begin("T1", "a", "", False, False)  # noqa: FBT003
    lines.oneshot.note_tool("T1", "web_search", {"query": "x"})
    [row] = lines.lines("tool")
    assert (row["group"], row["confidence"]) == (None, None)


def test_the_state_has_the_speaker_note_the_previous_line_and_the_judged_line() -> None:
    """The shape the experiment measured."""
    assert oneshot_state(
        "上海的", "", over_her=False, conversation=True, prev="查天气", age_s=3.4,
    ).splitlines() == [
        "[The assistant was silent when this was heard; hands-free mode on]",
        "Previous user line (3 s earlier): 查天气",
        "User: 上海的",
    ]


# --- the decision stage reads the same request ------------------------------


def _say(
    tmp_path: Path, words: str, lines: _Lines, jev_endpoint: SurrogateJev,
) -> tuple[Any, sqlite3.Connection, Any]:
    """One utterance through decide() whose request was begun, as the surface does."""
    ctx, conn, llm = build_decide_context(tmp_path, draft_text=LLM_ANSWER)
    route = SurrogateRoute(
        model="typesafe/jev-1.13", min_confidence=0.95, timeout_ms=400,
        clock=jev_endpoint.clock, url=jev_endpoint.url,
    )
    ctx = replace(ctx, tier0_table=TABLE, surrogate_route=route, oneshot=lines.oneshot)
    lines.oneshot.begin("T9", words, "", False, False)  # noqa: FBT003
    trigger = emit_event(
        conn,
        type="utterance.received",
        payload={"turn_id": "T9", "transcript": words, "channel": "inherent_wake"},
        correlation={"turn_id": "T9"},
    )
    return decide(trigger, ctx), conn, llm


def test_the_instant_function_comes_from_the_merged_request(
    tmp_path: Path,
    jev: SurrogateJev,  # noqa: F811
) -> None:
    """A confident instant runs by the Tier 0 path from the one request; no separate call."""
    lines = _Lines(tmp_path)
    lines.say(intent=("time", 0.95), tool_group=("none", 0.99))
    result, conn, llm = _say(tmp_path, _WORDS, lines, jev)
    (ran,) = ran_tier0(conn)
    assert (ran["pattern_id"], ran["caller_principal"]) == ("time_now", "regex_router")
    assert len(llm.requests) == 0
    assert result.response_plan is not None
    assert jev.requests == []
    assert len(lines.route.sent) == 1
    (event,) = logged_events(conn, "route.surrogate_decided")
    assert (event["choice"], event["confidence"], event["accepted"]) == ("time", 0.95, True)


@pytest.mark.parametrize(("choice", "confidence"), [("keep_going", 0.99), ("time", 0.9)])
def test_a_control_word_or_a_weak_choice_leaves_the_turn_to_the_model(
    tmp_path: Path,
    jev: SurrogateJev,  # noqa: F811
    choice: str,
    confidence: float,
) -> None:
    """A control word is not an instant function; below 0.95 nothing runs."""
    lines = _Lines(tmp_path)
    lines.say(intent=(choice, confidence))
    result, conn, llm = _say(tmp_path, _WORDS, lines, jev)
    assert len(llm.requests) == 1
    assert result.response_plan is not None
    assert result.response_plan.text == LLM_ANSWER
    (event,) = logged_events(conn, "route.surrogate_decided")
    assert (event["choice"], event["accepted"], event["error"]) == (choice, False, None)
    assert jev.requests == []


def test_a_turn_without_a_begun_request_asks_on_its_own(tmp_path: Path, jev: SurrogateJev) -> None:  # noqa: F811
    """Typed text and PTT never go through the surface: today's separate question."""
    lines = _Lines(tmp_path)
    ctx, conn, _llm = build_decide_context(tmp_path, draft_text=LLM_ANSWER)
    route = SurrogateRoute(
        model="typesafe/jev-1.13",
        min_confidence=0.9,
        timeout_ms=400,
        clock=jev.clock,
        url=jev.url,
    )
    ctx = replace(ctx, tier0_table=TABLE, surrogate_route=route, oneshot=lines.oneshot)
    trigger = emit_event(
        conn,
        type="utterance.received",
        payload={"turn_id": "T9", "transcript": _WORDS, "channel": "x"},
        correlation={"turn_id": "T9"},
    )
    decide(trigger, ctx)
    assert len(jev.requests) == 1
    assert lines.route.sent == []


def test_a_tool_the_spoken_model_proposes_is_noted_with_the_group_jev_predicted(
    tmp_path: Path,
    jev: SurrogateJev,  # noqa: F811
) -> None:
    """Real spoken stream, one request shared: the dataset pairs group and the tool called."""
    outputs = [[("call", "kept")], [("final_answer", "好。")]]
    words = "看一下我的便签"
    with SpokenPeer(outputs) as peer:
        runtime = spoken_runtime(tmp_path, peer.url)
        lines = _Lines(tmp_path / "jev")
        lines.say(intent=("none", 0.99), tool_group=("records_notes", 0.97))
        route = SurrogateRoute(
            model="typesafe/jev-1.13",
            min_confidence=0.9,
            timeout_ms=400,
            clock=jev.clock,
            url=jev.url,
            parallel=True,
        )
        runtime = replace(
            runtime,
            tier0_table=TABLE,
            surrogate_route=route,
            oneshot=lines.oneshot,
        )
        lines.oneshot.begin("turn-a", words, "", False, False)  # noqa: FBT003
        drive_streaming_turn(runtime, spoken_intent(runtime.conn, "turn-a", words))
    assert jev.requests == []
    [row] = lines.lines("tool")
    assert (row["ref"], row["tool"], row["group"], row["confidence"]) == (
        "turn-a",
        "kept",
        "records_notes",
        0.97,
    )
    assert lines.emitted == []  # records_notes has no line


# --- config -----------------------------------------------------------------


def _config(**changes: Any) -> dict[str, Any]:  # noqa: ANN401 - config values
    block: dict[str, Any] = {
        "enabled": True,
        "model": "m",
        "timeout_ms": 800,
        "zdr": True,
        "relation": {"enabled": True, "at": 0.98, "window_s": 30},
        "tool_line": {"enabled": True, "at": 0.95, "groups": ["web_search"]},
        **changes,
    }
    return {
        "realtime": {
            "surrogate_route": {"enabled": True},
            "jev_words": {"enabled": True, "at": 0.95, "timeout_ms": 800, "max_chars": 24},
            "jev_oneshot": block,
        },
    }


def test_the_block_is_off_unless_enabled_and_bad_values_stop_boot(tmp_path: Path) -> None:
    """Off or absent is None; every wrong key stops boot with the file named."""
    path = tmp_path / "jarvis.yaml"
    assert _jev_oneshot({}, path, FakeClock(), None, TABLE) is None
    assert _jev_oneshot(_config(enabled=False), path, FakeClock(), None, TABLE) is None
    built = _jev_oneshot(_config(), path, FakeClock(), None, TABLE)
    assert built is not None
    assert built.words_enabled
    quiet = _config(relation={"enabled": False, "at": 0.98, "window_s": 30})
    assert _jev_oneshot(quiet, path, FakeClock(), None, TABLE) is not None
    bad_blocks = (
        {"model": ""},
        {"timeout_ms": 0},
        {"relation": {"enabled": True, "at": 1.5, "window_s": 30}},
        {"relation": {"enabled": True, "at": 0.98, "window_s": 0}},
        {"tool_line": {"enabled": True, "at": 0, "groups": []}},
        {"tool_line": {"enabled": True, "at": 0.95, "groups": ["not_a_group"]}},
        {"tool_line": {"enabled": True, "at": 0.95, "groups": ["none"]}},
    )
    for bad in bad_blocks:
        with pytest.raises(RuntimeBootstrapError):
            _jev_oneshot(_config(**bad), path, FakeClock(), None, TABLE)
    no_route = _config()
    no_route["realtime"]["surrogate_route"] = {"enabled": False}
    with pytest.raises(RuntimeBootstrapError, match="surrogate_route"):
        _jev_oneshot(no_route, path, FakeClock(), None, TABLE)


def test_without_jev_words_the_surface_asks_nothing(tmp_path: Path) -> None:
    """The control words stay off unless realtime.jev_words is on."""
    config = _config()
    config["realtime"]["jev_words"] = {"enabled": False}
    built = _jev_oneshot(config, tmp_path / "jarvis.yaml", FakeClock(), None, TABLE)
    assert built is not None
    assert not built.words_enabled
    assert built.ask("T1", "x", "", False, True) is None  # noqa: FBT003


def test_the_shipped_config_has_the_block_off_and_it_boots_when_switched_on(
    tmp_path: Path,
) -> None:
    """Every switch ships off; the shipped values are valid as they stand."""
    config = yaml.safe_load((repo_root() / "config" / "jarvis.yaml").read_text("utf-8"))
    block = config["realtime"]["jev_oneshot"]
    assert (block["enabled"], block["relation"]["enabled"], block["tool_line"]["enabled"]) == (
        False,
        False,
        False,
    )
    assert (block["relation"]["at"], block["relation"]["window_s"]) == (0.98, 30)
    assert block["tool_line"]["at"] == 0.95
    path = tmp_path / "jarvis.yaml"
    assert _jev_oneshot(config, path, FakeClock(), None, TABLE) is None
    block["enabled"] = block["relation"]["enabled"] = block["tool_line"]["enabled"] = True
    config["realtime"]["surrogate_route"]["enabled"] = True
    config["realtime"]["jev_words"]["enabled"] = True
    assert _jev_oneshot(config, path, FakeClock(), None, TABLE) is not None
