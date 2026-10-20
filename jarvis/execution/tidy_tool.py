"""ADR 0310: ``tidy_day``, his asking her to tidy today's timeline, as a model-only tool.

The writer of the doing lane is the runtime's, which holds the stores and the model; the tool
layer may not import it, so the runtime hands in a :data:`DayTidier` that writes today's newest
part at once and says what it did. The timeline's own button asks through its route.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from jarvis.execution.tools import Tool
from jarvis.shared import CallerPrincipal

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from jarvis.execution.tools import ToolContext

    type DayTidier = Callable[[], str]
    """Writes today's newest part now; the outcome, ``landed (...)`` when a run was stored."""

DESCRIPTION: Final = (
    "Tidy today's timeline of what Allen was doing, when he asks you to tidy or sort out his day: "
    "rewrites the part since it was last written, now. Takes up to a minute. Not for a past day."
)


def build_tidy_tool(tidy: DayTidier | None) -> tuple[Tool, ...]:
    """``tidy_day``; none without a writer of the doing lane."""
    if tidy is None:
        return ()

    def handle(_args: Mapping[str, Any], _ctx: ToolContext) -> dict[str, Any]:
        outcome = tidy()
        return {"done": outcome.startswith("landed"), "outcome": outcome}

    return (
        Tool(
            name="tidy_day",
            description=DESCRIPTION,
            input_schema={"type": "object", "properties": {}},
            handler=handle,
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L1",
            read_only=False,
        ),
    )


__all__ = ["DESCRIPTION", "build_tidy_tool"]
