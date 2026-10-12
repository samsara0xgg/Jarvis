"""ADR 0239: a call the model repeats is not run again; an answer no call backs goes back once.

Two acceptance checks. The first reads the guards' pure functions over a table of (answer,
calls) pairs, Chinese and English, the evidence phrases and the plain talk that must not trip
them. The second drives the real ``decide()`` loops, dispatcher and event log with a scripted
model (no provider is called): the batch loop, then the spoken route with and without the
structured reply (ADR 0114).
"""

# ruff: noqa: RUF001 - the replies under test are Chinese, with fullwidth punctuation
from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, cast

import pytest

from jarvis.decision import (
    DecideContext,
    LifecycleLike,
    RuntimePathsLike,
    ToolRegistryLike,
    decide,
)
from jarvis.decision.llm import ChatResult, LLMClient, ToolCall
from jarvis.decision.llm_session import LLMSessionFactory
from jarvis.decision.llm_stream import LLMTextDelta, LLMToolCallCompleted
from jarvis.decision.packet import assemble_packet
from jarvis.decision.pre_route import RoutineStreamRoute, spoken_risk_context
from jarvis.decision.response_run import start_response_run
from jarvis.decision.stream_gate import spoken_stream_policy
from jarvis.decision.turn_guards import (
    REPEATED_CALL,
    REPEATED_FAILED,
    UNBACKED_NOTE,
    TurnCall,
    repeated_call,
    unbacked_claim,
    unbacked_said,
)
from jarvis.execution.tools import ActionLifecycle, build_default_registry
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.surface.stream_emission import emit_permitted_segment

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Iterator, Sequence
    from pathlib import Path

    from jarvis.decision.llm_stream import LLMStreamEvent

WROTE = TurnCall("create_memo", read_only=False, ok=True)
WRITE_FAILED = TurnCall("create_memo", read_only=False, ok=False)
READ = TurnCall("recall", read_only=True, ok=True)
DID, CHECK = "did it", "will check"


@pytest.mark.parametrize(
    ("text", "calls", "expected"),
    [
        # What the replay exam caught (Haiku 16 turns, Luna 9, Sonnet 4): claimed, never done.
        ("记下了", [], DID),
        ("我记住啦", [], DID),
        ("已记下：10月3日…", [READ], DID),
        ("好的，已经设好了，会提醒你。", [], DID),
        ("发出去了。", [], DID),
        ("已经放到屏幕上了。", [], DID),
        ("I've put the findings on screen", [], DID),
        ("Saved. Want me to add a reminder?", [], DID),
        ("Noted.", [], DID),
        ("I'll remind you at 3.", [], DID),
        ("The reminder is set.", [], DID),
        ("我会记住你说的。", [], DID),  # an emotional reply still promises a save: call remember
        ("记下了", [WRITE_FAILED], DID),  # the write failed, so it did not happen
        ('{"spoken": "记下了。", "written": ""}', [], DID),  # the structured reply (ADR 0114)
        # Announced, never looked up.
        ("我查一下10月1日的记录。", [], CHECK),
        ("我去看看", [], CHECK),
        ("让我看看", [], CHECK),
        ("稍等", [], CHECK),
        ("I need to check tomorrow's forecast", [], CHECK),
        ("Let me check that for you.", [], CHECK),
        # Backed: a write succeeded, or the lookup was a call.
        ("记下了", [WROTE], None),
        ("记下了", [READ, WROTE], None),
        ("我查一下", [READ], None),
        ("I'll check", [READ], None),
        # Plain talk, questions and reports of what exists.
        ("你记得吗", [], None),
        ("do you remember", [], None),
        ("Do you remember?", [], None),
        ("你记住了吗？", [], None),
        ("要我记下来吗？", [], None),
        ("Want me to save it?", [], None),
        ("我会一直在", [], None),
        ("提醒你，下午三点有会", [], None),
        ("备忘录里有3条", [READ], None),
        ("已经发现了问题", [], None),
        # Only what he hears counts: the screen part may list what already exists.
        ("<voice>面试是周二下午一点。</voice><document>- 已设置两个提醒</document>", [], None),
        ('{"spoken":"面试是周二下午一点。","written":"提醒：已设置两个提醒"}', [], None),
        ("<voice>好，记下了。</voice><document>- 回家修手机发烫</document>", [], DID),
        ('{"spoken":"已记下：周六吃饭。","written":""}', [], DID),
        # "On screen" is true when the answer carries its written part.
        ('{"spoken":"时间我写在屏幕上了。","written":"- 10:00 产品会"}', [], None),
        ("<voice>I've put the list on screen.</voice><document>- one</document>", [], None),
        ("I've put the findings on screen.", [], DID),
        ("我看你是累了", [], None),
        ("我找到了", [], None),
        ("", [], None),
    ],
)
def test_the_answer_table(text: str, calls: list[TurnCall], expected: str | None) -> None:
    """Each (answer, calls) reads as the guard should: a claim, a promise, or plain talk."""
    assert unbacked_said(text, calls) == expected
    note = unbacked_claim(text, calls)
    assert note == (None if expected is None else UNBACKED_NOTE.format(said=expected))


def test_a_long_answer_that_mentions_a_lookup_is_an_answer() -> None:
    """Only an answer that just announces the lookup is a promise."""
    text = "你上次问过这个：" + "会议定在周五下午，地点在三楼，" * 6 + "我再查一下细节。"
    assert unbacked_said(text, []) is None


RECALL = TurnCall("recall", read_only=True, ok=True, key="a")
RECALL_FAILED = TurnCall("recall", read_only=True, ok=False, key="a")
LISTED = TurnCall("list_memos", read_only=True, ok=True)
MEMO_A = TurnCall("create_memo", read_only=False, ok=True, key="a")
MEMO_X = TurnCall("create_memo", read_only=False, ok=True, key="x")
MEMO_X_FAILED = TurnCall("create_memo", read_only=False, ok=False, key="x")


@pytest.mark.parametrize(
    ("calls", "name", "key", "expected"),
    [
        ([], "recall", "a", None),
        ([RECALL], "recall", "a", REPEATED_CALL),
        ([RECALL], "recall", "b", None),  # other arguments
        ([RECALL], "get_briefing", "a", None),  # other tool
        ([RECALL_FAILED], "recall", "a", REPEATED_FAILED),  # it failed: change something
        ([RECALL, MEMO_X], "recall", "a", None),  # a write since: the repeat may differ
        ([RECALL, MEMO_X_FAILED], "recall", "a", REPEATED_CALL),  # it failed, nothing changed
        ([RECALL, LISTED], "recall", "a", REPEATED_CALL),  # a read changes nothing
        ([MEMO_A, RECALL], "recall", "a", REPEATED_CALL),  # the write came before the read
        ([MEMO_A], "create_memo", "a", REPEATED_CALL),  # the same memo again
    ],
)
def test_the_repeat_table(
    calls: list[TurnCall], name: str, key: str, expected: str | None,
) -> None:
    """A call identical to an earlier one is refused unless a write succeeded in between."""
    assert repeated_call(calls, name, key) == expected


# --- decide() with a scripted model ------------------------------------------------------

Step = str | tuple[str, dict[str, Any]]


@dataclass(frozen=True)
class _StubRuntimePaths:
    event_log: Path
    artifacts_root: Path


class _ScriptedModel:
    """Answers each request with the next step: text, or one tool call (name, arguments)."""

    def __init__(self, *steps: Step) -> None:
        self._steps = list(steps)
        self.requests: list[tuple[list[dict[str, Any]], bool]] = []  # (messages, tools offered)
        self.model = "stub-model"

    @property
    def last_input_tokens(self) -> int | None:
        return 0

    @property
    def last_output_tokens(self) -> int | None:
        return 0

    @property
    def last_finish_reason(self) -> str | None:
        return "stop"

    @contextmanager
    def fresh_context(self) -> Iterator[_ScriptedModel]:
        yield self

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str,  # noqa: ARG002
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",  # noqa: ARG002
    ) -> ChatResult:
        self.requests.append(([dict(m) for m in messages], bool(tools)))
        step = self._steps.pop(0)
        calls: tuple[ToolCall, ...] = ()
        text: str | None = None
        if isinstance(step, str):
            text = step
        else:
            name, args = step
            calls = (ToolCall(f"call{len(self.requests)}", name, json.dumps(args)),)
        return ChatResult(
            text=text,
            tool_calls=calls,
            finish_reason="tool_calls" if calls else "stop",
            input_tokens=0,
            output_tokens=0,
            raw={},
            model_used="stub-model",
            tokens_in=0,
            tokens_out=0,
        )


def _count(conn: sqlite3.Connection, event_type: str) -> int:
    return int(
        conn.execute("SELECT COUNT(*) FROM events WHERE type=?", (event_type,)).fetchone()[0]
    )


def _guards(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT payload_json FROM events WHERE type='turn.guard_fired'")
    return [json.loads(row[0]) for row in rows.fetchall()]


def _tool_texts(messages: Sequence[dict[str, Any]]) -> list[str]:
    return [m["content"] for m in messages if m["role"] == "tool"]


def _notes(messages: Sequence[dict[str, Any]], said: str) -> list[dict[str, Any]]:
    return [m for m in messages if m.get("content") == UNBACKED_NOTE.format(said=said)]


def _context(
    conn: sqlite3.Connection, tmp_path: Path, llm: object, budget: int,
) -> DecideContext:
    paths = _StubRuntimePaths(
        event_log=tmp_path / "events.db", artifacts_root=tmp_path / "artifacts"
    )
    paths.artifacts_root.mkdir(parents=True, exist_ok=True)
    return DecideContext(
        conn=conn,
        runtime_paths=cast("RuntimePathsLike", paths),
        tool_registry=cast("ToolRegistryLike", build_default_registry()),
        lifecycle=cast("LifecycleLike", ActionLifecycle()),
        llm_client=cast("LLMClient", llm),
        system_prompt="stub system prompt",
        max_tool_iterations=budget,
    )


def _batch(
    tmp_path: Path, model: _ScriptedModel, *, budget: int = 8,
) -> tuple[str, sqlite3.Connection]:
    conn = open_event_log(tmp_path / "events.db")
    trigger = emit_event(
        conn,
        type="surface.user_intent",
        payload={"transcript": "把明天开会记一下", "turn_id": "T_guard"},
        correlation={"turn_id": "T_guard"},
    )
    result = decide(trigger, _context(conn, tmp_path, model, budget))
    assert result.response_plan is not None
    return result.response_plan.text, conn


MEMO = ("create_memo", {"text": "明天开会"})


def test_an_identical_repeat_is_not_run_and_gets_the_note(tmp_path: Path) -> None:
    """(a) The same read twice in a turn: one run, and the second call's result is the note."""
    model = _ScriptedModel(("list_memos", {}), ("list_memos", {}), "没有备忘。")
    text, conn = _batch(tmp_path, model)
    try:
        assert text == "没有备忘。"
        assert _count(conn, "action.result_observed") == 1
        assert _tool_texts(model.requests[-1][0])[1] == REPEATED_CALL
        assert _guards(conn) == [{"turn_id": "T_guard", "guard": "repeat", "tool": "list_memos"}]
    finally:
        conn.close()


def test_a_repeat_of_a_call_that_failed_names_the_fix(tmp_path: Path) -> None:
    """The same failing call again is refused with the note that says to change what failed."""
    empty = ("create_memo", {"text": ""})
    model = _ScriptedModel(empty, empty, "没记成。")
    _, conn = _batch(tmp_path, model)
    try:
        assert _count(conn, "action.result_observed") == 1
        failed, repeat = _tool_texts(model.requests[-1][0])
        assert "empty_text" in failed
        assert repeat == REPEATED_FAILED
    finally:
        conn.close()


def test_the_same_memo_twice_is_saved_once(tmp_path: Path) -> None:
    """A write repeated unchanged (create_memo, the same text) is refused too."""
    model = _ScriptedModel(MEMO, MEMO, "记好了。")
    _, conn = _batch(tmp_path, model)
    try:
        assert _count(conn, "memo.captured") == 1
        assert _tool_texts(model.requests[-1][0])[1] == REPEATED_CALL
        assert _count(conn, "turn.guard_fired") == 1
    finally:
        conn.close()


def test_a_repeat_after_a_write_runs(tmp_path: Path) -> None:
    """(b) list, write, list: the second list may differ, so it runs."""
    model = _ScriptedModel(("list_memos", {}), MEMO, ("list_memos", {}), "有一条。")
    _, conn = _batch(tmp_path, model)
    try:
        assert _count(conn, "action.result_observed") == 3
        assert REPEATED_CALL not in _tool_texts(model.requests[-1][0])
        assert _count(conn, "turn.guard_fired") == 0
    finally:
        conn.close()


def test_a_claim_with_no_call_goes_back_once_and_the_call_follows(tmp_path: Path) -> None:
    """(c) 「记下了」 with nothing saved: one note, then the model saves, then the answer stands."""
    model = _ScriptedModel("记下了。", MEMO, "好了，明天开会记在备忘录里了。")
    text, conn = _batch(tmp_path, model)
    try:
        assert text == "好了，明天开会记在备忘录里了。"
        assert len(model.requests) == 3
        second = model.requests[1][0]
        (note,) = _notes(second, DID)
        assert note["role"] == "user"
        assert second[second.index(note) - 1] == {"role": "assistant", "content": "记下了。"}
        assert _count(conn, "memo.captured") == 1
        assert _guards(conn) == [{"turn_id": "T_guard", "guard": "unbacked", "said": DID}]
    finally:
        conn.close()


def test_the_bounce_happens_once_a_turn(tmp_path: Path) -> None:
    """(d) The answer after the note stands, claim or not."""
    model = _ScriptedModel("记下了。", "记下了。")
    text, conn = _batch(tmp_path, model)
    try:
        assert text == "记下了。"
        assert len(model.requests) == 2
        assert _count(conn, "turn.guard_fired") == 1
        assert _count(conn, "turn.ended") == 1
    finally:
        conn.close()


def test_an_announced_lookup_goes_back_and_the_lookup_follows(tmp_path: Path) -> None:
    """「我查一下」 and no call: the note says it will check; the model then looks."""
    model = _ScriptedModel("我查一下。", ("list_memos", {}), "还没有备忘。")
    text, conn = _batch(tmp_path, model)
    try:
        assert text == "还没有备忘。"
        assert _notes(model.requests[1][0], CHECK)
        assert _count(conn, "action.result_observed") == 1
    finally:
        conn.close()


def test_no_bounce_when_the_budget_wraps_the_turn_up(tmp_path: Path) -> None:
    """(e) The budget's own no-tool answer is final, claim or not."""
    wrapped = _ScriptedModel(("list_memos", {"n": 1}), ("list_memos", {"n": 2}), "记下了。")
    text, conn = _batch(tmp_path, wrapped, budget=2)
    try:
        assert text == "记下了。"
        assert len(wrapped.requests) == 3
        assert not wrapped.requests[2][1]  # the wrap-up offers no tools
        assert _count(conn, "turn.guard_fired") == 0
    finally:
        conn.close()


def test_no_bounce_on_the_last_request_the_budget_allows(tmp_path: Path) -> None:
    """There is no request left to run after a note, so the answer stands."""
    last = _ScriptedModel(("list_memos", {}), "记下了。")
    text, conn = _batch(tmp_path, last, budget=2)
    try:
        assert text == "记下了。"
        assert len(last.requests) == 2
        assert _count(conn, "turn.guard_fired") == 0
    finally:
        conn.close()


# --- the spoken route -----------------------------------------------------------------------


class _Stream:
    """One scripted request's typed events, the way the runtime's stream hands them over."""

    def __init__(self, events: list[LLMStreamEvent]) -> None:
        self._events = events

    def __iter__(self) -> Iterator[LLMStreamEvent]:
        return iter(self._events)

    def cancel(self, reason: str) -> None:
        """Never cancelled here."""

    def interrupt_when(self, predicate: Callable[[], bool]) -> None:
        """Nothing here waits on Jev."""

    def close(self) -> None:
        """Nothing to release."""

    @property
    def disposition(self) -> None:
        return None


class _SpokenModel:
    """The client the spoken route opens streams with; ``script`` holds each request's events."""

    provider = "openai"
    model = "stub-model"

    def __init__(self, *requests: list[LLMStreamEvent]) -> None:
        self.script = list(requests)
        self.sent: list[list[dict[str, Any]]] = []

    def request_tier(self, tier: str) -> str:
        return tier

    def stream_events(self, **request: Any) -> object:  # noqa: ANN401 - the client's keyword surface
        self.sent.append([dict(m) for m in request["messages"]])
        return object()


def _say(text: str, *, structured: bool) -> list[LLMStreamEvent]:
    body = json.dumps({"spoken": text, "written": ""}, ensure_ascii=False) if structured else text
    return [
        LLMTextDelta(
            llm_request_id="r", provider_response_id=None, text=body, content_block_index=0,
            phase="final_answer",
        )
    ]


def _propose(name: str, args: dict[str, Any]) -> list[LLMStreamEvent]:
    return [
        LLMToolCallCompleted(
            llm_request_id="r", provider_response_id=None, call_index=0, call_id="c1",
            name=name, arguments_json=json.dumps(args),
        )
    ]


@pytest.mark.parametrize("structured", [False, True])
def test_the_spoken_route_bounces_a_claim_and_speaks_the_answer_after_it(
    tmp_path: Path, *, structured: bool,
) -> None:
    """The first answer is already spoken; the call and the answer after the note follow it."""
    conn = open_event_log(tmp_path / "events.db")
    try:
        model = _SpokenModel(
            _say("记下了。", structured=structured),
            _propose(*MEMO),
            _say("好了，明天开会记在备忘录里了。", structured=structured),
        )
        trigger = emit_event(
            conn,
            type="utterance.received",
            payload={"transcript": "把明天开会记一下", "turn_id": "T_say", "channel": "speech"},
            correlation={"turn_id": "T_say"},
        )
        context = spoken_risk_context(
            assemble_packet(trigger, conn), response_id="R_say", turn_id="T_say",
        )
        policy = spoken_stream_policy(context, preset_snapshot_hash="b" * 64)
        factory = LLMSessionFactory(
            {"provider": "openai", "model": "synthetic", "base_url": "https://example.invalid"}
        )
        run = start_response_run(
            conn,
            turn_id="T_say",
            trigger_event_uid=trigger.event_uid,
            request_client=factory.create(factory.snapshot(), response_id="R_say"),
            policy=policy,
            response_id="R_say",
        )
        route = RoutineStreamRoute(
            policy=policy,
            context=context,
            open_stream=lambda _handle: _Stream(model.script.pop(0)),
            emit_segment=lambda permit, text: emit_permitted_segment(
                conn, permit, text, query=context.user_request,
                attention_channel=context.attention_channel,
            ).event,
            segment_guard=run.admission_guard,
            structured=structured,
        )
        ctx = _context(conn, tmp_path, model, 8)
        result = decide(trigger, replace(ctx, routine_stream=route))
        assert result.response_plan is not None
        spoken = result.response_plan.text
        assert spoken.index("记下了") < spoken.index("记在备忘录里了")
        assert len(model.sent) == 3
        assert _notes(model.sent[1], DID)
        assert _count(conn, "memo.captured") == 1
        assert _guards(conn) == [{"turn_id": "T_say", "guard": "unbacked", "said": DID}]
    finally:
        conn.close()
