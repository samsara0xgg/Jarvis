"""A tool call whose arguments do not parse is not run: the model is told and may call again.

Drives the real ``decide()`` batch loop, dispatcher and event log with a scripted model whose
first call to a tool with only optional arguments (``kept``) is cut off mid-arguments. A call
dispatched with ``{}`` would run and answer as if the model had asked for it.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
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
from jarvis.execution.tools import ActionLifecycle, build_default_registry
from jarvis.state.event_log import emit_event, open_event_log

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from pathlib import Path

ANSWER = "好的，我再试一次。"  # noqa: RUF001 — fullwidth punctuation, a Chinese reply.


@dataclass(frozen=True)
class _StubRuntimePaths:
    event_log: Path
    artifacts_root: Path


class _CutOffClient:
    """Calls ``kept`` with ``arguments_json`` on its first request, then answers in words."""
    native_tool_search = False  # Jarvis's own tool_search (ADR 0287)
    model = "stub-model"
    last_input_tokens = 0
    last_output_tokens = 0
    last_finish_reason = "stop"

    def __init__(self, arguments_json: str) -> None:
        self.arguments_json = arguments_json
        self.requests: list[list[dict[str, Any]]] = []

    @contextmanager
    def fresh_context(self) -> Iterator[_CutOffClient]:
        yield self

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str,  # noqa: ARG002
        tools: list[dict[str, Any]] | None = None,  # noqa: ARG002
        tool_choice: str | None = "auto",  # noqa: ARG002
    ) -> ChatResult:
        self.requests.append(json.loads(json.dumps(messages)))
        first = len(self.requests) == 1
        calls = (ToolCall(call_id="c1", name="kept", arguments_json=self.arguments_json),)
        return ChatResult(
            text=None if first else ANSWER,
            tool_calls=calls if first else (),
            finish_reason="tool_calls" if first else "stop",
            input_tokens=0,
            output_tokens=0,
            raw={},
            model_used="stub-model",
            tokens_in=0,
            tokens_out=0,
        )


def _run(tmp_path: Path, llm: _CutOffClient) -> tuple[str, sqlite3.Connection]:
    conn = open_event_log(tmp_path / "events.db")
    paths = _StubRuntimePaths(
        event_log=tmp_path / "events.db", artifacts_root=tmp_path / "artifacts",
    )
    paths.artifacts_root.mkdir(parents=True, exist_ok=True)
    ctx = DecideContext(
        conn=conn,
        runtime_paths=cast("RuntimePathsLike", paths),
        tool_registry=cast("ToolRegistryLike", build_default_registry()),
        lifecycle=cast("LifecycleLike", ActionLifecycle()),
        llm_client=cast("LLMClient", llm),
        system_prompt="stub system prompt",
    )
    trigger = emit_event(
        conn,
        type="surface.user_intent",
        payload={"transcript": "我记过什么", "turn_id": "T_cut"},
        correlation={"turn_id": "T_cut"},
    )
    result = decide(trigger, ctx)
    assert result.response_plan is not None
    return result.response_plan.text, conn


def _count(conn: sqlite3.Connection, event_type: str) -> int:
    return int(
        conn.execute("SELECT COUNT(*) FROM events WHERE type=?", (event_type,)).fetchone()[0]
    )


@pytest.mark.parametrize(
    "arguments_json", ['{"page": 2', "not json at all", "[1, 2]", '"page"', "3"],
)
def test_a_call_whose_arguments_do_not_parse_is_not_run_and_the_model_is_told(
    tmp_path: Path, arguments_json: str,
) -> None:
    """Nothing is proposed or dispatched; the next request carries the tool error for that call."""
    llm = _CutOffClient(arguments_json)
    text, conn = _run(tmp_path, llm)
    try:
        assert ANSWER in text
        for event_type in ("action.proposed", "action.dispatched", "action.result_observed"):
            assert _count(conn, event_type) == 0, event_type
        assert len(llm.requests) == 2
        told = [m for m in llm.requests[1] if m.get("role") == "tool"]
        assert [m["tool_call_id"] for m in told] == ["c1"]
        error = json.loads(told[0]["content"])["error"]
        assert error.startswith("arguments were not ")
    finally:
        conn.close()


def test_the_error_names_where_the_json_broke(tmp_path: Path) -> None:
    """A cut-off call's error carries the parser's own position, so the model can see why."""
    llm = _CutOffClient('{"page": 2')
    _, conn = _run(tmp_path, llm)
    try:
        told = [m for m in llm.requests[1] if m.get("role") == "tool"]
        assert "char 10" in json.loads(told[0]["content"])["error"]
    finally:
        conn.close()


@pytest.mark.parametrize("arguments_json", ["", "{}"])
def test_a_call_with_no_arguments_is_still_run(tmp_path: Path, arguments_json: str) -> None:
    """An empty argument string is a call with none: the tool runs and its result goes back."""
    llm = _CutOffClient(arguments_json)
    _, conn = _run(tmp_path, llm)
    try:
        assert _count(conn, "action.proposed") == 1
        assert _count(conn, "action.result_observed") == 1
        told = [m for m in llm.requests[1] if m.get("role") == "tool"]
        assert "error" not in json.loads(told[0]["content"])
    finally:
        conn.close()
