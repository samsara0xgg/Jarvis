"""A scripted step model and the batch that runs the turn guards on it."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

from jarvis.decision import DecideContext, decide
from jarvis.execution.tools import ActionLifecycle, build_default_registry
from jarvis.state.event_log import emit_event, open_event_log
from tests.support.runtime_paths import StubRuntimePaths

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence
    from pathlib import Path

    from jarvis.decision import LifecycleLike, RuntimePathsLike, ToolRegistryLike
    from jarvis.decision.llm import LLMClient
    from tests.support.model import ScriptedModel

def logged_guards(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT payload_json FROM events WHERE type='turn.guard_fired'")
    return [json.loads(row[0]) for row in rows.fetchall()]


def tool_texts(messages: Sequence[dict[str, Any]]) -> list[str]:
    return [m["content"] for m in messages if m["role"] == "tool"]


def guard_context(  # noqa: PLR0913 - one argument per thing a turn's context can carry
    conn: sqlite3.Connection,
    tmp_path: Path,
    llm: object,
    budget: int,
    live_context: tuple[str, ...] = (),
    history: Sequence[dict[str, Any]] = (),
) -> DecideContext:
    paths = StubRuntimePaths(
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
        live_context=live_context,
        history=history,
    )


def guard_batch(
    tmp_path: Path,
    model: ScriptedModel,
    *,
    budget: int = 8,
    live_context: tuple[str, ...] = (),
    history: Sequence[dict[str, Any]] = (),
) -> tuple[str, sqlite3.Connection]:
    conn = open_event_log(tmp_path / "events.db")
    trigger = emit_event(
        conn,
        type="surface.user_intent",
        payload={"transcript": "把明天开会记一下", "turn_id": "T_guard"},
        correlation={"turn_id": "T_guard"},
    )
    result = decide(trigger, guard_context(conn, tmp_path, model, budget, live_context, history))
    assert result.response_plan is not None
    return result.response_plan.text, conn
