"""ADR 0251: the voice lane's way to hand a heavy task to the deep lane.

``think_deeper`` is on the voice lane's menu and returns at once; the runtime owns the deep
turn it starts (``jarvis.runtime.deep_lane``). The deep turn sees every tool except this one
and ``tool_search``, with the deferred ones on its menu, through :class:`DeepToolRegistry`.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any, Final, Protocol

from jarvis.execution.tools import Tool, ToolError, UnknownToolError, _turn_of
from jarvis.shared import CallerPrincipal

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping

    from jarvis.execution.tools import (
        ActionLifecycle,
        RawResultBundle,
        RuntimePathsLike,
        ToolContext,
        ToolDefinition,
        ToolRegistry,
    )
    from jarvis.shared import ActionRequest

NAME: Final = "think_deeper"
SEARCH_NAME: Final = "tool_search"
_SCHEMA: Final = {
    "type": "object",
    "properties": {
        "task": {
            "type": "string",
            "description": (
                "What to work out, in his own words, with everything this conversation "
                "already established that the work needs (names, dates, what he decided)."
            ),
        },
    },
    "required": ["task"],
}
DESCRIPTION: Final = (
    "Hand a heavy ask to the deep lane: it works on it in the background with every tool and "
    "many more steps, then tells him the answer when it is ready. Use it when the ask needs "
    "several lookups put together or judged: go through a week of mail and say which need a "
    "reply, plan a weekend around the calendar and the weather, find out why spending went "
    "up, research a topic. Not for what one or two tools answer: do those yourself. Pass task: "
    "his whole ask in his words plus what you already know for it. It returns at once; your "
    "reply is the result's `spoken`, word for word."
)


_HIDDEN: Final = frozenset({
    NAME, SEARCH_NAME,
    "ask_user",  # nobody is there to answer: the turn ends on a question instead of its result
    "open_plugin",  # its resume would run the task text again as a voice-lane plugin turn
    "show_on_dashboard",  # read-only, but it flips the page on his screen while he is elsewhere
})
"""What the deep turn never sees, beyond the writes (ADR 0251). An exception to the write rule
would be an explicit name here or in an allowlist; there is none."""


def _offered(tool: Any) -> bool:  # noqa: ANN401 - Tool or ToolDefinition, both carry the flags
    """Whether the deep turn may use ``tool``: it only reads.

    A tool that asks first is out too: its card would arrive at a random moment, take the one
    confirmation slot from the voice lane, and his next "好" for that lane would fire the write.
    """
    return tool.name not in _HIDDEN and tool.read_only


class DeepHandOff(Protocol):
    """The runtime's deep lane (``jarvis.runtime.deep_lane.DeepLane``)."""

    def hand_over(
        self, conn: sqlite3.Connection, *, task: str, from_turn: str | None,
    ) -> dict[str, Any]:
        """Start the deep turn for ``task``, or answer why one cannot start."""


def build_deep_tool(deep: DeepHandOff | None) -> tuple[Tool, ...]:
    """``think_deeper`` bound to the runtime's deep lane; none when no lane is configured."""
    if deep is None:
        return ()

    def think_deeper(args: Mapping[str, Any], ctx: ToolContext) -> dict[str, Any]:
        task = str(args.get("task") or "").strip()
        if not task:
            msg = "think_deeper: task is empty; pass his ask and what you know for it"
            raise ToolError(msg, code="invalid_argument")
        return deep.hand_over(ctx.conn, task=task, from_turn=_turn_of(ctx.action_id))

    return (
        Tool(
            name=NAME,
            description=DESCRIPTION,
            input_schema=_SCHEMA,
            handler=think_deeper,
            # The regex router: the on-words ("想想", "仔细查") hand over without a model.
            allowed_callers=frozenset({CallerPrincipal.REGEX_ROUTER, CallerPrincipal.JARVIS_LLM}),
            risk_level="L1",
            read_only=False,
        ),
    )


class DeepToolRegistry:
    """The registry as the deep turn sees it (ADR 0251).

    Every deferred tool is on the menu, since the turn has no ``tool_search``; the ones a menu
    tool supersedes stay off (ADR 0242). Nobody watches the turn, so it only gets the tools that
    read, never ``_HIDDEN``. What the menu leaves off
    also refuses to dispatch.
    """

    def __init__(self, inner: ToolRegistry) -> None:
        """Wrap ``inner``; nothing is copied, the view is cut per call."""
        self._inner = inner

    def _view(self, tools: tuple[Any, ...]) -> tuple[Any, ...]:
        superseded = self._inner.superseded()
        return tuple(
            replace(tool, deferred=False) if tool.deferred else tool
            for tool in tools
            if _offered(tool) and not (tool.deferred and tool.name in superseded)
        )

    def get_definitions(self) -> tuple[ToolDefinition | Tool, ...]:
        """Every registered tool the deep turn may call."""
        return self._view(self._inner.get_definitions())

    def for_caller(self, caller_principal: CallerPrincipal) -> tuple[ToolDefinition | Tool, ...]:
        """The caller's tools, deferred ones included."""
        return self._view(self._inner.for_caller(caller_principal))

    def dispatch(
        self,
        action_request: ActionRequest,
        conn: sqlite3.Connection,
        runtime_paths: RuntimePathsLike,
        lifecycle: ActionLifecycle,
    ) -> RawResultBundle:
        """Forward a dispatch of a tool this view offers; refuse every other."""
        if action_request.tool_name not in {t.name for t in self.get_definitions()}:
            msg = f"unknown tool: {action_request.tool_name!r} (deep view)"
            raise UnknownToolError(msg)
        return self._inner.dispatch(action_request, conn, runtime_paths, lifecycle)


__all__ = ["DESCRIPTION", "NAME", "DeepHandOff", "DeepToolRegistry", "build_deep_tool"]
