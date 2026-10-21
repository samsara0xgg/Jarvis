"""A routine turn through drive_turn against a localhost OpenAI-shaped peer."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jarvis.decision.llm import LLMClient
from jarvis.decision.llm_session import LLMSessionFactory
from jarvis.decision.pre_route import load_tool_cues
from jarvis.decision.response_run import ResponseRunRegistry
from jarvis.deployment import bootstrap_runtime
from jarvis.execution.tools import ActionLifecycle, build_default_registry
from jarvis.runtime import JarvisRuntime, _wave4_response_flags, drive_turn
from jarvis.state.committed_event_bus import CommittedEventBus
from jarvis.state.event_log import emit_event, open_event_log

if TYPE_CHECKING:
    from jarvis.shared import Event

_REPO = Path(__file__).resolve().parents[2]


_QUERY = "解释冰为什么融化。"


def routine_runtime(
    tmp_path: Path,
    provider_url: str,
    *,
    routine: bool = True,
    answer_hold_s: float | None = None,
) -> JarvisRuntime:
    """A runtime on the peer at ``provider_url``; ``answer_hold_s`` sets the hold (ADR 0272).

    Left out, the shipped hold of 1.5 s applies to a spoken turn.
    """
    paths = bootstrap_runtime(tmp_path)
    config: dict[str, Any] = {
        "realtime": {
            "enabled": True,
            "concurrency_safety": {
                "confirmation_dispatch_outbox": False,
            },
            "response": {
                "cancel_timeout_ms": 500,
                "routine_streaming": {"enabled": routine},
            },
        },
    }
    if answer_hold_s is not None:
        config["realtime"]["response"]["answer_hold"] = {"seconds": answer_hold_s}
    llm_config: dict[str, Any] = {
        "provider": "openai",
        "model": "fixture-model",
        "base_url": provider_url,
        "api_key_env": "ROUTINE_STREAM_FIXTURE_KEY",
        "max_tokens": 256,
        "timeout_s": 5,
        "max_retries": 0,
    }
    return JarvisRuntime(
        config=config,
        runtime_paths=paths,
        conn=open_event_log(paths.event_log),
        tool_registry=build_default_registry(),
        lifecycle=ActionLifecycle(),
        llm_client=LLMClient(llm_config),
        system_prompt="",
        response_flags=_wave4_response_flags(config),
        llm_session_factory=LLMSessionFactory(llm_config),
        response_runs=ResponseRunRegistry(),
        committed_event_bus=CommittedEventBus(),
        tool_cues=load_tool_cues(_REPO / "config" / "tool_cues.yaml"),
    )


def user_intent(conn: sqlite3.Connection, turn_id: str, transcript: str = _QUERY) -> Event:
    return emit_event(
        conn,
        type="surface.user_intent",
        payload={"transcript": transcript, "turn_id": turn_id, "channel": "cli_stdin"},
        correlation={"turn_id": turn_id},
    )


def drive_streaming_turn(runtime: JarvisRuntime, intent: Event) -> Any:  # noqa: ANN401 - RunTurnResult
    """Run drive_turn the way the daemon worker thread does, on its own connection."""
    conn = open_event_log(runtime.runtime_paths.event_log)
    try:
        return drive_turn(
            replace(runtime, conn=conn),
            user_intent_event=intent,
            available_surfaces=frozenset(),
            streaming_enabled=True,
        )
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()


def logged_rows(
    conn: sqlite3.Connection, *types: str
) -> list[tuple[int, str, dict[str, Any], str | None, str]]:
    """``(id, type, payload, source_event_id, event_uid)`` in append order."""
    placeholders = ",".join("?" for _ in types)
    return [
        (int(row[0]), str(row[1]), json.loads(row[2]), row[3], str(row[4]))
        for row in conn.execute(
            "SELECT id, type, payload_json, source_event_id, event_uid FROM events "  # noqa: S608 - fixed type names
            f"WHERE type IN ({placeholders}) ORDER BY id",
            types,
        )
    ]


def logged_payloads(conn: sqlite3.Connection, event_type: str) -> list[dict[str, Any]]:
    return [row[2] for row in logged_rows(conn, event_type)]


def wait_for(predicate: Any, timeout_s: float = 10) -> None:  # noqa: ANN401 - callable
    deadline = time.monotonic() + timeout_s
    while not predicate():
        assert time.monotonic() < deadline, "timed out waiting"
        time.sleep(0.02)
