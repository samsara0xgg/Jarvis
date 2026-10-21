"""A smart-home MCP server, a decision context over it, and a way to read the log (ADR 0321)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from jarvis.decision import DecideContext, LifecycleLike, RuntimePathsLike, ToolRegistryLike
from jarvis.decision.confirm_grammar import load_confirm_grammar
from jarvis.execution.tools import ActionLifecycle, build_default_registry
from jarvis.shared.realtime import Wave1FeatureFlags
from jarvis.state.event_log import iter_events_of_types, open_event_log
from jarvis.state.projections import PendingConfirmations
from tests.support.model import Call, Reply, ScriptedModel
from tests.support.plugin_approval import Paths, say

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from jarvis.decision.llm import LLMClient
    from jarvis.execution.mcp_tools import McpServers
    from jarvis.state.projections import PendingConfirmationSlot

REPO = Path(__file__).resolve().parents[2]
LIGHTS_SERVER = REPO / "tests" / "integration" / "mcp_lights_server.py"
LIGHTS_TOOLS = (
    "mcp__lights__list_lights",
    "mcp__lights__set_light",
    "mcp__lights__send_to_cloud",
    "mcp__lights__wipe_bridge",
)
"""Every tool of the stand-in, on the menu at once so a test needs no search."""


def hue_manifest_tools() -> dict[str, Any]:
    """The ``tools`` map the shipped Hue plugin declares: the manifest the tests run behind."""
    manifest = json.loads((REPO / "plugins" / "hue" / ".mcp.json").read_text(encoding="utf-8"))
    return dict(manifest["mcpServers"]["hue"]["tools"])


def lights(tools: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """The stand-in server's entry, with ``tools`` as its manifest (and Allen's) per-tool map."""
    return {
        "lights": {
            "command": sys.executable,
            "args": [str(LIGHTS_SERVER)],
            "tools": dict(tools),
        },
    }


def context(
    tmp_path: Path,
    servers: McpServers,
    llm: ScriptedModel,
    spec: Mapping[str, Mapping[str, Any]],
    *,
    words: ScriptedModel | None = None,
) -> DecideContext:
    """A real ``decide()`` context over the registry, the stand-in server and a fresh log."""
    registry = build_default_registry(confirmation_dispatch_outbox=True)
    for one in servers.connect(spec):
        registry.register(one)
    paths = Paths(event_log=tmp_path / "events.db", artifacts_root=tmp_path / "artifacts")
    paths.artifacts_root.mkdir(parents=True, exist_ok=True)
    reader: Callable[[], LLMClient] | None = (
        None if words is None else (lambda: cast("LLMClient", words))
    )
    return DecideContext(
        conn=open_event_log(paths.event_log),
        runtime_paths=cast("RuntimePathsLike", paths),
        tool_registry=cast("ToolRegistryLike", registry),
        lifecycle=cast("LifecycleLike", ActionLifecycle()),
        llm_client=cast("LLMClient", llm),
        system_prompt="stub system prompt",
        confirm_grammar_table=load_confirm_grammar(REPO / "config" / "confirm_grammar.yaml"),
        wave1_features=Wave1FeatureFlags(confirmation_dispatch_outbox=True),
        words_client=reader,
    )


ROOMS = ("living room", "bedroom", "kitchen")
CARD_TYPES = (
    "confirmation.requested", "confirmation.accepted", "confirmation.rejected", "gate.evaluated",
    "surface.user_intent", "utterance.received",
)


def all_off(rooms: tuple[str, ...] = ROOMS) -> Reply:
    """One model response that asks to switch every room off."""
    return Reply(
        calls=tuple(
            Call("mcp__lights__set_light", {"light": room, "on": False}, f"c{index}")
            for index, room in enumerate(rooms)
        ),
    )


def prompting() -> dict[str, Any]:
    """Hue's manifest, with his per-tool prompt mode on the write so every call needs consent."""
    tools = hue_manifest_tools()
    tools["set_light"] = {**tools["set_light"], "approval_mode": "prompt"}
    return tools


def waiting_card(ctx: DecideContext) -> PendingConfirmationSlot | None:
    """The card the log shows as the current one."""
    return PendingConfirmations.from_events(iter_events_of_types(ctx.conn, CARD_TYPES)).slot


def asked(
    tmp_path: Path,
    servers: McpServers,
    llm: ScriptedModel,
    words: ScriptedModel | None = None,
) -> tuple[DecideContext, str]:
    """Turn 1: the model asks for three lights off at once; returns the context and her words."""
    ctx = context(tmp_path, servers, llm, lights(prompting()), words=words)
    return ctx, say(ctx, "把灯都关了", "T1")


def tool_results(model: ScriptedModel, request: int) -> list[dict[str, Any]]:
    """The tool-result messages one request carried, decoded."""
    return [
        {"is_error": m.get("is_error", False), **json.loads(m["content"])}
        for m in model.requests[request].messages
        if m["role"] == "tool"
    ]
