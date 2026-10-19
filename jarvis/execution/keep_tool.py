"""ADR 0245, 0299: a lasting fact about him is one write, ``keep``; reading them back is ``kept``.

``keep`` saves a fact as an item of core memory (ADR 0146) and says in one sentence what it did;
saving the same fact again writes nothing. ``kept`` reads the facts back. What he has to go to or
do (a memo, a reminder, an appointment) is the ledger's, and its tool is ``schedule``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, Final

from jarvis.execution.tools import Tool, ToolError
from jarvis.shared import CallerPrincipal
from jarvis.state.core_memory import SECTIONS
from jarvis.state.daily_report import resolve_zone
from jarvis.state.memory_db import current_core_memory, keep_fact

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from datetime import tzinfo
    from pathlib import Path

    from jarvis.execution.tools import ToolContext

_KEEP_DESCRIPTION: Final = (
    "Use when he states a lasting fact about himself or asks you to remember one: a birthday, a"
    " preference, a person, a habit. Not for something he has to do or go to (schedule), nor for"
    " what he is doing or where he is right now. Saves it to long-term memory; the same topic"
    " replaces its line. Returns a sentence saying whether it was new."
)
_KEEP_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "description": "The fact in his words, one short line.",
        },
        "topic": {
            "type": "string",
            "description": "A short label, e.g. birthday; keeping the same label replaces it.",
        },
        "section": {
            "type": "string",
            "enum": list(SECTIONS),
            "description": "The core-memory section it belongs in; omit when unsure.",
        },
    },
    "additionalProperties": False,
}
_KEPT_DESCRIPTION: Final = (
    "Use to read back the lasting facts about him that were kept, newest first, or those whose"
    " words match a query. Not for what he has to do or go to (schedule), mail or earlier"
    " conversations. Returns a sentence and the facts with when each was saved, 8 to a page."
)
_KEPT_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "Words a fact must contain."},
        "page": {"type": "integer", "description": "2 gives the next 8 after the first 8."},
    },
    "additionalProperties": False,
}
_RETIRED: Final = (
    "kind", "at", "until", "place", "in_minutes", "cancel", "confirm", "whole_sender",
)
"""What ``keep`` took before ADR 0299 moved memos, reminders and appointments to ``schedule``."""
_RESULT_CHARS: Final = 4000
_FACT_CHARS: Final = 240
_TOPIC_CHARS: Final = 40
_SHOWN_CHARS: Final = 120
_ROW_CHARS: Final = 240
_SAID_CHARS: Final = 60
_PAGE: Final = 8


def _now() -> datetime:
    return datetime.now(UTC)


def _cut(text: str, limit: int) -> str:
    """One line of at most ``limit`` characters."""
    line = " ".join(text.split())
    return line if len(line) <= limit else f"{line[: limit - 1]}…"


def _text(args: Mapping[str, Any], name: str) -> str:
    value = args.get(name)
    return value.strip() if isinstance(value, str) else ""


def _page(args: Mapping[str, Any]) -> int:
    """The page asked for: 1 when none is, else a whole number from 1."""
    value = args.get("page")
    if value is None:
        return 1
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        msg = "kept: page must be a whole number, e.g. page=2 for the next 8"
        raise ToolError(msg, code="invalid_arguments")
    return value


def _since(day: object, today: date) -> str:
    """``saved Oct 3`` for a core-memory item's ISO day; ``saved earlier`` when it has none."""
    try:
        at = date.fromisoformat(str(day)[:10])
    except ValueError:
        return "saved earlier"
    year = f" {at.year}" if at.year != today.year else ""
    return f"saved {at:%b} {at.day}{year}"


class _Keeper:
    """The handlers: ``keep`` writes core memory, ``kept`` reads it."""

    def __init__(
        self, memory_db: Path | None, zone: tzinfo, clock: Callable[[], datetime],
    ) -> None:
        self._memory_db = memory_db
        self._zone = zone
        self._clock = clock

    def keep(self, args: Mapping[str, Any], _ctx: ToolContext) -> dict[str, Any]:
        """Save the fact the arguments say; the result says whether it was new."""
        if unknown := sorted(set(args) - set(_KEEP_SCHEMA["properties"])):
            if unknown[0] in _RETIRED:
                msg = (
                    f"keep: {unknown[0]} is not a fact's argument; keep holds lasting facts about"
                    ' him only. For something he has to do or go to, call schedule(action="add",'
                    " what=..., at=...)"
                )
            else:
                msg = f"keep: no argument {unknown[0]!r}; use text, topic or section"
            raise ToolError(msg, code="invalid_arguments")
        text, topic, section = _text(args, "text"), _text(args, "topic"), _text(args, "section")
        if not text:
            msg = "keep: text is empty; pass the fact, e.g. keep(text='allergic to peanuts')"
            raise ToolError(msg, code="empty_text")
        if self._memory_db is None:
            msg = "keep: long-term memory is not available here, so nothing was saved; tell him"
            raise ToolError(msg, code="unavailable")
        if len(text) > _FACT_CHARS or len(topic) > _TOPIC_CHARS:
            msg = (
                f"keep: a fact is one short line (at most {_FACT_CHARS} characters, its topic"
                f" {_TOPIC_CHARS}); shorten it, or add a longer note with schedule"
            )
            raise ToolError(msg, code="too_long")
        if section and section not in SECTIONS:
            msg = f"keep: section {section!r} is not one of the section names; omit it if unsure"
            raise ToolError(msg, code="invalid_arguments")
        stored = f"{topic}: {text}" if topic else text
        try:
            kept = keep_fact(self._memory_db, text, topic or None, section or None)
        except ValueError as exc:
            msg = f"keep: {exc}"
            raise ToolError(msg, code="invalid_arguments") from exc
        if kept:
            summary = f'Saved to long-term memory: "{stored}".'
        else:
            summary = f'Already in long-term memory, nothing new: "{stored}".'
        return {"summary": summary, "text": stored}

    def kept(self, args: Mapping[str, Any], _ctx: ToolContext) -> dict[str, Any]:
        """One page of the facts kept, newest first."""
        query, page = _text(args, "query").casefold(), _page(args)
        found = [row for row in self._facts(self._clock()) if query in row["text"].casefold()]
        first = (page - 1) * _PAGE
        if page > 1 and first >= len(found):
            end = max(1, -(-len(found) // _PAGE))
            msg = f"There is no page {page}; what was kept ends at page {end}."
            return {"summary": msg, "items": [], "more": False}
        shown = found[first : first + _PAGE]
        return {
            "summary": self._summary(found, shown, query, first),
            "items": [{**row, "text": _cut(row["text"], _ROW_CHARS)} for row in shown],
            "more": first + _PAGE < len(found),
        }

    @staticmethod
    def _summary(
        found: list[dict[str, Any]], shown: list[dict[str, Any]], query: str, first: int,
    ) -> str:
        if not found:
            return f'No lasting facts kept match "{query}".' if query else "No lasting facts kept."
        head = f"{len(found)} lasting fact{'' if len(found) == 1 else 's'}" + (
            f' matching "{query}"' if query else ""
        )
        if len(found) > _PAGE:
            head += f" (showing {first + 1}-{first + len(shown)})"
        said = "; ".join(f'"{_cut(row["text"], _SAID_CHARS)}" ({row["when"]})' for row in shown)
        return f"{head}: {said}."

    def _facts(self, now: datetime) -> list[dict[str, Any]]:
        if self._memory_db is None:
            return []
        today = now.astimezone(self._zone).date()
        items = [
            item for section in current_core_memory(self._memory_db).doc.values()
            for item in section
        ]
        # Newest day first; within a day the item written last.
        newest = sorted(
            enumerate(items),
            key=lambda one: (str(one[1].get("since") or ""), one[0]),
            reverse=True,
        )
        return [
            {"text": str(item["text"]), "when": _since(item.get("since"), today)}
            for _, item in newest
        ]


def build_keep_tools(
    memory_db: Path | None,
    zone: tzinfo | None,
    clock: Callable[[], datetime] = _now,
) -> tuple[Tool, ...]:
    """``keep`` and ``kept`` over ``memory_db`` (None: no facts) and his zone."""
    keeper = _Keeper(memory_db, resolve_zone(None, zone)[1], clock)
    both = frozenset({CallerPrincipal.REGEX_ROUTER, CallerPrincipal.JARVIS_LLM})
    return (
        Tool(
            name="keep",
            description=_KEEP_DESCRIPTION,
            input_schema=_KEEP_SCHEMA,
            handler=keeper.keep,
            allowed_callers=both,
            risk_level="L1",
            read_only=False,
            max_result_chars=_RESULT_CHARS,
            supersedes={"remember": "keep(text=...)"},
        ),
        Tool(
            name="kept",
            description=_KEPT_DESCRIPTION,
            input_schema=_KEPT_SCHEMA,
            handler=keeper.kept,
            allowed_callers=both,
            risk_level="L0",
            read_only=True,
            max_result_chars=_RESULT_CHARS,
        ),
    )


__all__ = ["build_keep_tools"]
