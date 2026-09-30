"""ADR 0093: the conversation's way into the night run the runtime owns."""

from __future__ import annotations

import re
from datetime import time
from typing import TYPE_CHECKING, Any, Final, Protocol

from jarvis.execution.tools import Tool, ToolError
from jarvis.shared import CallerPrincipal

if TYPE_CHECKING:
    from collections.abc import Mapping

    from jarvis.execution.tools import ToolContext

MIN_HOURS: Final = 0.25
MAX_HOURS: Final = 12.0
_CLOCK: Final = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


class NightControl(Protocol):
    """The runtime's night run (``jarvis.runtime.night_run.NightRun``)."""

    def start(
        self, *, hours: float | None, until: time | None, source: str, action_id: str | None,
    ) -> dict[str, Any]:
        """Start a run, or answer the one already running."""

    def end(self, *, action_id: str | None) -> dict[str, Any]:
        """End the running one: put back what it changed; answers when none runs."""


_START_SCHEMA: Final = {
    "type": "object",
    "properties": {
        "hours": {
            "type": "number",
            "minimum": MIN_HOURS,
            "maximum": MAX_HOURS,
            "description": "How long to keep the Mac awake, only when the user said so.",
        },
        "until": {
            "type": "string",
            "description": (
                "Local clock time HH:MM to keep it awake until, instead of hours, only when "
                "the user said one."
            ),
        },
    },
    "required": [],
}
_START_DESCRIPTION: Final = (
    "Start a night run: the user is going to sleep and wants the Mac to keep running "
    "(agents, downloads, builds) with the screen off and the sound muted. Call it right "
    "away; when the user named no length or end time, leave both out and the default "
    "length applies. Never ask how long. Keeps the Mac awake until the deadline, then "
    "lets it sleep; brightness and sound come back when the user gets up. Your reply is "
    "the result's `spoken`, word for word: it says when the screen goes off."
)
_END_DESCRIPTION: Final = (
    "End the night run now because the user is up: puts brightness and sound back and "
    "lets the Mac sleep normally again. Your reply is the result's `spoken`, word for word."
)


def _clock(value: object) -> time | None:
    """``"07:30"`` -> 07:30; None when absent. A malformed value is the model's error."""
    if value is None or value == "":
        return None
    match = _CLOCK.match(str(value).strip())
    if match is None:
        msg = "until must be a local time HH:MM"
        raise ToolError(msg, code="invalid_argument")
    return time(int(match.group(1)), int(match.group(2)))


def _hours(value: object) -> float | None:
    """The run's length, when given; out of range is the model's error."""
    if value is None or value == "":
        return None
    try:
        hours = float(str(value))
    except ValueError:
        hours = -1.0
    if not MIN_HOURS <= hours <= MAX_HOURS:
        msg = f"hours must be between {MIN_HOURS} and {MAX_HOURS:g}"
        raise ToolError(msg, code="invalid_argument")
    return hours


def build_night_tools(night: NightControl | None) -> tuple[Tool, ...]:
    """``start_night_run`` and ``end_night_run`` bound to the runtime's run; none when unwired."""
    if night is None:
        return ()

    def start(args: Mapping[str, Any], ctx: ToolContext) -> dict[str, Any]:
        return night.start(
            hours=_hours(args.get("hours")),
            until=_clock(args.get("until")),
            source="conversation",
            action_id=ctx.action_id,
        )

    def end(_args: Mapping[str, Any], ctx: ToolContext) -> dict[str, Any]:
        return night.end(action_id=ctx.action_id)

    callers = frozenset({CallerPrincipal.REGEX_ROUTER, CallerPrincipal.JARVIS_LLM})
    return (
        Tool(
            name="start_night_run",
            description=_START_DESCRIPTION,
            input_schema=_START_SCHEMA,
            handler=start,
            allowed_callers=callers,
            risk_level="L1",
            read_only=False,
        ),
        Tool(
            name="end_night_run",
            description=_END_DESCRIPTION,
            input_schema={"type": "object", "properties": {}, "required": []},
            handler=end,
            allowed_callers=callers,
            risk_level="L1",
            read_only=False,
        ),
    )


__all__ = ["MAX_HOURS", "MIN_HOURS", "NightControl", "build_night_tools"]
