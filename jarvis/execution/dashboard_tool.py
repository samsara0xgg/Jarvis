"""ADR 0176: the conversation's way to turn the Dashboard's pages."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from jarvis.execution.tools import Tool, ToolContext, ToolError
from jarvis.shared import CallerPrincipal

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

_DESCRIPTION: Final = (
    "Turn Allen's Dashboard to a page, and optionally to one item on it (a letter, a note, a "
    "session, a job row). Use it to move a Dashboard that is already open so it follows the "
    "conversation, and to open a closed one only when Allen asks to see something; never open "
    "it uninvited. item_id is an id from the Dashboard line of the state block (the open item "
    'or a numbered row; "the second one" means row 2); any other id opens the page alone. '
    "This only moves the screen: read what a page holds with the tool that owns it, and never "
    "claim the page shows something you have not read."
)


def build_dashboard_tool(
    pages: Sequence[str],
    present: Callable[[str, str | None], Mapping[str, Any]] | None,
) -> tuple[Tool, ...]:
    """``show_on_dashboard`` bound to the runtime's presenter; none when the view is off.

    ``present(page, item_id)`` raises ValueError for a page it does not know or when no
    Dashboard is connected, and answers what it sent otherwise.
    """
    if present is None:
        return ()
    schema = {
        "type": "object",
        "properties": {
            "page": {"type": "string", "enum": list(pages)},
            "item_id": {
                "type": "string",
                "description": "An id from the Dashboard line of the state block; optional.",
            },
        },
        "required": ["page"],
        "additionalProperties": False,
    }

    def show(args: Mapping[str, Any], _ctx: ToolContext) -> dict[str, Any]:
        item_id = args.get("item_id")
        try:
            sent = present(str(args.get("page", "")), str(item_id) if item_id else None)
        except ValueError as exc:
            msg = f"show_on_dashboard: {exc}"
            raise ToolError(msg, code="invalid_argument") from exc
        return {
            "shown": sent.get("page"),
            "item": sent.get("item_id"),
            "opened_page_only": sent.get("item_id") is None,
        }

    return (
        Tool(
            name="show_on_dashboard",
            description=_DESCRIPTION,
            input_schema=schema,
            handler=show,
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L0",
            read_only=True,
        ),
    )


__all__ = ["build_dashboard_tool"]
