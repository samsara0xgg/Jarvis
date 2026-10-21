"""The post-turn pass's question and the reading of its answer (ADR 0300). Pure: no I/O, no clock.

After each record of his is answered, one model call reads it in context and returns the actions
it makes on the ledger of his things: a new one, a move, done, paid, cancelled, confirmed.
:func:`build_prompt` is that question and :func:`parse_answer` the reading of the answer. The
answer is untrusted JSON and every check is code, not the model's word: a quote that is not in that
record's words or its picture notes drops the action; a time that does not read drops it; a new
thing already past when he said it is not new; a move, close or confirm must name an open thing
the model was shown. What does not pass is dropped, never repaired, and said in ``dropped``. The
caller owns the reads, the model call and the writes.

Layer rules: stdlib, ``jarvis.shared`` and ``jarvis.state``; no wiring.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, NamedTuple

from jarvis.state import things

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import tzinfo

    from jarvis.state.plan import Plan
    from jarvis.state.turn_things import Turn

ACTIONS: Final[tuple[str, ...]] = ("new", "move", "done", "paid", "cancelled", "confirmed")
MAX_OPEN: Final[int] = 100
"""How many open things the model is shown (:func:`shown`)."""
_MAX_ITEMS: Final[int] = 10
"""How many actions of one answer are read."""
_TITLE_CHARS: Final[int] = 120
_PLACE_CHARS: Final[int] = 120
_WORDS_CHARS: Final[int] = 4000
_ANSWER_CHARS: Final[int] = 1500
_BEFORE_CHARS: Final[int] = 500
_YEAR: Final[timedelta] = timedelta(days=366)
_DAY: Final[re.Pattern[str]] = re.compile(r"\d{4}-\d{2}-\d{2}")

INSTRUCTIONS: Final[str] = """\
You read one message that Allen sent to his assistant, with the messages before it, and answer \
what it changes in the list of his things. The messages may be in Chinese, English or both. \
Answer with one JSON object and nothing else: {"actions": [...]}; [] means none.

Only his own things count. Other people's things, jokes, hypotheticals, wishes, questions, and \
anything only the assistant said do not. A picture he sends shows what is so for him, whoever \
wrote the words in it; what anyone in it suggests he do does not count. The assistant's answer \
and the earlier messages are there to understand his words, not to be quoted. When in doubt, \
leave it out.

An action is one change to the list of his things:
{"action": "new" | "move" | "done" | "paid" | "cancelled" | "confirmed",
 "thing": the id of the open thing it is about; every action but "new" has one,
 "quote": the words that make it so, copied exactly from his message or from a note on a picture \
he sent,
 "sure": true if he said or showed it plainly, false if you infer it or he hedged,
 "kind": "go" if he goes to it, "do" if he does it ("new" only),
 "title": his words for it, short, in the language he used ("new" only),
 "place": where, only if he said or showed it ("new" only),
 "start_at", "end_at", "on_day", "due": when ("new" and "move"),
 "weekly_until": the last day of a thing that repeats every week ("new" only),
 "occurrence": one date of a thing that repeats every week, when the action is about that date \
only}
- new: a thing he will go to or do later, or must do, that the list does not hold already, however \
it is worded, and that the assistant's tools did not write in this turn. A thing he already did \
is not new.
- move: he gives an open thing another time.
- done: he did it, also when he tells or shows what he did and that finishes it. paid: he paid \
it. cancelled: it will not happen, or he drops it. confirmed: he says or shows that a thing \
marked unconfirmed is his.
- Every action but new must be something he said or showed plainly; if he did not, leave it out. \
Only a new thing may be unsure.
- A thing has one kind of time at most: start_at (with end_at if he gave one), or on_day (a date, \
no hour), or due (a date, or a time it must be done by), or none if he gave none. weekly_until \
goes with start_at or on_day.
- All times are local, YYYY-MM-DDTHH:MM:SS, worked out from the time the message was sent; dates \
are YYYY-MM-DD.
"""
EXTRACTOR: Final[str] = "turn-things-" + hashlib.sha256(INSTRUCTIONS.encode()).hexdigest()[:8]
"""Names the instructions and the checks of this module: a record's row says which one read it."""


class AnswerError(ValueError):
    """The answer is not the JSON object the question asks for."""


class _Drop(Exception):  # noqa: N818 - says why one action does not pass, not a fault
    """One action is dropped; the message is the reason."""


@dataclass(frozen=True)
class Written:
    """A write her tool made to the ledger in this turn: what it did, and the thing now."""

    what: str
    thing: things.Thing


@dataclass(frozen=True)
class Action:
    """One checked action. ``thing`` is the full id of the open thing; empty for a new one.

    ``source`` says where the quote was found, ``said`` or ``picture``; times are ISO with an
    offset (or a date), empty when there is none.
    """

    action: str
    quote: str
    sure: bool
    thing: str = ""
    source: str = "said"
    kind: str = ""
    title: str = ""
    place: str = ""
    start_at: str = ""
    end_at: str = ""
    on_day: str = ""
    due: str = ""
    weekly_until: str = ""
    occurrence: str = ""


@dataclass(frozen=True)
class Read:
    """What passed: the actions in the answer's order, and a reason for each drop."""

    actions: tuple[Action, ...]
    dropped: tuple[str, ...]


# --- the question --------------------------------------------------------------------------


def _local(at: datetime, zone: tzinfo) -> str:
    return at.astimezone(zone).isoformat(timespec="seconds")


def _cut(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _listed(one: things.Thing) -> dict[str, Any]:
    """An open thing as the model is shown it: its short id and what says what and when."""
    out: dict[str, Any] = {"id": things.short_id(one), "kind": one.kind, "title": one.title}
    for name in ("start_at", "end_at", "on_day", "due", "weekly_until", "place"):
        if value := getattr(one, name):
            out[name] = value
    if not one.confirmed:
        out["unconfirmed"] = True
    return out


def _gone(one: things.Thing, day: date, zone: tzinfo) -> bool:
    """Whether ``one`` is a thing to go to whose last time was before ``day``, open or not."""
    if one.kind != "go" or not (one.start_at or one.on_day):
        return False
    if one.weekly_until:
        return date.fromisoformat(one.weekly_until) < day
    if one.on_day:
        return date.fromisoformat(one.last_day or one.on_day) < day
    return datetime.fromisoformat(one.end_at or one.start_at).astimezone(zone).date() < day


def shown(open_things: Sequence[things.Thing], day: date, zone: tzinfo) -> list[things.Thing]:
    """The open things he could mean on ``day``, at most :data:`MAX_OPEN`, in the ledger's order.

    Nobody closes a thing to go to once its time is past, so those are left out; a thing to do
    stays however late it is. Over the limit, the things to go to furthest ahead give way first,
    so his to-dos and bills are always there.
    """
    current = [one for one in open_things if not _gone(one, day, zone)]
    outings = [one.thing_id for one in current if one.kind == "go" and (one.start_at or one.on_day)]
    room = max(MAX_OPEN - (len(current) - len(outings)), 0)
    cut = set(outings[room:])
    return [one for one in current if one.thing_id not in cut][:MAX_OPEN]


def _planned(plan: Plan | None) -> list[dict[str, Any]]:
    """Today's plan as the model is shown it: what, from when, and whether it is done."""
    if plan is None:
        return []
    return [
        {"start": item.start, "title": item.title, "done": item.done}
        for item in plan.items
    ]


def _wrote(one: Written) -> dict[str, Any]:
    return {"what": one.what, **_listed(one.thing)}


def build_prompt(  # noqa: PLR0913 - the record, its clock and what the model is shown with it
    turn: Turn, *, now: datetime, zone: tzinfo, zone_name: str, plan: Plan | None,
    open_things: Sequence[things.Thing], written: Sequence[Written],
) -> tuple[str, str]:
    """``(system, user)``: the instructions, then his record with everything the model reads it by.

    ``now`` is when the question is asked, ``zone_name`` his zone's name; ``open_things`` is what
    :func:`shown` chose.
    """
    sent = turn.at.astimezone(zone)
    user = [
        f"Now: {_local(now, zone)}, {now.astimezone(zone):%A}, {zone_name}.",
        f"His message was sent at {_local(sent, zone)}, {sent:%A}.",
        "Today's plan: " + json.dumps(_planned(plan), ensure_ascii=False),
        "Open things: "
        + json.dumps([_listed(one) for one in open_things], ensure_ascii=False),
        "What the assistant's tools wrote to the list in this turn: "
        + json.dumps([_wrote(one) for one in written], ensure_ascii=False),
        "",
        "The messages before his, oldest first:",
        *[
            f"{said.who} ({_local(said.at, zone)}): {_cut(said.text, _BEFORE_CHARS)}"
            for said in turn.before
        ],
        "",
        "Notes on the pictures he sent with his message:",
        *[f"{number}. {note}" for number, note in enumerate(turn.notes, start=1)],
        "",
        "His message:",
        _cut(turn.words, _WORDS_CHARS),
        "",
        "The assistant's answer to it:",
        _cut(turn.answer, _ANSWER_CHARS),
    ]
    return INSTRUCTIONS, "\n".join(user)


# --- the answer ----------------------------------------------------------------------------


def _object(text: str) -> dict[str, Any]:
    """The JSON object in the model's text (a code fence around it is fine)."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        msg = "the answer holds no JSON object"
        raise AnswerError(msg)
    try:
        found = json.loads(text[start : end + 1])
    except ValueError as exc:
        msg = "the answer is not JSON"
        raise AnswerError(msg) from exc
    if not isinstance(found, dict) or not isinstance(found.get("actions"), list):
        msg = "the answer is not an object with an actions list"
        raise AnswerError(msg)
    return found


def _flat(text: str) -> str:
    return " ".join(text.split()).casefold()


class _When(NamedTuple):
    """The time of an action: at most one of the first three, and the end of a start."""

    start_at: str = ""
    end_at: str = ""
    on_day: str = ""
    due: str = ""
    weekly_until: str = ""


@dataclass(frozen=True)
class _Reading:
    """What one answer is checked against."""

    turn: Turn
    zone: tzinfo
    known: Mapping[str, things.Thing]

    @property
    def said(self) -> datetime:
        return self.turn.at

    @property
    def today(self) -> date:
        return self.turn.at.astimezone(self.zone).date()


def _where(raw: Mapping[str, Any], reading: _Reading) -> str:
    """Where the quote is, ``said`` or ``picture``; ``_Drop`` when it is nowhere."""
    quote = raw.get("quote")
    if not isinstance(quote, str) or not (key := _flat(quote)):
        msg = "no quote"
        raise _Drop(msg)
    if key in _flat(reading.turn.words):
        return "said"
    if any(key in _flat(note) for note in reading.turn.notes):
        return "picture"
    msg = "the quote is not in his words or the notes"
    raise _Drop(msg)


def _text(value: object, limit: int, what: str) -> str:
    """A line of at most ``limit`` characters; empty for none."""
    if value is None or value == "":
        return ""
    if not isinstance(value, str):
        msg = f"{what} is not text"
        raise _Drop(msg)
    return " ".join(value.split())[:limit]


def _instant(value: object, reading: _Reading, what: str) -> datetime:
    """An ISO time, read in his zone when it has no offset."""
    if not isinstance(value, str):
        msg = f"{what} is not a time"
        raise _Drop(msg)
    try:
        at = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        msg = f"{what} does not read"
        raise _Drop(msg) from exc
    at = at.replace(tzinfo=reading.zone) if at.tzinfo is None else at
    if abs(at.astimezone(reading.zone).date() - reading.today) > _YEAR:
        msg = f"{what} is more than a year away"
        raise _Drop(msg)
    return at


def _date(value: object, reading: _Reading, what: str) -> date:
    if not isinstance(value, str) or _DAY.fullmatch(value.strip()) is None:
        msg = f"{what} is not a date"
        raise _Drop(msg)
    try:
        day = date.fromisoformat(value.strip())
    except ValueError as exc:
        msg = f"{what} is not a date"
        raise _Drop(msg) from exc
    if abs(day - reading.today) > _YEAR:
        msg = f"{what} is more than a year away"
        raise _Drop(msg)
    return day


def _given(raw: Mapping[str, Any], name: str) -> bool:
    return raw.get(name) not in (None, "")


def _times(raw: Mapping[str, Any], reading: _Reading, *, weekly: bool) -> _When:  # noqa: C901 - one rule per kind of time
    """The time an action gives, each part checked and none of it past when he said it."""
    out = dict.fromkeys(_When._fields, "")
    kinds = [name for name in ("start_at", "on_day", "due") if _given(raw, name)]
    if len(kinds) > 1:
        msg = "more than one kind of time"
        raise _Drop(msg)
    if _given(raw, "end_at") and kinds != ["start_at"]:
        msg = "end_at needs start_at"
        raise _Drop(msg)
    first: date | None = None
    if kinds == ["start_at"]:
        start = _instant(raw["start_at"], reading, "start_at")
        if start < reading.said:
            msg = "the time is already past"
            raise _Drop(msg)
        out["start_at"] = start.isoformat(timespec="seconds")
        first = start.astimezone(reading.zone).date()
        if _given(raw, "end_at"):
            end = _instant(raw["end_at"], reading, "end_at")
            if end <= start:
                msg = "end_at is not after start_at"
                raise _Drop(msg)
            out["end_at"] = end.isoformat(timespec="seconds")
    elif kinds == ["on_day"]:
        first = _date(raw["on_day"], reading, "on_day")
        if first < reading.today:
            msg = "the day is already past"
            raise _Drop(msg)
        out["on_day"] = first.isoformat()
    elif kinds == ["due"]:
        out["due"] = _due(raw["due"], reading)
    if _given(raw, "weekly_until"):
        if not weekly or first is None:
            msg = "weekly_until needs a new thing with start_at or on_day"
            raise _Drop(msg)
        until = _date(raw["weekly_until"], reading, "weekly_until")
        if until < first:
            msg = "weekly_until is before the first day"
            raise _Drop(msg)
        out["weekly_until"] = until.isoformat()
    return _When(**out)


def _due(value: object, reading: _Reading) -> str:
    """A due date or time that is not past when he said it."""
    if isinstance(value, str) and _DAY.fullmatch(value.strip()):
        day = _date(value, reading, "due")
        if day < reading.today:
            msg = "the due date is already past"
            raise _Drop(msg)
        return day.isoformat()
    at = _instant(value, reading, "due")
    if at < reading.said:
        msg = "the due time is already past"
        raise _Drop(msg)
    return at.isoformat(timespec="seconds")


def _key(start_at: str, on_day: str, due: str) -> tuple[str, str | int]:
    """A time as an instant or a day, so two spellings of one moment are the same.

    A start and a due at the same instant are one moment, and so are a day and a date-only due:
    whether it is to go to or be done by, it is the same thing at the same time.
    """
    if start_at:
        return "at", int(datetime.fromisoformat(start_at).timestamp())
    if on_day:
        return "day", on_day
    if due:
        return ("day", due) if _DAY.fullmatch(due) else (
            "at", int(datetime.fromisoformat(due).timestamp())
        )
    return "none", ""


def _stamp(value: str) -> int | None:
    return int(datetime.fromisoformat(value).timestamp()) if value else None


def _known(raw: Mapping[str, Any], reading: _Reading) -> things.Thing:
    """The open thing the action names; ``_Drop`` when it names none the model was shown."""
    wanted = raw.get("thing")
    one = reading.known.get(wanted.strip().lower()) if isinstance(wanted, str) else None
    if one is None:
        msg = "it names no open thing"
        raise _Drop(msg)
    return one


def _action(raw: object, reading: _Reading) -> Action:  # noqa: C901, PLR0912 - one branch per action
    """The action ``raw`` says, checked; ``_Drop`` says why it does not pass."""
    if not isinstance(raw, dict) or raw.get("action") not in ACTIONS:
        msg = "it is not an action"
        raise _Drop(msg)
    name = str(raw["action"])
    source = _where(raw, reading)
    sure = raw.get("sure")
    if not isinstance(sure, bool):
        msg = "sure is not true or false"
        raise _Drop(msg)
    quote = " ".join(str(raw["quote"]).split())
    if name == "new":
        kind = raw.get("kind")
        if kind not in things.KINDS:
            msg = "kind is not go or do"
            raise _Drop(msg)
        title = _text(raw.get("title"), _TITLE_CHARS, "title")
        if not title:
            msg = "no title"
            raise _Drop(msg)
        when = _times(raw, reading, weekly=True)
        return Action(
            name, quote, sure, source=source, kind=str(kind), title=title,
            place=_text(raw.get("place"), _PLACE_CHARS, "place"), start_at=when.start_at,
            end_at=when.end_at, on_day=when.on_day, due=when.due, weekly_until=when.weekly_until,
        )
    if not sure:
        msg = "he did not say or show it plainly"
        raise _Drop(msg)
    one = _known(raw, reading)
    occurrence = ""
    if _given(raw, "occurrence"):
        occurrence = _date(raw["occurrence"], reading, "occurrence").isoformat()
    if name == "confirmed":
        if one.confirmed:
            msg = "it is confirmed already"
            raise _Drop(msg)
        return Action(name, quote, sure, thing=one.thing_id, source=source)
    if name != "move":
        return Action(name, quote, sure, thing=one.thing_id, source=source, occurrence=occurrence)
    when = _times(raw, reading, weekly=False)
    if not (when.start_at or when.on_day or when.due):
        msg = "a move needs the new time"
        raise _Drop(msg)
    if occurrence and not when.start_at:
        msg = "one date of a weekly thing moves to a start_at"
        raise _Drop(msg)
    held = (_key(one.start_at, one.on_day, one.due), _stamp(one.end_at))
    if not occurrence and held == (_key(when.start_at, when.on_day, when.due), _stamp(when.end_at)):
        msg = "it is at that time already"
        raise _Drop(msg)
    return Action(
        name, quote, sure, thing=one.thing_id, source=source, occurrence=occurrence,
        start_at=when.start_at, end_at=when.end_at, on_day=when.on_day, due=when.due,
    )


def parse_answer(
    text: str, turn: Turn, *, zone: tzinfo, known: Mapping[str, things.Thing],
) -> Read:
    """What passes the checks in the model's answer, in its order.

    ``AnswerError`` when the answer is not the JSON object asked for. ``known`` is the open
    things the model was shown, by short id. Anything that does not pass is dropped and named in
    ``Read.dropped``; nothing is repaired.
    """
    found = _object(text)
    reading = _Reading(turn, zone, known)
    actions: list[Action] = []
    dropped: list[str] = []
    for raw in found["actions"][:_MAX_ITEMS]:
        try:
            actions.append(_action(raw, reading))
        except _Drop as why:
            dropped.append(f"action {_brief(raw)}: {why}")
    return Read(tuple(actions), tuple(dropped))


def _brief(raw: object) -> str:
    """A short name for a dropped item: its kind and its title or quote."""
    if not isinstance(raw, dict):
        return "?"
    said = raw.get("title") or raw.get("quote") or raw.get("thing") or ""
    return f"{raw.get('action') or '?'} {str(said)[:40]!r}"


def made_already(action: Action, written: Sequence[Written]) -> bool:
    """Whether her tool wrote this new, timed thing in the turn already, whatever it called it."""
    if action.action != "new":
        return False
    when = _key(action.start_at, action.on_day, action.due)
    return when[0] != "none" and any(
        one.what == "added" and _key(one.thing.start_at, one.thing.on_day, one.thing.due) == when
        for one in written
    )
