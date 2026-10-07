"""ADR 0176: the conversation's way to turn the Dashboard's pages."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Final

from jarvis.execution.tools import Tool, ToolContext, ToolError
from jarvis.shared import CallerPrincipal

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

_DESCRIPTION: Final = (
    "Turn Allen's Dashboard to a page, and optionally to one item on it (a letter, a note, a "
    "session, a job row). Use it to move a Dashboard that is already open so it follows the "
    "conversation, and to open a closed one only when Allen asks to see something (show me, "
    "let me see, open it); a question is answered in words, not by opening it. item_id is an "
    "id from the Dashboard line of the state block (the open item "
    'or a numbered row; "the second one" means row 2); any other id opens the page alone. '
    "This only moves the screen: read what a page holds with the tool that owns it, and never "
    "claim the page shows something you have not read. "
    'Page "home" is its home screen; page "close" folds the Dashboard away, for when Allen '
    "asks to close it."
)

_DONE: Final = (
    "The Dashboard has turned. Do not call show_on_dashboard again for this request; its "
    "rows reach you in the Dashboard line of the next turn."
)
_UNKNOWN_ITEM: Final = (
    "That id is not on the Dashboard now, so only the page opened. Do not retry with other "
    "ids: its rows reach you in the Dashboard line of the next turn; until then ask Allen "
    "which one he means."
)


# Live 2026-10-07: asked "what did I get done yesterday", she opened the shut Dashboard on
# the brief page every time, whatever the description said. A shut one opens only when his
# words ask to see something.
_ASKS_TO_SEE: Final = re.compile(
    r"\b(show|open|see|display|pull up|bring up|put|look|dashboard|screen)\b"
    r"|打开|看看|看一下|给我看|显示|调出|放到|屏幕|面板",
    re.IGNORECASE,
)
_TURN_WORDS: Final = """
    SELECT json_extract(t.payload_json, '$.transcript') FROM events a
    JOIN events s ON s.type = 'turn.started'
        AND json_extract(s.payload_json, '$.turn_id') = json_extract(a.payload_json, '$.turn_id')
    JOIN events t ON t.event_uid = json_extract(s.payload_json, '$.trigger')
    WHERE a.type = 'action.proposed' AND json_extract(a.payload_json, '$.action_id') = ?
"""


# A few words alone are a command about the screen, however they were heard: "Show me." came
# through as "小how me" and "小米" (live 2026-10-07). Letters and digits only, any script.
_SHORT_COMMAND_CHARS: Final = 8


def _asks_to_see(ctx: ToolContext) -> bool:
    """Whether this turn's words ask to see something; True when they cannot be read."""
    row = None if ctx is None else ctx.conn.execute(_TURN_WORDS, (ctx.action_id,)).fetchone()
    words = row[0] if row else None
    if not isinstance(words, str):
        return True
    short = len(re.sub(r"[\W_]", "", words)) <= _SHORT_COMMAND_CHARS
    return short or bool(_ASKS_TO_SEE.search(words))


def build_dashboard_tool(
    pages: Sequence[str],
    present: Callable[..., Mapping[str, Any]] | None,
) -> tuple[Tool, ...]:
    """``show_on_dashboard`` bound to the runtime's presenter; none when the view is off.

    ``present(page, item_id, asked=...)`` raises ValueError for a page it does not know, a
    shut Dashboard nobody asked for, or when no Dashboard is connected, and answers what it
    sent otherwise.
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

    def show(args: Mapping[str, Any], ctx: ToolContext) -> dict[str, Any]:
        item_id = args.get("item_id")
        try:
            sent = present(
                str(args.get("page", "")),
                str(item_id) if item_id else None,
                asked=_asks_to_see(ctx),
            )
        except ValueError as exc:
            msg = f"show_on_dashboard: {exc}"
            raise ToolError(msg, code="invalid_argument") from exc
        shown: dict[str, Any] = {
            "shown": sent.get("page"),
            "item": sent.get("item_id"),
            "opened_page_only": sent.get("item_id") is None,
        }
        if item_id and sent.get("item_id") is None:
            shown["note"] = _UNKNOWN_ITEM
        else:
            shown["note"] = _DONE
        return shown

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
