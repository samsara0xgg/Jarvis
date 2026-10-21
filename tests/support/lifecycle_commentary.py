"""A runtime wired for lifecycle commentary, and the phrases it logged."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from jarvis.decision.response_run import ResponseRunRegistry
from jarvis.deployment import bootstrap_runtime
from jarvis.execution.tools import ActionLifecycle, build_default_registry
from jarvis.runtime import JarvisRuntime, _wave4_response_activation
from jarvis.state.committed_event_bus import CommittedEventBus
from jarvis.state.event_log import open_event_log
from jarvis.surface.cli import parse_response_channels
from tests.support.model import ScriptedModel, ScriptedSessions

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path


def _observer_config(*, commentary: bool) -> dict[str, Any]:
    """Config for a runtime with D6 commentary."""
    return {
        "realtime": {
            "enabled": True,
            "response": {"cancel_timeout_ms": 500},
            "commentary": {"enabled": commentary},
        },
    }


_LLM_CONFIG: dict[str, Any] = {
    "provider": "openai",
    "default_preset": "fast",
    "presets": {
        "fast": {
            "provider": "openai",
            "model": "gpt-fast",
            "base_url": "https://example.invalid/fast",
            "max_tokens": 64,
        },
    },
}


FINAL_ANSWER = "现在是下午三点。"
"""What the scripted model answers when a turn is driven on a commentary runtime."""


def commentary_runtime(
    tmp_path: Path,
    *,
    commentary: bool = True,
    model: ScriptedModel | None = None,
) -> JarvisRuntime:
    """Assemble the runtime the daemon's watchers receive; ``model`` answers its turns."""
    model = ScriptedModel(otherwise=FINAL_ANSWER) if model is None else model
    paths = bootstrap_runtime(tmp_path)
    config = _observer_config(commentary=commentary)
    return JarvisRuntime(
        config=config,
        runtime_paths=paths,
        conn=open_event_log(paths.event_log),
        tool_registry=build_default_registry(),
        lifecycle=ActionLifecycle(),
        llm_client=model.as_llm_client(),
        system_prompt="",
        response_flags=_wave4_response_activation(config).flags,
        llm_session_factory=ScriptedSessions(model, _LLM_CONFIG),
        response_runs=ResponseRunRegistry(),
        committed_event_bus=CommittedEventBus(),
    )


def typed_payloads(conn: sqlite3.Connection, event_type: str) -> list[dict[str, Any]]:
    """Return the decoded payloads of one event type in append order."""
    return [
        json.loads(row[0])
        for row in conn.execute(
            "SELECT payload_json FROM events WHERE type = ? ORDER BY id ASC",
            (event_type,),
        )
    ]


def spoken_phrases(conn: sqlite3.Connection) -> list[str]:
    """Return the voice text of every commentary response, tags stripped."""
    return [
        parse_response_channels(payload["text"]).voice
        for payload in typed_payloads(conn, "surface.response_emitted")
        if payload.get("phase") == "commentary"
    ]


def only_phrase(conn: sqlite3.Connection) -> str:
    """The single commentary phrase this log holds, asserting there is one."""
    spoken = spoken_phrases(conn)
    assert len(spoken) == 1, spoken
    return spoken[0]
