"""What the companion shows while a tool is really running — ADR 0115 (runtime).

A pure fold from the Event Log's action rows to one fixed line (a key of the
language table), so the line is driven by the real action lifecycle and costs
no model call. A tool expected to take a second or more shows at once; any
other tool shows the generic line only if it is still running after
:data:`SHOW_AFTER_S`, so a tool that returns at once never flashes text.
"""

from __future__ import annotations

import re
from collections import deque
from typing import TYPE_CHECKING, Final, NamedTuple

from jarvis.shared.lang import SLOW_TOOLS

if TYPE_CHECKING:
    from jarvis.shared import Event

SHOW_AFTER_S: Final[float] = 1.5
"""A tool not on the slow list shows its line only after running this long."""

# MCP reads (mcp__<server>__<tool>) were instant in the log, so they get their
# own line but only once they are slow too.
_MCP_SERVERS: Final[dict[str, str]] = {
    "microsoft": "tool_status.calendar",
    "gmail": "tool_status.mail",
    "notion": "tool_status.notion",
}
_READ_TOOL: Final = re.compile(r"search|list|get|fetch|read|query|view|find", re.IGNORECASE)
_ENDS_TURN: Final = ("turn.ended", "turn.failed", "response.cancelled")
_ENDS_ACTION: Final = (
    "action.result_observed", "action.failed", "action.timeout_assumed", "action.cancelled",
)
EVENT_TYPES: Final[tuple[str, ...]] = (
    "action.proposed", "action.running", *_ENDS_ACTION, *_ENDS_TURN,
)
"""Every row type :meth:`ToolStatus.feed` reads."""


def plan(tool_name: str) -> tuple[str, float]:
    """The language key a running tool shows, and how long it runs before it does."""
    if key := SLOW_TOOLS.get(tool_name):
        return key, 0.0
    parts = tool_name.split("__")
    if len(parts) == 3 and parts[0] == "mcp" and _READ_TOOL.search(parts[2]):  # noqa: PLR2004
        return _MCP_SERVERS.get(parts[1], "tool_status.generic"), SHOW_AFTER_S
    return "tool_status.generic", SHOW_AFTER_S


class Shown(NamedTuple):
    """The line to show now: its turn and its language key."""

    turn_id: str
    key: str


class _Live(NamedTuple):
    turn_id: str
    key: str
    show_at: float


class ToolStatus:
    """The tools running now, from the rows fed in order; the latest one shows."""

    def __init__(self) -> None:
        """Start with no tool known and none running."""
        self._names: dict[str, str] = {}
        self._live: dict[str, _Live] = {}
        self._over: deque[str] = deque(maxlen=64)

    def feed(self, event: Event, now: float) -> None:
        """Fold one action or turn-end row; ``now`` is a monotonic clock reading."""
        payload = event.payload
        turn_id = str(payload.get("turn_id") or (event.correlation or {}).get("turn_id") or "")
        action_id = str(payload.get("action_id", ""))
        if event.type == "action.proposed":
            self._names[action_id] = str(payload.get("tool_name", ""))
        elif event.type == "action.running":
            key, after = plan(self._names.get(action_id, ""))
            self._live[action_id] = _Live(turn_id, key, now + after)
        elif event.type in _ENDS_ACTION:
            self._live.pop(action_id, None)
            self._names.pop(action_id, None)
        else:
            self._live = {a: x for a, x in self._live.items() if x.turn_id != turn_id}
            self._over.append(turn_id)

    def over(self, turn_id: str) -> bool:
        """Whether the turn has ended (or failed, or been cancelled) since it was last fed."""
        return turn_id in self._over

    def shown(self, now: float) -> Shown | None:
        """The latest-started running tool whose time to show has come, if any."""
        due = [x for x in self._live.values() if x.show_at <= now]
        return Shown(due[-1].turn_id, due[-1].key) if due else None
