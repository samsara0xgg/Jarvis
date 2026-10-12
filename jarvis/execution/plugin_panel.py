"""Conversation tools: open the plugin panel and start a connection that needs no typed key.

They also turn a plugin off and remove one on Allen's yes (ADR 0202). The login itself is
approved on a device, never by a tool.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from jarvis.execution.tools import Tool, ToolContext, ToolError
from jarvis.shared import CallerPrincipal

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping


def _by_id(
    act: Callable[[str], dict[str, Any]],
) -> Callable[[Mapping[str, Any], ToolContext], dict[str, Any]]:
    """A handler for a tool whose only argument is a plugin id; a refusal is a tool error."""

    def handler(args: Mapping[str, Any], _ctx: ToolContext) -> dict[str, Any]:
        try:
            return act(str(args.get("plugin_id") or ""))
        except ValueError as exc:
            raise ToolError(str(exc), code="plugin") from exc

    return handler


_PLUGIN_ID_SCHEMA = {
    "type": "object",
    "properties": {"plugin_id": {"type": "string"}},
    "required": ["plugin_id"],
    "additionalProperties": False,
}


def plugin_panel_tools(
    catalog: Callable[[], dict[str, Any]],
    request: Callable[[Mapping[str, Any], ToolContext], dict[str, Any]],
    disable: Callable[[str], dict[str, Any]],
    remove: Callable[[str], dict[str, Any]],
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
                "Open Resonance's plugin connection/details panel. For an app that needs no "
                "typed key this also starts the connection at once: if the app asks for a "
                "login, a link appears in the panel for the user to approve on whichever "
                "device they hold. An app that needs a typed key only opens the panel and "
                "waits for the user to enter it and click Connect. Use after "
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
        Tool(
            name="disable_plugin",
            description=(
                "Turn a connected plugin off when the user asks to turn it off or disable it: "
                "its tools leave and its saved login stays, so turning it on again is quick. "
                "Refused while that plugin is connecting."
            ),
            input_schema=_PLUGIN_ID_SCHEMA,
            handler=_by_id(disable),
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L1",
            read_only=False,
        ),
        Tool(
            name="remove_plugin",
            description=(
                "Remove a plugin when the user asks to remove or delete it: turns it off and "
                "deletes its stored login and saved keys on this device. Asks the user to "
                "confirm first. Access granted at the service itself stays the user's to revoke."
            ),
            input_schema=_PLUGIN_ID_SCHEMA,
            handler=_by_id(remove),
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L3",
            read_only=False,
            requires_confirmation=True,
        ),
    )
