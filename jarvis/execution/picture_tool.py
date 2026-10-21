"""ADR 0223: ``look_at_picture`` re-reads a picture he sent earlier, through the vision preset.

The stored file is read while it is kept (``attachments.retention_days``). After that the tool says
the picture is gone and gives the note that was written for it, which is all there is.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from jarvis.execution.tools import Tool, ToolError
from jarvis.shared import CallerPrincipal
from jarvis.state import picture_notes
from jarvis.state.attachments import ATTACHMENTS_DIRNAME, LATEST, Attachments, short_id

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from jarvis.execution.tools import ToolContext, VisionClient

_DESCRIPTION: Final = (
    "Look at a picture the user sent earlier, again, and answer a question about it. `picture` "
    "is the id in its '[Picture note ... id XXXXXXXX]' line in the conversation, or 'latest' for "
    "the newest one. `question` is what to find out (a detail, a number, text in it). Pictures "
    "are kept about 30 days; after that this says the picture is gone and gives the note that "
    "was written for it. Use it when a note does not hold the detail asked for. It does not "
    "send a picture to the user."
)

_INSTRUCTIONS: Final = (
    "You answer one question about a picture a user sent to his assistant. Answer from the "
    "picture only, factually and briefly, in the language of the question. Say so when the "
    "picture does not show what is asked. Never guess who a person is and never name anyone "
    "from their face."
)

_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "properties": {
        "picture": {
            "type": "string",
            "description": "The picture's id (8 or more hex digits) from its note, or 'latest'.",
        },
        "question": {
            "type": "string",
            "description": "What to find out about the picture.",
        },
    },
    "required": ["picture", "question"],
}


def build_picture_tool(
    vision: VisionClient | None, memory_db: Path | None,
) -> tuple[Tool, ...]:
    """``look_at_picture`` over the vision seam; none without a vision preset."""
    if vision is None:
        return ()

    def look_at_picture(args: Mapping[str, Any], ctx: ToolContext) -> dict[str, Any]:
        wanted = str(args.get("picture", "")).strip()
        question = str(args.get("question", "")).strip()
        if not wanted or not question:
            msg = (
                'look_at_picture: picture (the id in its "[Picture note ... id XXXXXXXX]" line, '
                'or "latest") and question are both required'
            )
            raise ToolError(msg, code="invalid_arguments")
        store = Attachments(ctx.runtime_paths.artifacts_root / ATTACHMENTS_DIRNAME)
        ref = store.find(wanted)
        if ref is None or ref.kind != "image" or (path := store.path(ref.id)) is None:
            return _gone(memory_db, wanted)
        try:
            answer = vision.describe_image(path, question=question, instructions=_INSTRUCTIONS)
        except Exception as exc:  # degrades to an error observation; the file is untouched.
            msg = (
                f"look_at_picture: the picture could not be read just now "
                f"({type(exc).__name__}: {exc}); tell him it failed"
            )
            raise ToolError(msg, code="vision_call_failed") from exc
        return {"picture": short_id(ref.id), "answer": answer}

    return (
        Tool(
            name="look_at_picture",
            description=_DESCRIPTION,
            input_schema=_SCHEMA,
            handler=look_at_picture,
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L1",
            read_only=True,
        ),
    )


def _gone(memory_db: Path | None, wanted: str) -> dict[str, Any]:
    """The picture is not in the store: say so, with its note when the id names one."""
    result: dict[str, Any] = {
        "picture": wanted,
        "status": "gone",
        "message": (
            "That picture is no longer stored (pictures are kept about 30 days), or the id "
            "matches no picture. Say you can no longer look at it."
        ),
    }
    if memory_db is not None and wanted != LATEST:
        found = picture_notes.body_by_prefix(memory_db, wanted)
        if found:
            result["note"] = found
            result["message"] += " The note written for it is the only record left."
    return result


__all__ = ["build_picture_tool"]
