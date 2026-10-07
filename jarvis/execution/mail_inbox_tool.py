"""ADR 0190: the conversation's way to read the unread Primary mail the Mail page lists."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from jarvis.execution.tools import Tool, ToolContext, ToolError
from jarvis.shared import CallerPrincipal

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

_DESCRIPTION: Final = (
    "List Allen's unread Primary inbox mail with the marks the Dashboard Mail page shows: per "
    'letter its Gmail id, sender name, subject, received time, reply ("yes" means the letter '
    'needs an answer, "fyi" does not, absent is unrated), and importance (0-3, higher first) '
    "and category when rated; junk letters are left out. Use it first for questions like what "
    "needs a reply or is anything important in my inbox, instead of searching and opening "
    "letters one by one. Read a letter's text with gmail_get only when he asks about that "
    "letter. The ids are the Gmail ids the Dashboard and gmail_get use. Read-only."
)
_KEYS: Final = ("id", "from", "subject", "received", "reply", "importance", "category")


def build_mail_inbox_tool(read: Callable[[], Mapping[str, Any]]) -> tuple[Tool, ...]:
    """``mail_inbox`` over ``Home.mail``; the call fails as a tool error without Gmail."""

    def mail_inbox(_args: Mapping[str, Any], _ctx: ToolContext) -> dict[str, Any]:
        try:
            letters = list(read().get("unread") or [])
        except LookupError as exc:  # the home's "not connected".
            msg = f"mail_inbox: Gmail is not connected ({exc})"
            raise ToolError(msg, code="unavailable") from exc
        except ToolError:
            raise
        except Exception as exc:  # any other failure is the model's cue to use gmail_search.
            msg = f"mail_inbox unavailable ({type(exc).__name__}); use gmail_search"
            raise ToolError(msg, code="network_error") from exc
        kept = [one for one in letters if not one.get("junk")]
        return {
            "unread": [{k: one[k] for k in _KEYS if one.get(k) is not None} for one in kept],
            "junk_left_out": len(letters) - len(kept),
        }

    return (
        Tool(
            name="mail_inbox",
            description=_DESCRIPTION,
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=mail_inbox,
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L0",
            read_only=True,
        ),
    )


__all__ = ["build_mail_inbox_tool"]
