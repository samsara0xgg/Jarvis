"""ADR 0245: what he asks her to keep is one write, ``keep``; what she kept is one read, ``kept``.

``keep`` writes the three stores the old tools did, with their side effects unchanged: a memo is a
``memo.captured`` event, a reminder a ``reminder.scheduled`` event the daemon's tick fires
(ADR 0179), a fact an item of core memory (ADR 0146), an appointment a ``commitment.noted`` event
(ADR 0274). A write says in one sentence what it did and saving the same thing again writes
nothing.
"""

from __future__ import annotations

import dataclasses
import threading
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from jarvis.execution.tools import Tool, ToolError, _get_running_event_uid
from jarvis.shared import CallerPrincipal
from jarvis.state import commitments
from jarvis.state import reminders as reminder_state
from jarvis.state.core_memory import SECTIONS
from jarvis.state.daily_report import resolve_zone
from jarvis.state.event_log import emit_event, iter_events_of_types
from jarvis.state.memory_db import current_core_memory, keep_fact

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from datetime import tzinfo
    from pathlib import Path

    from jarvis.execution.tools import ToolContext

_KEEP_DESCRIPTION: Final = (
    "Use whenever he names something to be done later, by him or by you: a thing to fix, buy,"
    " check or do, a problem he found, an idea. That is a memo even when he only says it is for"
    " later and never says 记一下, 记得 or 别忘了. kind=memo (default): kept for later."
    " kind=reminder: he must be told at a time, with at (local time) or in_minutes ('in 10"
    " minutes'). kind=fact: a lasting fact about him. kind=appointment: something he must go"
    " to (at, until, place). It is saved only when this returns: never answer 记下了 or say it"
    " is noted without calling it. cancel=<id> cancels a reminder or appointment, confirm=<id>"
    " confirms one a mail made. To read back, use kept."
)
_KEEP_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "description": (
                "What to keep, in his words. It is read later: write 'tomorrow' or 'Friday' as"
                " the date it means."
            ),
        },
        "kind": {
            "type": "string",
            "enum": ["memo", "reminder", "fact", "appointment"],
            "description": (
                "memo (default): a note. reminder: needs at. fact: a standing fact he states or"
                " asks you to remember (birthday, preferences, people), never what he is doing,"
                " eating or where he is right now. appointment: needs at."
            ),
        },
        "at": {
            "type": "string",
            "description": (
                "reminder or appointment: when, local time worked out from the time in your"
                " state line, e.g. 2026-10-12 15:00."
            ),
        },
        "until": {
            "type": "string",
            "description": "appointment only: last day of a weekly one, 2026-10-21.",
        },
        "place": {"type": "string", "description": "appointment only: where, or a link."},
        "in_minutes": {
            "type": "integer",
            "description": (
                "reminder only, instead of at: for 'in 2 minutes' or 'in half an hour', the"
                " minutes from now (2, 30)."
            ),
        },
        "topic": {
            "type": "string",
            "description": "fact only: a short label, e.g. birthday; the same label replaces it.",
        },
        "section": {
            "type": "string",
            "enum": list(SECTIONS),
            "description": (
                "fact only: the core-memory section it belongs in (one of the eleven names);"
                " omit when unsure."
            ),
        },
        "cancel": {
            "type": "string",
            "description": "A reminder or appointment id from keep or kept, to cancel it, alone.",
        },
        "confirm": {
            "type": "string",
            "description": "An appointment id from kept that he says is his, alone.",
        },
        "whole_sender": {
            "type": "boolean",
            "description": "With confirm or cancel: that sender's later mail too.",
        },
    },
    "additionalProperties": False,
}
_KEPT_DESCRIPTION: Final = (
    "Use to read back what he had you keep: memos, waiting reminders, appointments (confirmed or"
    " not) and lasting facts about him; each reminder and appointment has the id keep(cancel=)"
    " needs. Not for mail, calendar or earlier"
    " conversations. kind: memo, reminder, appointment or fact (omit for all); query: words the"
    " text must"
    " hold; page=2 gives the next 8. Newest first, reminders soonest first. when is the ring"
    " time of a reminder, the time the rest was saved."
)
_KEPT_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["memo", "reminder", "fact", "appointment"]},
        "query": {"type": "string", "description": "Words the text must contain."},
        "page": {"type": "integer", "description": "2 gives the next 8 after the first 8."},
    },
    "additionalProperties": False,
}
_KINDS: Final = ("memo", "reminder", "fact", "appointment")
_RESULT_CHARS: Final = 4000
_MEMO_CHARS: Final = 2000
_REMINDER_CHARS: Final = 500
_FACT_CHARS: Final = 240
_TOPIC_CHARS: Final = 40
_SHOWN_CHARS: Final = 120
_ROW_CHARS: Final = 240
_SAID_CHARS: Final = 60
_PAGE: Final = 8
_NOON: Final = 12
_REPEAT_WINDOW: Final = timedelta(minutes=10)
"""How long a memo's text, saved again, is the same save."""
_PAST_GRACE: Final = timedelta(minutes=1)
_HORIZON: Final = timedelta(days=366)
_EMPTY: Final = {
    None: "Nothing kept yet.",
    "memo": "No memos kept.",
    "reminder": "No reminders waiting.",
    "appointment": "No appointments kept.",
    "fact": "No lasting facts kept.",
}
_COUNTED: Final = (
    ("reminder", "waiting reminder"), ("appointment", "appointment"), ("memo", "memo"),
    ("fact", "lasting fact"),
)
_SERIES_DAYS: Final = 366
"""How far ahead ``kept`` looks for the next time of an appointment."""


def _now() -> datetime:
    return datetime.now(UTC)


def _cut(text: str, limit: int) -> str:
    """One line of at most ``limit`` characters."""
    line = " ".join(text.split())
    return line if len(line) <= limit else f"{line[: limit - 1]}…"


def _same(one: str, other: str) -> bool:
    return " ".join(one.split()).casefold() == " ".join(other.split()).casefold()


def _when(at: datetime, now: datetime, zone: tzinfo) -> str:
    """``at`` as he would say it: ``tomorrow (Mon) 9:00 am``, else ``Mon Oct 12, 3:00 pm``."""
    local = at.astimezone(zone)
    today = now.astimezone(zone).date()
    hour = local.hour % _NOON or _NOON
    clock = f"{hour}:{local.minute:02d} {'am' if local.hour < _NOON else 'pm'}"
    day = {-1: "yesterday", 0: "today", 1: "tomorrow"}.get((local.date() - today).days)
    if day:
        return f"{day} ({local:%a}) {clock}"
    year = f" {local.year}" if local.year != today.year else ""
    return f"{local:%a %b} {local.day}{year}, {clock}"


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


class _Keeper:
    """The handlers: ``keep`` writes, ``kept`` reads, over the event log and memory.db."""

    def __init__(
        self, memory_db: Path | None, zone: tzinfo, clock: Callable[[], datetime],
    ) -> None:
        self._memory_db = memory_db
        self._zone = zone
        self._clock = clock
        self._lock = threading.Lock()

    def keep(self, args: Mapping[str, Any], ctx: ToolContext) -> dict[str, Any]:
        """Save, schedule or cancel what the arguments say; the result says which."""
        text, at, topic = _text(args, "text"), _text(args, "at"), _text(args, "topic")
        section, until, place = _text(args, "section"), _text(args, "until"), _text(args, "place")
        at = self._in_minutes(args.get("in_minutes"), at)
        if answered := self._about_one(args, ctx, text or at or topic or section or until or place):
            return answered
        kind = args.get("kind") or ("reminder" if at else "memo")
        if kind not in _KINDS:
            msg = f'keep: kind must be memo, reminder, appointment or fact, not "{kind}"'
            raise ToolError(msg, code="invalid_arguments")
        if not text:
            msg = "keep: text is empty; pass what to keep, e.g. keep(text='fix the emoji bug')"
            raise ToolError(msg, code="empty_text")
        only_with = (
            ("at", at, ("reminder", "appointment")), ("topic", topic, ("fact",)),
            ("section", section, ("fact",)), ("until", until, ("appointment",)),
            ("place", place, ("appointment",)),
        )
        for name, value, only in only_with:
            if value and kind not in only:
                msg = (
                    f"keep: {name} goes only with {' or '.join(f'kind={k}' for k in only)};"
                    " leave it out or change kind"
                )
                raise ToolError(msg, code="invalid_arguments")
        if kind == "appointment":
            return self._appointment(text, at, until, place, ctx)
        if kind == "reminder":
            return self._reminder(text, at, ctx)
        if kind == "fact":
            return self._fact(text, topic, section)
        return self._memo(text, ctx)

    def _about_one(
        self, args: Mapping[str, Any], ctx: ToolContext, other: str,
    ) -> dict[str, Any] | None:
        """``confirm=`` or ``cancel=`` (alone, with ``whole_sender`` at most); None for neither."""
        confirm, cancel = _text(args, "confirm"), _text(args, "cancel")
        whole = args.get("whole_sender") is True
        if whole and not (confirm or cancel):
            msg = "keep: whole_sender goes only with confirm=<id> or cancel=<id>"
            raise ToolError(msg, code="invalid_arguments")
        if not (confirm or cancel):
            return None
        if other or (confirm and cancel):
            msg = (
                f"keep: {'confirm' if confirm else 'cancel'}=<id> stands alone; to keep something"
                " new, call keep without it"
            )
            raise ToolError(msg, code="invalid_arguments")
        if confirm:
            return self._confirm(confirm, whole=whole, ctx=ctx)
        return self._cancel(cancel, whole=whole, ctx=ctx)

    def _in_minutes(self, minutes: object, at: str) -> str:
        """``at`` for a reminder ``minutes`` from now ("in 2 minutes"), else ``at`` as given."""
        if minutes is None:
            return at
        if at or not isinstance(minutes, int) or isinstance(minutes, bool) or minutes < 1:
            msg = "keep: in_minutes is a whole number of minutes from now (1 or more), not with at"
            raise ToolError(msg, code="invalid_time")
        return (self._clock() + timedelta(minutes=minutes)).astimezone(self._zone).isoformat()

    def _memo(self, text: str, ctx: ToolContext) -> dict[str, Any]:
        text = text[:_MEMO_CHARS]
        since = int((self._clock() - _REPEAT_WINDOW).timestamp() * 1000)
        with self._lock:
            known = iter_events_of_types(ctx.conn, ("memo.captured",), since_epoch_ms=since)
            saved = next((str(o.payload.get("text", "")) for o in known if _same(
                str(o.payload.get("text", "")), text,
            )), None)
            if saved is None:
                emit_event(
                    ctx.conn,
                    type="memo.captured",
                    payload={"text": text, "action_id": ctx.action_id},
                    source_event_id=_get_running_event_uid(ctx.conn, ctx.action_id),
                    correlation={"action_id": ctx.action_id},
                )
                summary = f'Saved to memos: "{_cut(text, _SHOWN_CHARS)}".'
            else:
                text = saved
                summary = f'Already saved to memos, nothing new: "{_cut(text, _SHOWN_CHARS)}".'
        return {"summary": summary, "kind": "memo", "text": text}

    def _due(
        self, raw: str, now: datetime, what: str = "a reminder", purpose: str = "to remind him",
    ) -> datetime:
        """``raw`` (a local ISO time, or one with its UTC offset) as an aware time to come."""
        tomorrow = now.astimezone(self._zone) + timedelta(days=1)
        example = f"{tomorrow:%Y-%m-%d} 09:00"
        if not raw:
            msg = f"keep: {what} needs at, the local time {purpose}, e.g. {example}"
            raise ToolError(msg, code="invalid_time")
        at: datetime | None
        try:
            at = datetime.fromisoformat(raw)
        except ValueError:
            at = None
        if at is None or len(raw) <= len("2026-10-12"):
            msg = (
                f'keep: at "{raw}" is not a local date and time like {example}; words such as'
                " tomorrow or 9am are not understood, so work the date out from the time in"
                " your state line"
            )
            raise ToolError(msg, code="invalid_time")
        at = at.replace(tzinfo=self._zone) if at.tzinfo is None else at.astimezone(self._zone)
        if at < now - _PAST_GRACE:
            msg = (
                f"keep: {_when(at, now, self._zone)} is in the past (it is"
                f" {_when(now, now, self._zone)} now); give a later local time, e.g. {example}"
            )
            raise ToolError(msg, code="time_in_past")
        if at > now + _HORIZON:
            msg = "keep: more than a year ahead; give a nearer local time, or keep it as a memo"
            raise ToolError(msg, code="too_far_ahead")
        return at

    def _reminder(self, text: str, raw: str, ctx: ToolContext) -> dict[str, Any]:
        now = self._clock()
        due = self._due(raw, now)
        text = text[:_REMINDER_CHARS]
        when, minute = _when(due, now, self._zone), int(due.timestamp() // 60)
        said = {"kind": "reminder", "when": when, "at": due.isoformat(timespec="seconds")}
        with self._lock:
            same = next(
                (
                    one for one in reminder_state.pending(ctx.conn)
                    if _same(one.text, text) and one.due_at_ms // 60_000 == minute
                ),
                None,
            )
            if same is not None:
                text = same.text
                said |= {"id": same.reminder_id}
                summary = f'Already set for {when}, nothing new: "{_cut(text, _SHOWN_CHARS)}".'
            else:
                said |= {"id": reminder_state.schedule(
                    ctx.conn,
                    due=due,
                    text=text,
                    action_id=ctx.action_id,
                    source_event_id=_get_running_event_uid(ctx.conn, ctx.action_id),
                )}
                summary = (
                    f'Reminder set for {when}: "{_cut(text, _SHOWN_CHARS)}". It rings then;'
                    " nothing else to do."
                )
        return {"summary": summary, **said, "text": text}

    def _appointment(
        self, text: str, raw: str, until: str, place: str, ctx: ToolContext,
    ) -> dict[str, Any]:
        """Keep something he says he has to go to: confirmed, being his own word (ADR 0274)."""
        now = self._clock()
        start = self._due(raw, now, "an appointment", "it starts")
        last = ""
        if until:
            try:
                ends = date.fromisoformat(until)
            except ValueError:
                msg = f'keep: until "{until}" is not a date like {start:%Y-%m-%d}'
                raise ToolError(msg, code="invalid_time") from None
            if ends < start.date() or ends > start.date() + _HORIZON:
                msg = "keep: until is the last day of a weekly series: at or later, within a year"
                raise ToolError(msg, code="invalid_time")
            last = ends.isoformat()
        title, when = _cut(text, _SHOWN_CHARS), _when(start, now, self._zone)
        said = {"kind": "appointment", "when": when, "at": start.isoformat(timespec="seconds")}
        with self._lock:
            same = next(
                (
                    one for one in commitments.live(ctx.conn)
                    if _same(one.title, title) and datetime.fromisoformat(one.start_at) == start
                ),
                None,
            )
            if same is not None:
                said |= {"id": same.commitment_id}
                summary = f'Already kept for {when}, nothing new: "{title}".'
            else:
                record = commitments.Commitment(
                    commitment_id=commitments.new_id(), title=title,
                    start_at=start.isoformat(timespec="seconds"), source="said", confirmed=True,
                    private=False, weekly_until=last,
                    place="" if place.startswith("http") else place[:_SHOWN_CHARS],
                    link=place if place.startswith("http") else "",
                )
                commitments.note(ctx.conn, record, actor="jarvis_llm")
                said |= {"id": record.commitment_id}
                weekly = f", every week until {last}" if last else ""
                summary = f'Appointment kept for {when}{weekly}: "{title}".'
        return {"summary": summary, **said, "text": title}

    def _confirm(self, commitment_id: str, *, whole: bool, ctx: ToolContext) -> dict[str, Any]:
        """He says an appointment a mail made is his; optionally every later mail of its sender."""
        with self._lock:
            one = self._appointment_by_id(commitment_id, ctx, whole=whole)
            what = f'the appointment "{_cut(one.title, _SHOWN_CHARS)}"'
            if one.confirmed:
                summary = f"Already confirmed, nothing changed: {what}."
            else:
                commitments.note(
                    ctx.conn, dataclasses.replace(one, confirmed=True), actor="jarvis_llm",
                )
                summary = f"Confirmed {what}."
            if whole:
                summary += self._sender_rule(one, "always", ctx)
        return {"summary": summary, "kind": "appointment", "id": commitment_id, "text": one.title}

    def _drop(self, commitment_id: str, *, whole: bool, ctx: ToolContext) -> dict[str, Any]:
        """He says an appointment is not his or is off; optionally all later mail of its sender."""
        with self._lock:
            one = self._appointment_by_id(commitment_id, ctx, whole=whole)
            what = f'the appointment "{_cut(one.title, _SHOWN_CHARS)}"'
            why = "not_his" if whole else "he_dropped"
            commitments.drop(ctx.conn, commitment_id, why, actor="jarvis_llm")
            summary = f"Dropped {what}."
            if whole:
                summary += self._sender_rule(one, "never", ctx)
        return {"summary": summary, "kind": "appointment", "id": commitment_id, "text": one.title}

    @staticmethod
    def _appointment_by_id(
        commitment_id: str, ctx: ToolContext, *, whole: bool,
    ) -> commitments.Commitment:
        """The live appointment; with ``whole``, a mail-made one, before anything is written."""
        one = commitments.fold(ctx.conn).get(commitment_id)
        if one is None or one.dropped is not None:
            msg = (
                f'keep: no appointment has the id "{commitment_id}" or it was dropped;'
                ' kept(kind="appointment") lists them with their ids'
            )
            raise ToolError(msg, code="not_found")
        if whole and not one.sender_domain:
            msg = "keep: that appointment did not come from a mail, so it has no sender to remember"
            raise ToolError(msg, code="invalid_arguments")
        return one

    @staticmethod
    def _sender_rule(one: commitments.Commitment, rule: str, ctx: ToolContext) -> str:
        if commitments.sender_rules(ctx.conn).get(one.sender_domain) != rule:
            commitments.set_sender_rule(ctx.conn, one.sender_domain, rule, actor="jarvis_llm")
        verb = "always" if rule == "always" else "never"
        return f" Later mail from {one.sender_domain} will {verb} count."

    def _fact(self, text: str, topic: str, section: str) -> dict[str, Any]:
        if self._memory_db is None:
            msg = "keep: long-term memory is not available here; kind=memo keeps it as a note"
            raise ToolError(msg, code="unavailable")
        if len(text) > _FACT_CHARS or len(topic) > _TOPIC_CHARS:
            msg = (
                f"keep: a fact is one short line (at most {_FACT_CHARS} characters, its topic"
                f" {_TOPIC_CHARS}); keep anything longer as a memo"
            )
            raise ToolError(msg, code="too_long")
        if section and section not in SECTIONS:
            msg = f"keep: section {section!r} is not one of the eleven names; omit it if unsure"
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
        return {"summary": summary, "kind": "fact", "text": stored}

    def _cancel(self, reminder_id: str, *, whole: bool, ctx: ToolContext) -> dict[str, Any]:
        if reminder_id.startswith(commitments.ID_PREFIX):
            return self._drop(reminder_id, whole=whole, ctx=ctx)
        if whole:
            msg = "keep: whole_sender goes only with an appointment's id"
            raise ToolError(msg, code="invalid_arguments")
        with self._lock:
            one = reminder_state.fold(ctx.conn).get(reminder_id)
            if one is None:
                msg = (
                    f'keep: no reminder has the id "{reminder_id}" (only reminders can be'
                    ' cancelled); kept(kind="reminder") lists the waiting ones with their ids'
                )
                raise ToolError(msg, code="not_found")
            due = datetime.fromtimestamp(one.due_at_ms / 1000, UTC)
            when = _when(due, self._clock(), self._zone)
            what = f'the reminder for {when}: "{_cut(one.text, _SHOWN_CHARS)}"'
            if one.cancelled:
                summary = f"Already cancelled, nothing changed: {what}."
            elif not one.pending:
                summary = f"It already rang, so there is nothing to cancel: {what}."
            else:
                reminder_state.cancel(
                    ctx.conn,
                    reminder_id,
                    action_id=ctx.action_id,
                    source_event_id=_get_running_event_uid(ctx.conn, ctx.action_id),
                )
                summary = f"Cancelled {what}."
        return {"summary": summary, "kind": "reminder", "id": reminder_id, "text": one.text}

    def kept(self, args: Mapping[str, Any], ctx: ToolContext) -> dict[str, Any]:
        """One page of what was kept, newest first (reminders soonest first)."""
        kind = args.get("kind") or None
        if kind is not None and kind not in _KINDS:
            msg = f'kept: kind must be memo, reminder or fact, not "{kind}"; omit it for all'
            raise ToolError(msg, code="invalid_arguments")
        query, page = _text(args, "query").casefold(), _page(args)
        now = self._clock()
        found = [
            row for row in (
                *(self._reminders(ctx, now) if kind in (None, "reminder") else ()),
                *(self._appointments(ctx, now) if kind in (None, "appointment") else ()),
                *(self._memos(ctx, now) if kind in (None, "memo") else ()),
                *(self._facts(now) if kind in (None, "fact") else ()),
            )
            if query in row["text"].casefold()
        ]
        first = (page - 1) * _PAGE
        if page > 1 and first >= len(found):
            end = max(1, -(-len(found) // _PAGE))
            msg = f"There is no page {page}; what was kept ends at page {end}."
            return {"summary": msg, "items": [], "more": False}
        shown = found[first : first + _PAGE]
        rows = [{**row, "text": _cut(row["text"], _ROW_CHARS)} for row in shown]
        return {
            "summary": self._summary(found, shown, kind, query, first),
            "items": rows,
            "more": first + _PAGE < len(found),
        }

    @staticmethod
    def _summary(
        found: list[dict[str, Any]], shown: list[dict[str, Any]], kind: str | None, query: str,
        first: int,
    ) -> str:
        if not found:
            return f'Nothing kept matches "{query}".' if query else _EMPTY[kind]
        counts = [
            f"{n} {noun}{'' if n == 1 else 's'}"
            for which, noun in _COUNTED
            if (n := sum(row["kind"] == which for row in found))
        ]
        head = ", ".join(counts) + (f' matching "{query}"' if query else "")
        if len(found) > _PAGE:
            head += f" (showing {first + 1}-{first + len(shown)})"
        # The summary is what a Tier 0 reply speaks: his facts are named only when he asks for them.
        said = "; ".join(
            f'"{_cut(row["text"], _SAID_CHARS)}" '
            f"({'rings ' if row['kind'] == 'reminder' else ''}{row['when']}"
            f"{', not confirmed' if row.get('confirmed') is False else ''})"
            for row in shown
            if kind == "fact" or row["kind"] != "fact"
        )
        return f"{head}: {said}." if said else f"{head}."

    def _reminders(self, ctx: ToolContext, now: datetime) -> list[dict[str, Any]]:
        return [
            {
                "kind": "reminder", "id": one.reminder_id, "text": one.text,
                "when": _when(datetime.fromtimestamp(one.due_at_ms / 1000, UTC), now, self._zone),
            }
            for one in reminder_state.pending(ctx.conn)
        ]

    def _appointments(self, ctx: ToolContext, now: datetime) -> list[dict[str, Any]]:
        """Each live appointment once, at its next time, soonest first."""
        first = int(now.timestamp() * 1000)
        ahead = commitments.occurrences(
            ctx.conn, first, first + _SERIES_DAYS * 86_400_000, zone=self._zone,
        )
        next_of = {occ.commitment.commitment_id: occ for occ in reversed(ahead)}
        rows = []
        for occ in sorted(next_of.values(), key=lambda o: o.start_ms):
            one = occ.commitment
            when = _when(datetime.fromtimestamp(occ.start_ms / 1000, UTC), now, self._zone)
            rows.append({
                "kind": "appointment", "id": one.commitment_id,
                "text": f"{one.title} ({one.place})" if one.place else one.title,
                "when": (
                    f"{when}, every week until {one.weekly_until}" if one.weekly_until else when
                ),
                "confirmed": one.confirmed,
            })
        return rows

    def _memos(self, ctx: ToolContext, now: datetime) -> list[dict[str, Any]]:
        saved = iter_events_of_types(ctx.conn, ("memo.captured",))
        return [
            {
                "kind": "memo", "text": str(one.payload.get("text", "")),
                "when": _when(datetime.fromtimestamp(one.ts_epoch_ms / 1000, UTC), now, self._zone),
            }
            for one in reversed(list(saved))
        ]

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
            {"kind": "fact", "text": str(item["text"]), "when": _since(item.get("since"), today)}
            for _, item in newest
        ]


def _since(day: object, today: date) -> str:
    """``saved Oct 3`` for a core-memory item's ISO day; ``saved earlier`` when it has none."""
    try:
        at = date.fromisoformat(str(day)[:10])
    except ValueError:
        return "saved earlier"
    year = f" {at.year}" if at.year != today.year else ""
    return f"saved {at:%b} {at.day}{year}"


def build_keep_tools(
    memory_db: Path | None,
    zone: tzinfo | None,
    clock: Callable[[], datetime] = _now,
) -> tuple[Tool, ...]:
    """``keep`` and ``kept`` over the event log, ``memory_db`` (None: no facts) and his zone."""
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
            # The six old names are gone; a search that names one (he still says them) is told
            # which call took its place.
            supersedes={
                "create_memo": "keep(text=...)",
                "set_reminder": 'keep(text=..., kind="reminder", at=... or in_minutes=...)',
                "cancel_reminder": "keep(cancel=<id>)",
                "remember": 'keep(text=..., kind="fact")',
            },
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
            supersedes={
                "list_memos": 'kept(kind="memo")', "list_reminders": 'kept(kind="reminder")',
            },
        ),
    )


__all__ = ["build_keep_tools"]
