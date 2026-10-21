"""ADR 0176: the conversation's way to turn the Dashboard's pages."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Final

from jarvis.execution.tools import Tool, ToolContext, ToolError
from jarvis.shared import CallerPrincipal

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

_DESCRIPTION: Final = (
    "Turn Allen's Dashboard to a page, or to one item on it, when he asks to see something "
    "(show me, open it) or to follow the conversation while it is open; a question is answered "
    "in words, not by opening a shut one. Pick the item with item=N, its number in the list on "
    'screen from 1 ("the second one" is item=2; the state block\'s Dashboard line lists them), '
    "or item_id, an exact id a tool returned. It only moves the screen: read what a page holds "
    "with the tool that owns it, and never claim the page shows something you have not read. "
    'Page "home" is the home screen, "close" folds the Dashboard away when he asks.'
)

_CATEGORIES: Final = (
    ' On page "settings", item_id may be a category: {ids}. "devices" is pairing a phone and '
    "shows its QR code."
)

_DONE: Final = (
    "The Dashboard has turned. Do not call show_on_dashboard again for this request; its "
    "rows reach you in the Dashboard line of the next turn."
)
_UNKNOWN_ITEM: Final = (
    "That id is not on the Dashboard now, so only the page opened. Do not guess other ids: "
    "use an id a tool returned, or ask Allen which one he means."
)
_UNKNOWN_POSITION: Final = (
    "The Dashboard was not on that page, so which item is number {n} there is not known and "
    "only the page opened. Do not retry: its list reaches you in the Dashboard line of the "
    "next turn; until then use an id a tool returned, or ask Allen which one he means."
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


def _position(value: object) -> int | None:
    """The ``item`` argument: None when absent, else a whole number from 1."""
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        msg = "show_on_dashboard: item must be a whole number from 1, e.g. item=2 for the second"
        raise ToolError(msg, code="invalid_arguments")
    return value


def _summary(page: str | None, item: str | None, *, asked: bool) -> str:
    """Where the Dashboard is now, as he would hear it."""
    if page is None:
        return "The Dashboard is shut."
    if page == "home":
        return "The Dashboard is on the home screen."
    if item is not None:
        return f"The Dashboard is on the {page} page, opened on the item."
    suffix = "; the item is not on it, so only the page opened" if asked else ""
    return f"The Dashboard is on the {page} page{suffix}."


def build_dashboard_tool(
    pages: Sequence[str],
    present: Callable[..., Mapping[str, Any]] | None,
    settings_categories: Sequence[str] = (),
) -> tuple[Tool, ...]:
    """``show_on_dashboard`` bound to the runtime's presenter; none when the view is off.

    ``present(page, item_id, asked=..., position=...)`` raises ValueError, worded for the model,
    for a page it does not know, a position past the list, a shut Dashboard nobody asked for,
    or when no Dashboard is connected, and answers what it sent otherwise.
    ``settings_categories`` are the Settings category ids ``item_id`` may name on page
    ``settings`` (ADR 0208).
    """
    if present is None:
        return ()
    schema = {
        "type": "object",
        "properties": {
            "page": {"type": "string", "enum": list(pages)},
            "item": {
                "type": "integer",
                "minimum": 1,
                "description": "The item's number in the list on screen, from 1; optional.",
            },
            "item_id": {
                "type": "string",
                "description": "An exact id a tool returned; optional, instead of item.",
            },
        },
        "required": ["page"],
        "additionalProperties": False,
    }

    def show(args: Mapping[str, Any], ctx: ToolContext) -> dict[str, Any]:
        item_id = args.get("item_id")
        position = _position(args.get("item"))
        try:
            sent = present(
                str(args.get("page", "")),
                str(item_id) if item_id else None,
                asked=_asks_to_see(ctx),
                position=position,
            )
        except ValueError as exc:
            msg = f"show_on_dashboard: {exc}"
            raise ToolError(msg, code="invalid_arguments") from exc
        page, item = sent.get("page"), sent.get("item_id")
        shown: dict[str, Any] = {
            "summary": _summary(page, item, asked=bool(item_id) or position is not None),
            "shown": page,
            "item": item,
            "opened_page_only": item is None,
        }
        if position is not None and item is None:
            shown["note"] = _UNKNOWN_POSITION.format(n=position)
        elif item_id and item is None:
            shown["note"] = _UNKNOWN_ITEM
        else:
            shown["note"] = _DONE
        return shown

    description = _DESCRIPTION
    if settings_categories:
        description += _CATEGORIES.format(ids=", ".join(settings_categories))
    return (
        Tool(
            name="show_on_dashboard",
            description=description,
            input_schema=schema,
            handler=show,
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L0",
            read_only=True,
        ),
    )


__all__ = ["build_dashboard_tool"]
