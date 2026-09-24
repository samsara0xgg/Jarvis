"""Conversation tools request plugin UI; they never connect or authorize."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from jarvis.execution.tools import Tool, ToolContext
from jarvis.shared import CallerPrincipal

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping


def plugin_panel_tools(
    catalog: Callable[[], dict[str, Any]],
    request: Callable[[Mapping[str, Any], ToolContext], dict[str, Any]],
) -> tuple[Tool, ...]:
    """Keep discovery available even when zero remote tools are connected."""
    return (
        Tool(
            name="list_plugins",
            description=(
                "List available plugins, including disconnected plugins, their capabilities "
                "and connection status. Use this when the user asks to connect an app or "
                "when a named app is unavailable. tool_search only finds connected tools."
            ),
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=lambda _args, _ctx: catalog(),
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L0",
            read_only=True,
            max_result_chars=24_000,
        ),
        Tool(
            name="open_plugin",
            description=(
                "Open Resonance's plugin connection/details panel. This only opens UI: "
                "the user must click Connect before any authorization or setup. Use after "
                "list_plugins when the user explicitly asks to connect/manage a named app, "
                "or requests a task in that named app which needs connection. In these cases "
                "call this tool directly; do not ask permission to open the panel. Set "
                "continue_task=true only for that unfinished task, false for connection "
                "or management alone. For unrequested app suggestions, mention the app "
                "in conversation and wait instead of opening a panel. Do not claim "
                "connection succeeded or invent results while the panel is pending."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "plugin_id": {"type": "string"},
                    "purpose": {
                        "type": "string",
                        "description": "Short task purpose in user's language.",
                    },
                    "continue_task": {"type": "boolean"},
                },
                "required": ["plugin_id", "continue_task"],
                "additionalProperties": False,
            },
            handler=request,
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L0",
            read_only=True,
        ),
    )
