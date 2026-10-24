"""ADR 0390: ``topics``, her tool for the topics she keeps over the one conversation.

It lists them, reads one (its recap and the days it was talked about, each with a line to open
in ``conversation``), and changes one when he asks: rename, merge into another, mark done,
reopen, drop. The nightly pass keeps them otherwise; a name is his, so only he renames.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, cast

from jarvis.execution import speakable
from jarvis.execution.tools import Receipted, Tool, ToolError
from jarvis.shared import CallerPrincipal
from jarvis.shared.receipts import Covered, Receipt
from jarvis.state import topics
from jarvis.state.daily_contract import encoded

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import date, tzinfo
    from pathlib import Path

    from jarvis.execution.tools import ToolContext
    from jarvis.shared import RiskLevel

_DESCRIPTION: Final = (
    "Use for the topics you keep over your talk with him: which there are, what one holds and"
    " the days it was talked about, and when he asks to rename one, merge two, mark one done,"
    " reopen or drop one. Not for finding what was said: use conversation. Returns each topic's"
    " id, name and where it stands; read adds its recap and its days, each with a line id to"
    " open in conversation."
)
_ACTIONS: Final = ("list", "read", "rename", "merge", "done", "reopen", "drop")
_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": list(_ACTIONS),
            "description": (
                "list (default): the topics. read: one topic whole. rename, merge, done, reopen,"
                " drop: change one, only when he asks."
            ),
        },
        "topic": {
            "type": "string",
            "description": "Every action but list: the topic, by its id (t3) or its name.",
        },
        "name": {
            "type": "string",
            "description": f"rename: the new name, in his words, at most {topics.NAME_MAX} chars.",
        },
        "into": {
            "type": "string",
            "description": "merge: the topic it goes into, by id or name; that one is kept.",
        },
        "page": {
            "type": "integer",
            "description": "list: topics 13-24 are page 2. read: older days, 7-12 are page 2.",
        },
    },
    "additionalProperties": False,
}
_ARGS: Final = {
    "list": ("page",),
    "read": ("topic", "page"),
    "rename": ("topic", "name"),
    "merge": ("topic", "into"),
    "done": ("topic",),
    "reopen": ("topic",),
    "drop": ("topic",),
}
_LIST_PAGE: Final = 12
_DAYS_PAGE: Final = 6
_FIRST_LINE_CHARS: Final = 120
_BUDGET: Final = 4000
_SOURCES: Final = ("the topics she keeps",)
_NAMED: Final = 3


def _text(args: Mapping[str, Any], name: str) -> str:
    value = args.get(name)
    return " ".join(value.split()) if isinstance(value, str) else ""


def _action(args: Mapping[str, Any]) -> str:
    return _text(args, "action") or "list"


def _reads(args: Mapping[str, Any]) -> bool:
    """Whether a ``topics`` call only reads: list and read do, the rest write (ADR 0319)."""
    return _action(args) in ("list", "read")


def _effect(args: Mapping[str, Any]) -> RiskLevel:
    """The call's level (ADR 0361): a read L0, a change of her own index L1."""
    return "L0" if _reads(args) else "L1"


def _cut(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else f"{flat[: limit - 1].rstrip()}…"


def _state(topic: topics.Topic) -> str:
    return "open" if topic.state == "open" else "done"


class _Topics:
    """The tool's handler over the store at ``memory_db``, in the zone of the call's clock."""

    def __init__(self, memory_db: Path) -> None:
        self._db = memory_db

    def run(self, args: Mapping[str, Any], ctx: ToolContext) -> Receipted:
        """Check the arguments, then read or change; a store that cannot be read is worded."""
        action = _action(args)
        if action not in _ARGS:
            msg = f"topics: action is one of {', '.join(_ACTIONS)}, not {action!r}"
            raise ToolError(msg, code="invalid_arguments")
        given = {key for key in args if key != "action" and args[key] not in (None, "")}
        if extra := sorted(given - set(_ARGS[action])):
            msg = f"topics: {action} takes {', '.join(_ARGS[action]) or 'nothing'}, not {extra[0]}"
            raise ToolError(msg, code="invalid_arguments")
        page = speakable.page_number(args.get("page"), "topics", "the next ones")
        now = ctx.clock.now()
        zone = ctx.clock.zone
        try:
            found = topics.listed(self._db, zone)
            if action == "list":
                return self._list(found, page, now, zone)
            kept = topics.current(self._db)
            one = _named(_text(args, "topic"), found, kept, "topic", gone=action == "reopen")
            if action == "read":
                return self._read(next(t for t in found if t.topic.id == one.id), page, now, zone)
            return self._change(cast("topics.Op", action), one, args, found, kept)
        except sqlite3.Error as exc:
            msg = f"topics: the topics could not be read just now ({exc}); tell him so"
            raise ToolError(msg, code="unavailable") from exc

    # --- reads ------------------------------------------------------------------------------

    @staticmethod
    def _row(one: topics.Listed, today: date, zone: tzinfo) -> dict[str, Any]:
        last = datetime.fromtimestamp(one.last_ms / 1000, zone).date()
        return {
            "id": one.topic.id,
            "name": one.topic.name,
            "state": _state(one.topic),
            "where_it_stands": one.topic.line,
            "last_talked": speakable.day_name(last, today),
            "days": len(one.places),
        }

    def _list(
        self, found: Sequence[topics.Listed], page: int, now: datetime, zone: tzinfo,
    ) -> Receipted:
        today = now.astimezone(zone).date()
        start = (page - 1) * _LIST_PAGE
        shown = [self._row(one, today, zone) for one in found[start : start + _LIST_PAGE]]
        more = max(0, len(found) - start - len(shown))
        open_ones = sum(one.topic.state == "open" for one in found)
        if not found:
            summary = "You keep no topics yet; the nightly pass opens them from his talk."
        else:
            summary = (
                f"{len(found)} topics, {open_ones} open."
                + (f" {more} more on page {page + 1}." if more else "")
            )
        answer = {"summary": summary, "topics": shown}
        line = f"read the topics: {len(shown)} shown" + (f", {more} more" if more else "")
        return Receipted(answer, Receipt(
            "done", reads_only=True, line=line,
            covered=Covered(_SOURCES, "every topic", len(shown), more,
                            f"page={page + 1}" if more else ""),
        ))

    def _read(self, one: topics.Listed, page: int, now: datetime, zone: tzinfo) -> Receipted:
        today = now.astimezone(zone).date()
        newest = list(reversed(one.places))
        start = (page - 1) * _DAYS_PAGE
        days = [
            {
                "day": speakable.day_name(place.day, today),
                "lines": len(place.lines),
                "said_in_it": place.by == "him",
                "first_line": _cut(place.lines[0].text, _FIRST_LINE_CHARS),
                "open": place.lines[0].record_id,
            }
            for place in newest[start : start + _DAYS_PAGE]
        ]
        more = max(0, len(newest) - start - len(days))
        answer: dict[str, Any] = {
            "summary": (
                f"{one.topic.name} ({_state(one.topic)}), talked about on {len(one.places)}"
                f" days: {one.topic.line}"
            ),
            **self._row(one, today, zone),
            "recap": one.topic.recap,
            "talked_on": days,
            "next": 'conversation(open="<open>") shows the talk around a day\'s first line',
        }
        if more:
            answer["summary"] += f" {more} older days on page {page + 1}."
        while len(encoded(answer)) > _BUDGET and answer["talked_on"]:
            answer["talked_on"] = answer["talked_on"][:-1]
            more += 1
        shown = len(answer["talked_on"])
        line = f"read topic {one.topic.id} ({one.topic.name}): {shown} days shown"
        return Receipted(answer, Receipt(
            "done", reads_only=True, line=line + (f", {more} more" if more else ""),
            covered=Covered(_SOURCES, f"topic {one.topic.id}", shown, more,
                            f"page={page + 1}" if more else ""),
        ))

    # --- changes ----------------------------------------------------------------------------

    def _change(
        self, action: topics.Op, one: topics.Topic, args: Mapping[str, Any],
        found: Sequence[topics.Listed], kept: Mapping[str, topics.Topic],
    ) -> Receipted:
        name, into = _text(args, "name"), None
        if action == "rename":
            clash = [
                other for other in found
                if other.topic.id != one.id and other.topic.name.casefold() == name.casefold()
            ]
            if clash:
                msg = (
                    f"topics: {clash[0].topic.id} is already called {name!r}; to make them one,"
                    f" merge {one.id} into it"
                )
                raise ToolError(msg, code="invalid_arguments")
        if action == "merge":
            into = _named(_text(args, "into"), found, kept, "topic it goes into").id
        try:
            done = topics.change(self._db, one.id, action, name=name or None, into=into)
        except ValueError as exc:
            msg = f"topics: {exc}"
            raise ToolError(msg, code="invalid_arguments") from exc
        if done is None:
            answer = {"summary": f"{one.name} is already so; nothing changed."}
            return Receipted(answer, Receipt(
                "done", reads_only=False, object=f"topic:{one.id}",
                line=f"nothing changed: topic {one.id} is already so",
            ))
        said, undo = _said(action, done, kept)
        changed: dict[str, Any] = {"summary": said, "topic": one.id, "undo": undo}
        return Receipted(changed, Receipt(
            "done", reads_only=False, object=f"topic:{one.id}", line=said,
        ))


def _named(
    ref: str, found: Sequence[topics.Listed], kept: Mapping[str, topics.Topic], what: str, *,
    gone: bool = False,
) -> topics.Topic:
    """The topic ``ref`` names, by id or name, among the listed ones.

    A merged topic's id names the one it went into. With ``gone`` (a reopen) it is among those
    that are not open instead, and an id names that topic itself.
    """
    if not ref:
        msg = f"topics: give the {what} by its id (t3) or its name; topics() lists them"
        raise ToolError(msg, code="invalid_arguments")
    pool = (
        [one for one in kept.values() if one.state != "open"] if gone
        else [one.topic for one in found]
    )
    if topics.TOPIC_ID.fullmatch(ref):
        one = kept.get(ref) if gone else topics.resolve(kept, ref)
        if one is not None and one in pool:
            return one
    wanted = ref.casefold()
    same = [one for one in pool if one.name.casefold() == wanted] or [
        one for one in pool if wanted in one.name.casefold()
    ]
    if not same:
        where = "that is done, merged or dropped" if gone else "listed"
        msg = f"topics: no topic {where} is {ref!r}; topics() lists them"
        raise ToolError(msg, code="not_found")
    if len(same) > 1:
        named = "; ".join(f"{one.name} ({one.id})" for one in same[:_NAMED])
        msg = f"topics: {ref!r} could be {named}. Ask him which one, then give its id"
        raise ToolError(msg, code="ambiguous", kind="needs_him")
    return same[0]


def _said(
    action: topics.Op, done: topics.Change, kept: Mapping[str, topics.Topic],
) -> tuple[str, dict[str, str]]:
    """What the change did, in words, and the call that puts it back."""
    before, after = done.before, done.topic
    back = {"action": "reopen", "topic": after.id}
    if action == "rename":
        return (
            f"Renamed {before.name!r} to {after.name!r}.",
            {"action": "rename", "topic": after.id, "name": before.name},
        )
    if action == "merge":
        goal = kept.get(after.merged_into or "")
        return f"Merged {before.name} into {goal.name if goal else after.merged_into}.", back
    if action == "done":
        return f"Marked {after.name} done.", back
    if action == "drop":
        return f"Dropped {after.name}; its lines stay in the conversation.", back
    if before.state == "merged":
        return (
            f"Reopened {after.name} apart from the topic it was merged into.",
            {"action": "merge", "topic": after.id, "into": before.merged_into or ""},
        )
    undo = "drop" if before.state == "dropped" else "done"
    return f"Reopened {after.name}.", {"action": undo, "topic": after.id}


def build_topics_tool(memory_db: Path | None) -> tuple[Tool, ...]:
    """``topics`` over the store at ``memory_db``; none without the memory store."""
    if memory_db is None:
        return ()
    handler = _Topics(memory_db)

    def run(args: Mapping[str, Any], ctx: ToolContext) -> Receipted:
        return handler.run(args, ctx)

    return (
        Tool(
            name="topics",
            description=_DESCRIPTION,
            input_schema=_SCHEMA,
            handler=run,
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L1",
            read_only=False,
            reads=_reads,
            effect=_effect,
            max_result_chars=_BUDGET,
        ),
    )


__all__ = ["build_topics_tool"]
