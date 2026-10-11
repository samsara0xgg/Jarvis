"""ADR 0235: ``mark_highlight``, his word on which items of a day mattered, as a model-only tool.

The day line is folded by the runtime, which holds the stores; the tool layer may not import it, so
the runtime hands in a :data:`HighlightMarker` that finds the item and keeps the mark. The model is
the only thing that hears "that lunch was the best part" or "the call didn't matter", which is why
this is a tool and not a button's route.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from jarvis.execution.tools import Tool, ToolError
from jarvis.shared import CallerPrincipal
from jarvis.state.day_highlights import LABEL_MAX_CHARS

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from jarvis.execution.tools import ToolContext

    type HighlightMarker = Callable[[str, str, bool, str | None], dict[str, Any]]
    """``(day, time, on, label)`` -> the item marked; raises ``ToolError`` for a refusal."""

DESCRIPTION: Final = (
    "Mark or unmark something on a day's timeline as one of that day's highlights, when he says it "
    "mattered or that it didn't. Name it by the day and a time it covers. His marks override your "
    "own picks."
)


def build_highlight_tool(mark: HighlightMarker | None) -> tuple[Tool, ...]:
    """``mark_highlight``; none without a runtime to find the item."""
    if mark is None:
        return ()

    def handle(args: Mapping[str, Any], _ctx: ToolContext) -> dict[str, Any]:
        day, at = args.get("day"), args.get("time")
        if not (isinstance(day, str) and isinstance(at, str)):
            msg = "day and time are required"
            raise ToolError(msg, code="invalid_argument")
        on, label = args.get("on", True), args.get("label")
        if not isinstance(on, bool) or not (label is None or isinstance(label, str)):
            msg = "on must be true or false, and label text"
            raise ToolError(msg, code="invalid_argument")
        label = (label or "").strip() or None
        if label is not None and len(label) > LABEL_MAX_CHARS:
            msg = f"label is over {LABEL_MAX_CHARS} characters"
            raise ToolError(msg, code="invalid_argument")
        return mark(day, at, on, label)

    return (
        Tool(
            name="mark_highlight",
            description=DESCRIPTION,
            input_schema={
                "type": "object",
                "properties": {
                    "day": {"type": "string", "description": 'YYYY-MM-DD, "today" or "yesterday"'},
                    "time": {"type": "string", "description": "HH:MM local, a time it covers"},
                    "on": {
                        "type": "boolean",
                        "description": "true marks it (the default), false unmarks it",
                    },
                    "label": {
                        "type": "string",
                        "description": (
                            f"a name for it in his language, at most {LABEL_MAX_CHARS} characters;"
                            " leave it out to keep the label it has"
                        ),
                    },
                },
                "required": ["day", "time"],
            },
            handler=handle,
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L1",
            read_only=False,
        ),
    )


__all__ = ["DESCRIPTION", "build_highlight_tool"]
