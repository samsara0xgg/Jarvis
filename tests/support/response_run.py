"""A runtime with the response-run lifecycle, scripted decide() and the rows it leaves."""

from __future__ import annotations

import contextlib
import json
import sqlite3
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal

from jarvis import runtime as runtime_module
from jarvis.decision.gates import ResponsePlan
from jarvis.decision.llm import LLMClient
from jarvis.decision.llm_session import LLMSessionFactory
from jarvis.decision.response_run import (
    ResponseRun,
    ResponseRunFacts,
    ResponseRunRegistry,
    legacy_full_text_policy,
    start_response_run,
)
from jarvis.deployment import bootstrap_runtime
from jarvis.execution.tools import ActionLifecycle, build_default_registry
from jarvis.runtime import JarvisRuntime, _wave4_response_flags, drive_turn
from jarvis.shared.realtime import Wave1FeatureFlags, stable_response_group_id
from jarvis.state.committed_event_bus import CommittedEventBus
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.state.response_runs import append_response_started

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import pytest

    from jarvis.decision import DecideContext, DecideResult
    from jarvis.shared import Event

PLAN_TEXT = "已完成。"


PLAN_HASH = "hash-fixed"


def rows_of(conn: sqlite3.Connection, event_type: str) -> list[tuple[Any, ...]]:
    """Return ``(payload_json, source_event_id, event_uid)`` rows of a type."""
    return list(
        conn.execute(
            "SELECT payload_json, source_event_id, event_uid FROM events "
            "WHERE type = ? ORDER BY id ASC",
            (event_type,),
        ),
    )


def event_count(conn: sqlite3.Connection, event_type: str) -> int:
    """Count rows of one event type."""
    row = conn.execute("SELECT COUNT(*) FROM events WHERE type = ?", (event_type,)).fetchone()
    assert row is not None
    return int(row[0])


def payloads_of(conn: sqlite3.Connection, event_type: str) -> list[dict[str, Any]]:
    """Return the decoded payloads of one event type in append order."""
    return [json.loads(row[0]) for row in rows_of(conn, event_type)]


def response_plan(text: str = PLAN_TEXT, response_hash: str = PLAN_HASH) -> ResponsePlan:
    """Build a fixed approved ResponsePlan."""
    return ResponsePlan(
        text=text,
        permission="allow_completion_language",
        downgrade_required=False,
        active_claim_levels=(),
        response_hash=response_hash,
        output_risk_class="routine",
        required_gate_mode="full_text",
    )


LLM_CONFIG: dict[str, Any] = {
    "provider": "openai",
    "default_preset": "fast",
    "presets": {
        "fast": {
            "provider": "openai",
            "model": "gpt-fast",
            "base_url": "https://example.invalid/fast",
            "max_tokens": 64,
        },
        "deep": {
            "provider": "openai",
            "model": "gpt-deep",
            "base_url": "https://example.invalid/deep",
            "max_tokens": 128,
        },
    },
}


def realtime_config(
    *,
    lifecycle: bool,
    cancel: bool,
    wave1: bool = True,
    enabled: bool = True,
    cancel_timeout_ms: int = 500,
) -> dict[str, Any]:
    """Build a config mapping for the Wave-4A flag graph."""
    return {
        "realtime": {
            "enabled": enabled,
            "concurrency_safety": {
                "transactional_event_append": wave1,
                "lifecycle_terminal_cas": wave1,
                "confirmation_dispatch_outbox": False,
                "exactly_once_cost_accounting": False,
            },
            "response": {
                "response_run_lifecycle": lifecycle,
                "independent_response_cancel": cancel,
                "cancel_timeout_ms": cancel_timeout_ms,
            },
        },
    }


def make_runtime(
    tmp_path: Path,
    *,
    lifecycle: bool = False,
    cancel: bool = False,
    cancel_timeout_ms: int = 500,
) -> JarvisRuntime:
    """Assemble a JarvisRuntime wired exactly the way bootstrap wires one."""
    paths = bootstrap_runtime(tmp_path)
    conn = open_event_log(paths.event_log)
    config = realtime_config(
        lifecycle=lifecycle,
        cancel=cancel,
        cancel_timeout_ms=cancel_timeout_ms,
    )
    flags = _wave4_response_flags(config)
    return JarvisRuntime(
        config=config,
        runtime_paths=paths,
        conn=conn,
        tool_registry=build_default_registry(),
        lifecycle=ActionLifecycle(),
        llm_client=LLMClient(LLM_CONFIG),
        system_prompt="",
        wave1_features=Wave1FeatureFlags(
            transactional_event_append=True,
            lifecycle_terminal_cas=True,
        ),
        response_flags=flags,
        llm_session_factory=(
            LLMSessionFactory(LLM_CONFIG) if flags.response_run_lifecycle else None
        ),
        response_runs=(ResponseRunRegistry() if flags.independent_response_cancel else None),
        committed_event_bus=CommittedEventBus(),
    )


def drive_turn_on_own_connection(
    runtime: JarvisRuntime,
    **kwargs: Any,  # noqa: ANN401 - forwarded verbatim to drive_turn.
) -> Any:  # noqa: ANN401 - RunTurnResult; the tests only assert on the log.
    """Run ``drive_turn`` the way the daemon's turn worker thread runs it.

    ``open_event_log`` uses ``check_same_thread=True``, so the connection has
    to be opened *inside* the worker thread — this mirrors
    ``inherent_loop._drive_turn_in_worker_thread`` exactly. The registry and
    the committed-event bus are shared by reference through
    :func:`dataclasses.replace`, which is what lets the cancelling thread see
    the run this one registers.
    """
    conn = open_event_log(runtime.runtime_paths.event_log)
    try:
        return drive_turn(replace(runtime, conn=conn), **kwargs)
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()


def emit_intent(conn: sqlite3.Connection, turn_id: str, transcript: str = "你好") -> Event:
    """Emit the ``surface.user_intent`` row a turn is driven from."""
    return emit_event(
        conn,
        type="surface.user_intent",
        payload={
            "transcript": transcript,
            "turn_id": turn_id,
            "channel": "cli_stdin",
            "language": "zh-CN",
        },
        correlation={"turn_id": turn_id},
    )


def script_decide(
    monkeypatch: pytest.MonkeyPatch,
    behavior: Callable[[Event, DecideContext], DecideResult],
) -> None:
    """Replace the composition root's ``decide`` with a scripted stub."""
    monkeypatch.setattr(runtime_module, "decide", behavior)


def final_result(plan: ResponsePlan | None = None) -> Callable[..., Any]:
    """Return a decide stub that finishes immediately with one plan."""

    def _decide(_trigger: Event, _ctx: DecideContext) -> Any:  # noqa: ANN401
        return SimpleNamespace(
            response_plan=plan if plan is not None else response_plan(),
            events_emitted=(),
            stream_failure=None,
            written_apart=False,
            reaction=None,
            route=None,
            last_gate_event_uid=None,
            turn_id=None,
            attention_channel="voice_notify",
        )

    return _decide


def seed_started(path: Path, response_id: str, turn_id: str) -> ResponseRunFacts:
    """Seed one durable ``response.started`` and return its facts."""
    conn = open_event_log(path)
    try:
        intent = emit_intent(conn, turn_id)
        group_id = stable_response_group_id(turn_id)
        started = append_response_started(
            conn,
            payload={
                "response_id": response_id,
                "response_group_id": group_id,
                "turn_id": turn_id,
                "phase": "final",
                "channel": "both",
                "emission_mode": "full_text",
                "output_risk_class": "unknown",
                "required_gate_mode": "full_text",
                "policy_hash": "policy",
                "active_subject_ref": "unknown",
                "evidence_snapshot_hash": "evidence",
            },
            source_event_id=intent.event_uid,
            correlation={"turn_id": turn_id},
        )
    finally:
        conn.close()
    return ResponseRunFacts(
        response_id=response_id,
        response_group_id=group_id,
        turn_id=turn_id,
        started_event_uid=started.event_uid,
    )


def open_run(
    runtime: JarvisRuntime,
    *,
    response_id: str,
    turn_id: str,
    confirmed_playback: Literal[
        "interrupt_expected_playback_generation",
        "ignore",
    ] = "interrupt_expected_playback_generation",
) -> ResponseRun:
    """Open and register one live ResponseRun with an explicit interrupt policy."""
    facts = seed_started(runtime.runtime_paths.event_log, response_id, turn_id)
    assert runtime.response_runs is not None
    factory = LLMSessionFactory(LLM_CONFIG)
    snapshot = factory.snapshot(None)
    run = start_response_run(
        runtime.conn,
        turn_id=turn_id,
        trigger_event_uid=facts.started_event_uid,
        request_client=factory.create(snapshot, response_id=response_id),
        policy=legacy_full_text_policy(
            evidence_snapshot_hash="e",
            preset_snapshot_hash=snapshot.snapshot_hash,
        ),
        response_id=response_id,
        confirmed_playback=confirmed_playback,
    )
    runtime.response_runs.register(run)
    return run
