"""ADR 0030: a spent tool budget still gets one no-tool request for the answer.

Drives the real ``decide()`` loop, dispatcher and event log with a scripted model that
keeps proposing a read-only tool until the loop bound, then answers only when asked
without tools. Asserts on the spoken text and on the persisted ``cost.recorded`` and
``action.result_observed`` rows.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from jarvis.decision import (
    DecideContext,
    LifecycleLike,
    RuntimePathsLike,
    ToolRegistryLike,
    decide,
)
from jarvis.decision.llm import ChatResult, LLMClient, ToolCall
from jarvis.execution.tools import ActionLifecycle, build_default_registry
from jarvis.state.event_log import emit_event, open_event_log

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from pathlib import Path

ANSWER = "查到了：今天没有网易云音乐的前台记录；微信聊天对象无法从截图确认。"  # noqa: RUF001 — fullwidth punctuation, a Chinese reply.
BUDGET = 3


@dataclass(frozen=True)
class _StubRuntimePaths:
    event_log: Path
    artifacts_root: Path


class _ToolHungryClient:
    """Proposes get_current_time on every request that offers tools; answers otherwise."""

    def __init__(self, *, answer: str | None, fail_without_tools: bool = False) -> None:
        self._answer = answer
        self._fail = fail_without_tools
        self.calls: list[dict[str, Any]] = []
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
    def fresh_context(self) -> Iterator[_ToolHungryClient]:
        yield self

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str,  # noqa: ARG002
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",
    ) -> ChatResult:
        self.calls.append({"tools": tools, "tool_choice": tool_choice, "last": messages[-1]})
        if tools:
            call = ToolCall(
                call_id=f"call{len(self.calls)}", name="get_current_time", arguments_json="{}"
            )
            return self._result(None, (call,))
        if self._fail:
            msg = "provider down"
            raise RuntimeError(msg)
        return self._result(self._answer, ())

    @staticmethod
    def _result(text: str | None, calls: tuple[ToolCall, ...]) -> ChatResult:
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


def _run(tmp_path: Path, llm: _ToolHungryClient) -> tuple[str, sqlite3.Connection]:
    conn = open_event_log(tmp_path / "events.db")
    paths = _StubRuntimePaths(
        event_log=tmp_path / "events.db", artifacts_root=tmp_path / "artifacts"
    )
    paths.artifacts_root.mkdir(parents=True, exist_ok=True)
    ctx = DecideContext(
        conn=conn,
        runtime_paths=cast("RuntimePathsLike", paths),
        tool_registry=cast("ToolRegistryLike", build_default_registry()),
        lifecycle=cast("LifecycleLike", ActionLifecycle()),
        llm_client=cast("LLMClient", llm),
        system_prompt="stub system prompt",
        max_tool_iterations=BUDGET,
    )
    trigger = emit_event(
        conn,
        type="surface.user_intent",
        payload={"transcript": "我今天微信和谁聊过天", "turn_id": "T_budget"},
        correlation={"turn_id": "T_budget"},
    )
    result = decide(trigger, ctx)
    assert result.response_plan is not None
    return result.response_plan.text, conn


def _count(conn: sqlite3.Connection, event_type: str) -> int:
    return int(
        conn.execute("SELECT COUNT(*) FROM events WHERE type=?", (event_type,)).fetchone()[0]
    )


def test_spent_budget_gets_one_no_tool_request_for_the_answer(tmp_path: Path) -> None:
    """Every budgeted request proposed a tool; the answer comes from one extra request."""
    llm = _ToolHungryClient(answer=ANSWER)
    text, conn = _run(tmp_path, llm)
    try:
        assert text == ANSWER
        assert len(llm.calls) == BUDGET + 1
        assert all(call["tools"] for call in llm.calls[:BUDGET])
        final = llm.calls[-1]
        assert final["tools"] is None
        assert final["tool_choice"] is None
        assert final["last"]["role"] == "user"
        assert "不能再调用工具" in final["last"]["content"]
        assert "不是用户的话" in final["last"]["content"]
        # Every tool the model asked for ran and left its observation before the answer.
        assert _count(conn, "action.result_observed") == BUDGET
        results = conn.execute(
            "SELECT payload_json FROM events WHERE type='action.result_observed'"
        ).fetchall()
        assert all(json.loads(row[0])["semantics"] == "observation" for row in results)
        # The answer request is paid for like every other request.
        assert _count(conn, "cost.recorded") == BUDGET + 1
        assert _count(conn, "turn.ended") == 1
    finally:
        conn.close()


def test_failed_answer_request_falls_back_to_a_chinese_limitation(tmp_path: Path) -> None:
    """A provider failure on the answer request ends the turn with fixed Chinese text."""
    llm = _ToolHungryClient(answer=None, fail_without_tools=True)
    text, conn = _run(tmp_path, llm)
    try:
        assert text == "这一轮工具调用次数用完了，还没整理出答案。请把问题拆小一点再问一次。"  # noqa: RUF001 — fullwidth punctuation, the fixed Chinese limitation.
        assert "exhausted" not in text
        assert len(llm.calls) == BUDGET + 1
        assert _count(conn, "cost.recorded") == BUDGET
        assert _count(conn, "turn.ended") == 1
    finally:
        conn.close()


def test_empty_answer_falls_back_too(tmp_path: Path) -> None:
    """An empty reply is not an answer; the same limitation is spoken."""
    llm = _ToolHungryClient(answer="  ")
    text, conn = _run(tmp_path, llm)
    try:
        assert text.startswith("这一轮工具调用次数用完了")
        assert _count(conn, "cost.recorded") == BUDGET + 1
    finally:
        conn.close()
