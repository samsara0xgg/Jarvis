"""ADR 0147: the conversation's way to write a reply under the letter open on the Dashboard."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, Protocol

from jarvis.execution.tools import Tool, ToolError
from jarvis.shared import CallerPrincipal

if TYPE_CHECKING:
    from collections.abc import Mapping

    from jarvis.execution.tools import ToolContext


class DraftStore(Protocol):
    """The runtime's drafts (``jarvis.runtime.dashboard.MailDrafts``)."""

    def write(self, letter_id: str, text: str) -> int:
        """Replace the draft body under the open letter; the new revision. ValueError if refused."""


_DESCRIPTION: Final = (
    "Write or replace the draft reply under the open letter on Allen's Dashboard. Pass the "
    "whole reply text each time; the screen shows it as the draft. Nothing is sent."
)
_SCHEMA: Final = {
    "type": "object",
    "properties": {
        "letter_id": {"type": "string", "description": "The Gmail id of the open letter."},
        "text": {"type": "string", "description": "The whole reply, ready to send."},
    },
    "required": ["letter_id", "text"],
}


def build_mail_draft_tool(drafts: DraftStore | None) -> tuple[Tool, ...]:
    """``write_mail_draft`` bound to the runtime's drafts; none when the mail page is off."""
    if drafts is None:
        return ()

    def write(args: Mapping[str, Any], _ctx: ToolContext) -> dict[str, Any]:
        try:
            revision = drafts.write(str(args.get("letter_id", "")), str(args.get("text", "")))
        except ValueError as exc:
            msg = f"write_mail_draft: {exc}"
            raise ToolError(msg, code="invalid_argument") from exc
        return {"drafted": True, "revision": revision}

    return (
        Tool(
            name="write_mail_draft",
            description=_DESCRIPTION,
            input_schema=_SCHEMA,
            handler=write,
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L1",
            read_only=False,
        ),
    )


__all__ = ["DraftStore", "build_mail_draft_tool"]
