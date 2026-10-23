"""A scripted step model and the batch that runs the turn guards on it."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

from jarvis.decision import DecideContext, decide
from jarvis.decision.llm_session import LLMSessionFactory
from jarvis.decision.packet import assemble_packet
from jarvis.decision.pre_route import RoutineStreamRoute, spoken_risk_context
from jarvis.decision.response_run import start_response_run
from jarvis.decision.stream_gate import spoken_stream_policy
from jarvis.execution.tools import ActionLifecycle, build_default_registry
from jarvis.runtime.stream_bridge import LoopBoundTokenStream
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.surface.stream_emission import emit_permitted_segment
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
    registry: object | None = None,
) -> DecideContext:
    """A turn's context; ``registry`` replaces the default tool registry."""
    paths = StubRuntimePaths(
        event_log=tmp_path / "events.db", artifacts_root=tmp_path / "artifacts"
    )
    paths.artifacts_root.mkdir(parents=True, exist_ok=True)
    return DecideContext(
        conn=conn,
        runtime_paths=cast("RuntimePathsLike", paths),
        tool_registry=cast("ToolRegistryLike", registry or build_default_registry()),
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


def spoken_turn(  # noqa: PLR0913 - one argument per thing a spoken turn can carry
    tmp_path: Path,
    model: ScriptedModel,
    *,
    structured: bool = False,
    live_context: tuple[str, ...] = (),
    history: Sequence[dict[str, Any]] = (),
    budget: int = 8,
    registry: object | None = None,
    words: str = "把明天开会记一下",
) -> tuple[Any, sqlite3.Connection]:
    """Run one spoken turn through ``decide()`` on the scripted model; the caller closes it.

    ``words`` is what he said and ``registry`` the tools the model has.
    """
    conn = open_event_log(tmp_path / "events.db")
    trigger = emit_event(
        conn,
        type="utterance.received",
        payload={"transcript": words, "turn_id": "T_say", "channel": "speech"},
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
        open_stream=lambda handle: LoopBoundTokenStream(
            handle, cancellation_token=run.cancellation_token
        ),
        emit_segment=lambda permit, text: emit_permitted_segment(
            conn, permit, text, query=context.user_request,
            attention_channel=context.attention_channel,
        ).event,
        segment_guard=run.admission_guard,
        structured=structured,
    )
    ctx = guard_context(conn, tmp_path, model, budget, live_context, history, registry)
    return decide(trigger, replace(ctx, routine_stream=route)), conn


def segments(conn: sqlite3.Connection) -> str:
    """Everything the turn put out as spoken segments, in order."""
    rows = conn.execute(
        "SELECT payload_json FROM events WHERE type LIKE '%segment%' OR type LIKE '%chunk%'"
    ).fetchall()
    return "".join(str(json.loads(r[0]).get("text", "")) for r in rows)


def claims_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """The turn's ``answer.claims_checked`` rows, in order."""
    rows = conn.execute("SELECT payload_json FROM events WHERE type='answer.claims_checked'")
    return [json.loads(row[0]) for row in rows.fetchall()]
