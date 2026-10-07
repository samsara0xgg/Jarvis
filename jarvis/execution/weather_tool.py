"""ADR 0188: the conversation's one-call read of the weather at home."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from jarvis.execution.tools import Tool, ToolError
from jarvis.shared import CallerPrincipal

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from jarvis.execution.tools import ToolContext

_DESCRIPTION: Final = (
    "Current, today's and tomorrow's weather at the user's home area (Victoria / Saanich),"
    " plus the next 12 hours: temperature, conditions, high/low and chance of rain. Use it"
    " for any question about the weather now, today, tonight or tomorrow, also when the user"
    " just says 'weather' or names Victoria or Saanich. For another city, a date beyond"
    " tomorrow, or anything the result does not hold, use web_search. If it returns an"
    " error, fall back to web_search."
)


def build_weather_tool(lookup: Callable[[], Mapping[str, Any]] | None) -> tuple[Tool, ...]:
    """``weather`` over ``lookup``; none when the home has no location."""
    if lookup is None:
        return ()

    def weather(_args: Mapping[str, Any], _ctx: ToolContext) -> dict[str, Any]:
        try:
            return dict(lookup())
        except Exception as exc:  # any failure is the model's cue to web_search.
            msg = f"weather unavailable ({type(exc).__name__}); use web_search"
            raise ToolError(msg, code="network_error") from exc

    return (
        Tool(
            name="weather",
            description=_DESCRIPTION,
            input_schema={"type": "object", "properties": {}, "required": []},
            handler=weather,
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L1",
            read_only=True,
        ),
    )


__all__ = ["build_weather_tool"]
