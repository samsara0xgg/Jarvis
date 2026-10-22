"""ADR 0362: his mail is one tool that lists, searches, reads and archives, and remembers its rows.

A listing numbers its rows from 1 and is held, with every message it matched, until the next
listing or a restart: a row's number names that row in a later call and in a later turn, and a
message id always works. The tool composes three modules: ``mail_gmail`` (the server: ids, raw
messages, headers kept by id), ``mail_lists`` (his words as one Gmail query, exact counts, who
wrote last) and ``mail_reads`` (a mail, an attachment and a conversation read in parts). An
archive removes Gmail's inbox label, writes one ``mail.inbox_changed`` event and gives the id
that puts the mails back.

Layer rules: execution; ``jarvis.state`` for the event log.
"""

from __future__ import annotations

import json
import math
import re
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from jarvis.execution import mail_lists, mail_reads, speakable
from jarvis.execution.mail_gmail import Hit
from jarvis.execution.paragraphs import PART_CHARS
from jarvis.execution.tools import Receipted, Tool, ToolError, _get_running_event_uid
from jarvis.shared import CallerPrincipal
from jarvis.shared.receipts import Covered, Receipt
from jarvis.state.event_log import emit_event, iter_events_of_types

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from datetime import datetime

    from jarvis.execution.mail_gmail import Mailbox
    from jarvis.execution.mail_lists import Ask, Waiting
    from jarvis.execution.mail_reads import Thread
    from jarvis.execution.tools import ToolContext
    from jarvis.shared import Event, RiskLevel

_DESCRIPTION: Final = (
    "Use for any question about his mail: what came, what a mail says, what is waiting on his"
    " reply, a file attached to one, and taking mail out of his inbox. His job applications are"
    " jobs's and his schedule is schedule's. Returns numbered rows (sender, time, subject, the"
    " mail's own opening line, files, unread) or one mail's text, in parts when long; a row's"
    " number works until the next listing, a message id always."
)
_ACTIONS: Final = ("list", "search", "read", "thread", "attachment", "archive")
_ACTION_TEXT: Final = (
    "list (default): his unread inbox, or the mail he received narrowed by the fields below."
    " search: all his mail; a single match comes back read. read: a mail's text. thread: a"
    " conversation, oldest first. attachment: one file's text. archive: take mails out of his"
    " inbox, or put them back with undo."
)
_TAKEN: Final[Mapping[str, tuple[str, ...]]] = {
    "query": ("list", "search"),
    "when": ("list", "search"),
    "from": ("list", "search"),
    "to": ("list", "search"),
    "unread": ("list", "search"),
    "waiting": ("list",),
    "page": ("list", "search", "read", "thread", "attachment"),
    "looking_for": ("read", "attachment"),
    "message": ("read", "thread", "attachment", "archive"),
    "file": ("attachment",),
    "undo": ("archive",),
}
"""Each argument and the actions that take it. One an action does not take is an error."""
_ABOUT: Final[Mapping[str, str]] = {
    "query": "Words to find, or Gmail syntax such as has:attachment.",
    "when": (
        "When it arrived, in his words: today, yesterday, this week, since Monday, Oct 16."
        " Not Gmail dates."
    ),
    "from": "Who sent it, as he said it: a name or an address.",
    "to": "Who it went to, as he said it: a name or an address.",
    "unread": "True: only unread mail.",
    "waiting": (
        "True: only conversations where someone else wrote last and is waiting on him, over the"
        " last 14 days unless when is given."
    ),
    "page": (
        "2 and up: the next rows of a list, the next part of a long text, or the messages before"
        " the newest six of a conversation."
    ),
    "looking_for": (
        "What he wants from a long text, in his words: gives the opening and the paragraphs that"
        " match best instead of a part."
    ),
    "message": (
        "A row number from the last listing, or a message id. read takes up to 3 and archive up"
        " to 50, as 1,2,3 or 1-3."
    ),
    "file": "The attachment's number or name, as read listed it.",
    "undo": "The undo id an earlier archive gave: puts those mails back in his inbox.",
}
_TYPES: Final = {"unread": "boolean", "waiting": "boolean", "page": "integer"}
_FLAGS: Final = frozenset({"unread", "waiting"})


def _properties() -> dict[str, Any]:
    found: dict[str, Any] = {
        "action": {"type": "string", "enum": list(_ACTIONS), "description": _ACTION_TEXT},
    }
    for name, takers in _TAKEN.items():
        found[name] = {
            "type": _TYPES.get(name, "string"),
            "description": f"{', '.join(takers)}: {_ABOUT[name]}",
        }
    return found


_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object", "properties": _properties(), "additionalProperties": False,
}
_PARAMS: Final = ("action", *_TAKEN)
READ_MAX: Final = 3
"""Mails one ``read`` takes: three of them share the result a part would have."""
ARCHIVE_MAX: Final = 50
_INBOX: Final = "INBOX"
_SOURCES: Final = ("Gmail",)
_ROW: Final = re.compile(r"\d{1,4}")
_RANGE: Final = re.compile(r"(\d{1,4})-(\d{1,4})")
_SEPARATORS: Final = re.compile(r"[,\s]+")
_PART: Final = re.compile(r"part (\d+) of (\d+)")
_ROWS_BUDGET: Final = mail_reads.RESULT_CHARS - 600
"""What the rows of a list may take of its result; the summary and notes take the rest."""
_NAMED: Final = 3
"""How many of the mails an archive names in its summary."""
_UNDO_CHARS: Final = 8
"""How much of an archive's event id is its undo id: the start of it names the archive."""
_SUBJECT_CHARS: Final = 40
_SUPERSEDES: Final = {
    "mcp__gmail__gmail_search": 'mail(action="search", query=...)',
    "mcp__gmail__gmail_get": 'mail(action="read", message=<id>)',
    "mcp__gmail__gmail_downloadAttachment": 'mail(action="attachment", message=<id>, file=...)',
    "mcp__gmail__gmail_modify": 'mail(action="archive", message=<id>)',
    "mcp__gmail__gmail_batchModify": 'mail(action="archive", message=<id>)',
    "mcp__gmail__gmail_modifyThread": 'mail(action="archive", message=<id>)',
}
"""The plugin's Gmail tools whose job ``mail`` does. The draft and send tools are ADR 0366's."""


def _effect(args: Mapping[str, Any]) -> RiskLevel:
    """Reading is L0; an archive changes his inbox, with an undo, and needs no card (ADR 0361)."""
    return "L1" if args.get("action") == "archive" else "L0"


# --- the arguments -----------------------------------------------------------------------------


def _text(value: object, name: str) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, int | float) and not isinstance(value, bool):
        return str(value)
    msg = f"mail: {name} must be text"
    raise ToolError(msg, code="invalid_arguments")


def _message(value: object) -> str:
    """``message`` as one string; a list of rows or ids is joined as he would write them."""
    if isinstance(value, list | tuple):
        return ",".join(_text(one, "message") for one in value)
    return _text(value, "message")


def _flag(value: object, name: str) -> bool:
    if value is None or isinstance(value, bool):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    msg = f"mail: {name} must be true or false"
    raise ToolError(msg, code="invalid_arguments")


def _or(words: tuple[str, ...]) -> str:
    return f"{', '.join(words[:-1])} or {words[-1]}" if len(words) > 1 else words[0]


def _arguments(args: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """The action and the arguments given for it, each checked.

    A name the tool does not have, and an argument the action does not take, are errors that say
    so: ignoring one would answer a different question than the one he asked.
    """
    unknown = sorted(set(args) - set(_PARAMS))
    if unknown:
        msg = f"mail: no argument {', '.join(unknown)}; its arguments are {', '.join(_PARAMS)}"
        raise ToolError(msg, code="invalid_arguments")
    action = _text(args.get("action"), "action") or "list"
    if action not in _ACTIONS:
        msg = f"mail: action must be one of {', '.join(_ACTIONS)}, not {action!r}"
        raise ToolError(msg, code="invalid_arguments")
    given: dict[str, Any] = {}
    for name, takers in _TAKEN.items():
        raw = args.get(name)
        value: Any
        if name == "page":
            value = speakable.page_number(raw, "mail", "the next rows")
            empty = value == 1
        elif name in _FLAGS:
            value = _flag(raw, name)
            empty = not value
        else:
            value = _message(raw) if name == "message" else _text(raw, name)
            empty = not value
        if empty:
            continue
        if action not in takers:
            msg = f"mail: {name} is for {_or(takers)}, not {action}"
            raise ToolError(msg, code="invalid_arguments")
        given[name] = value
    return action, given


# --- what a result says ------------------------------------------------------------------------


def _size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False))


def _noun(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _reads(line: str, window: str, shown: int, more: int = 0, how: str = "") -> Receipt:
    """The receipt of a read: what it looked at, what it showed, what is left and how to get it."""
    return Receipt(
        "done", reads_only=True, line=line, covered=Covered(_SOURCES, window, shown, more, how),
    )


def _part_of(out: Mapping[str, Any]) -> tuple[int, int] | None:
    """Which part of how many a result is, from its ``part`` key; ``None`` when it is whole."""
    found = _PART.fullmatch(str(out.get("part", "")))
    return (int(found[1]), int(found[2])) if found else None


def _more_parts(got: tuple[int, int] | None) -> tuple[str, int, str]:
    """A part as the summary's words, the parts left after it, and how to get the next."""
    if got is None:
        return "", 0, ""
    part, parts = got
    left = parts - part
    how = f"page={part + 1}" if left else ""
    return f" Part {part} of {parts}" + (f"; {how} gives the next." if left else "."), left, how


def _fit(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The rows from the first that fit the result's budget: whole rows, at least one."""
    kept = rows[:1]
    for one in rows[1:]:
        if _size([*kept, one]) > _ROWS_BUDGET:
            break
        kept.append(one)
    return kept


def _waiting_summary(found: Waiting, ask: Ask, now: datetime, cap: int) -> str:
    """Who is waiting on him, and what the window held that is not."""
    lead = (
        f"{mail_lists.waiting_line(found, now)}." if found.hits
        else f"No thread is waiting on him ({ask.window})."
    )
    rest = found.threads - found.answered - found.skipped - found.unreadable - len(found.hits)
    tail = f"Of {_noun(found.threads, 'conversation')} with mail in {ask.window}:"
    ends = "ends" if found.answered == 1 else "end"
    unchecked = "older one not checked" if found.skipped == 1 else "older ones not checked"
    tail += f" {found.answered} {ends} with his own message"
    for count, what in (
        (rest, "bulk, automated or not addressed to him"),
        (found.skipped, unchecked),
        (found.unreadable, "could not be read"),
    ):
        if count > 0:
            tail += f", {count} {what}"
    tail += "."
    if found.more:
        tail += f" The window held more than {cap} received mails, so the oldest are missing."
    return f"{lead} {tail}"


def _archive_named(events: Sequence[Event], given: str) -> Event:
    """The archive an undo id names: its id is the start of the archive's event id."""
    said = given.strip().lower()
    found = [
        one for one in events
        if not one.payload.get("in_inbox") and one.event_uid.startswith(said)
    ] if len(said) >= _UNDO_CHARS else []
    if len(found) != 1:
        msg = f"mail: {given} does not name one archive; use the undo an archive gave"
        raise ToolError(msg, code="invalid_arguments")
    return found[0]


@dataclass(frozen=True)
class _Listing:
    """What the tool last showed: its rows' messages in row order, and what asked for them."""

    key: tuple[object, ...]
    hits: tuple[Hit, ...]
    more: bool = False
    """Gmail had more matches than the cap, so the last row is not the last match."""
    waiting: Waiting | None = None
    thread: Thread | None = None
    """For a conversation: its messages, so a later page of it needs no new search."""


def _opening(ask: Ask, listing: _Listing, now: datetime, cap: int) -> str:
    """The sentence a list opens with: how many, of what, newest first."""
    if listing.waiting is not None:
        return _waiting_summary(listing.waiting, ask, now, cap)
    total = len(listing.hits)
    count = f"More than {total}" if listing.more else str(total)
    if ask.query == mail_lists.INBOX_UNREAD:
        return f"{count} {ask.window}, newest first."
    what = "mails" if listing.more or total != 1 else "mail"
    return f"{count} {what}, {ask.window}, newest first."


def _page_notes(  # noqa: PLR0913 — what a page says besides its rows: which, left out, gone, not shown.
    kept: Sequence[Mapping[str, Any]], cut: Sequence[Mapping[str, Any]], gone: int,
    listing: _Listing, hidden: Sequence[Hit], names: Mapping[str, int], page: int, cap: int,
) -> list[str]:
    """The sentences after a list's opening: which rows these are, and what was not shown."""
    notes: list[str] = []
    if kept and (kept[0]["n"], kept[-1]["n"]) != (1, len(listing.hits)):
        notes.append(f"Showing {kept[0]['n']}-{kept[-1]['n']}.")
    if cut:
        numbers = ", ".join(f"message={one['n']}" for one in cut)
        notes.append(f"{_noun(len(cut), 'row')} did not fit; read them with {numbers}.")
    if gone:
        notes.append(f"{_noun(gone, 'mail')} on this page no longer exist in Gmail.")
    if hidden:
        where = ", ".join(f"{name} {count}" for name, count in names.items())
        count = f"more than {len(hidden)}" if listing.more else str(len(hidden))
        said = f"Not shown: {count}{f' ({where})' if where else ''};"
        said += f" page={page + 1} gives the next {mail_lists.PAGE_ROWS}."
        if listing.more:
            said += f" Past the first {cap} only a narrower when or from reaches the rest."
        notes.append(said)
    return notes


def _nothing(ask: Ask) -> Receipted:
    """An empty list says what was looked at and how to look differently."""
    if ask.query == mail_lists.INBOX_UNREAD:
        summary = "No unread mail in his inbox."
    else:
        summary = f"No mail matches {ask.window}. Try fewer words, a wider when or another sender."
    return Receipted(
        {"summary": summary, "rows": []},
        _reads(f"read mail, {ask.window}: none found", f"mail, {ask.window}", 0),
    )


# --- the tool ----------------------------------------------------------------------------------


class _Mail:
    """The tool's handler: its actions over one :class:`Mailbox` and the listing it last showed."""

    def __init__(self, box: Mailbox) -> None:
        self._box = box
        self._lock = threading.Lock()
        self._listing: _Listing | None = None

    def run(self, args: Mapping[str, Any], ctx: ToolContext) -> Receipted:
        """Check the arguments, then do the action; a failure of Gmail's is already worded."""
        action, given = _arguments(args)
        if action in {"list", "search"}:
            return self._list(given, ctx, searching=action == "search")
        if action == "archive" and "undo" in given:
            return self._put_back(given, ctx)
        if "message" not in given:
            msg = f"mail: {action} needs message=<a row of the last listing, or a message id>"
            raise ToolError(msg, code="invalid_arguments")
        handlers: Mapping[str, Callable[[Mapping[str, Any], ToolContext], Receipted]] = {
            "read": self._read, "thread": self._thread, "attachment": self._attachment,
            "archive": self._archive,
        }
        return handlers[action](given, ctx)

    # --- rows and ids --------------------------------------------------------------------------

    def _keep(self, listing: _Listing) -> None:
        with self._lock:
            self._listing = listing

    def _held(self) -> _Listing | None:
        with self._lock:
            return self._listing

    def _row(self, number: int) -> str:
        """The message id of row ``number`` of the last listing."""
        held = self._held()
        if held is None:
            msg = (
                "mail: a row number means a row of the last list or search, and nothing has been"
                " listed since mail was last restarted; list or search again, or give the"
                " message id"
            )
            raise ToolError(msg, code="no_rows", kind="retry_other_args")
        if not 1 <= number <= len(held.hits):
            msg = f"mail: the last listing has {len(held.hits)} rows; there is no row {number}"
            raise ToolError(msg, code="invalid_arguments")
        return held.hits[number - 1].id

    def _targets(self, wanted: str, *, limit: int, action: str) -> list[str]:
        """The message ids ``wanted`` names: row numbers, ranges of them (1-3) and message ids."""
        ids: list[str] = []
        for part in _SEPARATORS.split(wanted):
            if not part:
                continue
            if span := _RANGE.fullmatch(part):
                first, last = int(span[1]), int(span[2])
                if first > last or last - first >= limit:
                    msg = f"mail: {action} takes up to {_noun(limit, 'mail')}; {part} is more"
                    raise ToolError(msg, code="invalid_arguments")
                ids.extend(self._row(number) for number in range(first, last + 1))
            elif _ROW.fullmatch(part):
                ids.append(self._row(int(part)))
            else:
                ids.append(part)
        ids = list(dict.fromkeys(ids))
        if len(ids) > limit:
            msg = f"mail: {action} takes up to {_noun(limit, 'mail')} at a time, not {len(ids)}"
            raise ToolError(msg, code="invalid_arguments")
        if not ids:
            msg = f"mail: {action} needs message=<a row number or a message id>"
            raise ToolError(msg, code="invalid_arguments")
        return ids

    # --- list and search -----------------------------------------------------------------------

    def _find(self, ask: Ask, key: tuple[object, ...], now: datetime, *, fresh: bool) -> _Listing:
        """The listing for ``ask``: the held one for a later page, else a new search."""
        if not fresh and (held := self._held()) is not None and held.key == key:
            return held
        if ask.waiting:
            found = mail_lists.find_waiting(self._box, ask, now)
            listing = _Listing(key, found.hits, found.more, waiting=found)
        else:
            matches = self._box.search(ask.query)
            listing = _Listing(key, matches.hits, matches.more)
        self._keep(listing)
        return listing

    def _list(self, given: Mapping[str, Any], ctx: ToolContext, *, searching: bool) -> Receipted:
        """A page of his mail, eight rows: his unread inbox, a narrowed list, or a search."""
        now, zone = ctx.clock.now(), ctx.clock.zone
        if searching and not any(
            given.get(name) for name in ("query", "from", "to", "when", "unread")
        ):
            msg = (
                "mail: search needs query, from, to, when or unread; list with no arguments"
                " gives his unread inbox"
            )
            raise ToolError(msg, code="invalid_arguments")
        ask = mail_lists.ask_of(given, now, zone, searching=searching)
        page: int = given.get("page", 1)
        listing = self._find(ask, (searching, ask.query, ask.waiting), now, fresh=page == 1)
        total = len(listing.hits)
        if searching and total == 1 and page == 1 and not listing.more:
            lead = f"1 mail matches {ask.window}. "
            return self._read_one(listing.hits[0].id, {}, ctx, lead=lead)
        if total == 0 and listing.waiting is None:
            return _nothing(ask)
        if (page - 1) * mail_lists.PAGE_ROWS >= max(total, 1):
            end = max(1, math.ceil(total / mail_lists.PAGE_ROWS))
            msg = (
                f"mail: this list has {_noun(total, 'mail')}, ending at page {end};"
                f" page={page} is past it"
            )
            raise ToolError(msg, code="invalid_arguments")
        return self._page(ask, listing, page, ctx)

    def _page(self, ask: Ask, listing: _Listing, page: int, ctx: ToolContext) -> Receipted:
        """Rows ``(page - 1) * 8 + 1`` on, each read, with what is not shown counted."""
        now, zone = ctx.clock.now(), ctx.clock.zone
        start = (page - 1) * mail_lists.PAGE_ROWS
        lasts = listing.waiting.lasts if listing.waiting is not None else None
        rows, gone = mail_lists.page_rows(self._box, listing.hits, start, ask, now, lasts=lasts)
        hidden = listing.hits[start + mail_lists.PAGE_ROWS :]
        names = mail_lists.not_shown(self._box, ask.query, hidden)
        shown = [mail_lists.row_json(one, now, zone) for one in rows]
        kept = _fit(shown)
        cut = shown[len(kept) :]
        notes = _page_notes(kept, cut, gone, listing, hidden, names, page, self._box.cap)
        summary = " ".join([_opening(ask, listing, now, self._box.cap), *notes])
        what = "mail waiting on him" if ask.waiting else "mail"
        found = f"more than {len(listing.hits)}" if listing.more else str(len(listing.hits))
        return Receipted(
            {"summary": summary, "rows": kept},
            _reads(
                f"read {what}, {ask.window}: {found} found, {len(kept)} shown",
                f"{what}, {ask.window}", len(kept), len(hidden) + len(cut),
                f"page={page + 1}" if hidden else "",
            ),
        )

    # --- read ----------------------------------------------------------------------------------

    def _read(self, given: Mapping[str, Any], ctx: ToolContext) -> Receipted:
        """One mail's text in parts, or up to three mails, each with its share."""
        ids = self._targets(given["message"], limit=READ_MAX, action="read")
        if len(ids) == 1:
            return self._read_one(ids[0], given, ctx)
        now, zone = ctx.clock.now(), ctx.clock.zone
        mails: list[dict[str, Any]] = []
        summaries: list[str] = []
        for message, read in zip(ids, self._box.reads(ids), strict=True):
            if read is None:
                msg = f"No mail with id {message}; list or search first to get one"
                raise ToolError(msg, code="not_found")
            mails.append(mail_reads.mail_json(
                read, page=given.get("page", 1), looking_for=given.get("looking_for", ""),
                limit=PART_CHARS // len(ids), raw_max_bytes=self._box.raw_max_bytes,
                now=now, zone=zone,
            ))
            summaries.append(mail_reads.summary_of(read, now, zone))
        said = f"Read {len(ids)} mails: {' '.join(summaries)}"
        if any("part" in one or "showing" in one for one in mails):
            said += " Each is cut to its share; read one alone for the rest."
        return Receipted(
            {"summary": said, "mails": mails},
            _reads(f"read {len(ids)} mails", f"{len(ids)} mails by id", len(ids)),
        )

    def _read_one(
        self, message: str, given: Mapping[str, Any], ctx: ToolContext, *, lead: str = "",
    ) -> Receipted:
        """One mail read whole or in parts; ``lead`` opens the summary (a search's one match)."""
        now, zone = ctx.clock.now(), ctx.clock.zone
        read = self._box.read(message)
        out = mail_reads.mail_json(
            read, page=given.get("page", 1), looking_for=given.get("looking_for", ""),
            limit=PART_CHARS, raw_max_bytes=self._box.raw_max_bytes, now=now, zone=zone,
        )
        got = _part_of(out)
        said, left, how = _more_parts(got)
        who = read.mail.sender.shown or "an unknown sender"
        subject = speakable.cut(read.mail.subject, _SUBJECT_CHARS) or "(no subject)"
        line = f'read {who}\'s mail "{subject}"' + (f", part {got[0]} of {got[1]}" if got else "")
        return Receipted(
            {"summary": lead + mail_reads.summary_of(read, now, zone) + said, **out},
            _reads(line, f"one mail, {read.id}", 1, left, how),
        )

    # --- thread --------------------------------------------------------------------------------

    def _held_thread(self, message: str) -> Thread | None:
        held = self._held()
        if held is not None and held.thread is not None and any(
            one.id == message for one in held.hits
        ):
            return held.thread
        return None

    def _thread(self, given: Mapping[str, Any], ctx: ToolContext) -> Receipted:
        """A conversation's messages, the newest six, each with its own new text."""
        (message,) = self._targets(given["message"], limit=1, action="thread")
        now, zone = ctx.clock.now(), ctx.clock.zone
        page: int = given.get("page", 1)
        found = self._held_thread(message) if page > 1 else None
        if found is None:
            found = mail_reads.find_thread(self._box, self._box.read(message))
            self._keep(_Listing(
                ("thread", message), tuple(Hit(one.id, one.thread) for one in found.messages),
                found.more, thread=found,
            ))
        rows, earlier = mail_reads.thread_json(found, page=page, now=now, zone=zone)
        total = len(found.messages)
        subject = mail_reads.subject_of(found.messages[-1].mail.subject) or "(no subject)"
        titled = speakable.cut(subject, 80)
        said = f'Conversation "{titled}": {_noun(total, "message")}, oldest first.'
        if (rows[0]["n"], rows[-1]["n"]) != (1, total):
            said += f" Showing {rows[0]['n']}-{rows[-1]['n']}."
        how = f"page={page + 1}" if earlier else ""
        if earlier:
            said += f" {earlier} earlier: {how}."
        if not found.searched:
            said += " It has no subject, so only this message could be found."
        elif found.more:
            said += " Older messages may be missing: only the newest were looked for."
        named = speakable.cut(subject, _SUBJECT_CHARS)
        return Receipted(
            {"summary": said, "messages": rows},
            _reads(
                f'read the conversation "{named}": {len(rows)} of {total} messages',
                f'conversation "{named}"', len(rows), earlier, how,
            ),
        )

    # --- attachment ----------------------------------------------------------------------------

    def _attachment(self, given: Mapping[str, Any], ctx: ToolContext) -> Receipted:
        """One attachment of a mail as text, in parts as a mail is."""
        (message,) = self._targets(given["message"], limit=1, action="attachment")
        read = self._box.read(message)
        files = read.mail.attachments
        wanted = given.get("file") or ("1" if len(files) == 1 else "")
        if files and not wanted:
            listed = "; ".join(mail_reads.file_lines(files))
            msg = f"mail: attachment needs file=<number or name>; its attachments are {listed}"
            raise ToolError(msg, code="invalid_arguments")
        one = mail_reads.choose_file(files, wanted)
        out = mail_reads.attachment_json(
            read, one, page=given.get("page", 1), looking_for=given.get("looking_for", ""),
            limit=PART_CHARS, raw_max_bytes=self._box.raw_max_bytes,
        )
        of = mail_reads.summary_of(read, ctx.clock.now(), ctx.clock.zone).removeprefix("From ")
        said, left, how = _more_parts(_part_of(out))
        summary = f"{one.name} ({out['type']}, {out['size']}) from {of}{said}"
        return Receipted(
            {"summary": summary, **out},
            _reads(
                f"read {speakable.cut(one.name, 60)} from a mail", f"attachment {one.name}", 1,
                left, how,
            ),
        )

    # --- archive -------------------------------------------------------------------------------

    def _archive(self, given: Mapping[str, Any], ctx: ToolContext) -> Receipted:
        """Take mails out of his inbox: only those in it, so the undo restores exactly them."""
        ids = self._targets(given["message"], limit=ARCHIVE_MAX, action="archive")
        labels = self._box.labels(ids)
        gone = [one for one in ids if one not in labels]
        inbox = [one for one in ids if _INBOX in labels.get(one, frozenset())]
        out = [one for one in ids if one in labels and one not in inbox]
        if len(gone) == len(ids):
            msg = f"No mail with id {gone[0]}; list or search first to get one"
            raise ToolError(msg, code="not_found")
        notes = ""
        if out:
            notes += f" {_noun(len(out), 'mail')} already out of his inbox."
        if gone:
            notes += f" {_noun(len(gone), 'mail')} no longer exist in Gmail."
        if not inbox:
            return Receipted(
                {"summary": f"Nothing to archive:{notes}", "archived": []},
                Receipt("done", reads_only=False, line="nothing to archive: none was in his inbox"),
            )
        named = [
            f'{one.sender.shown or "(unknown sender)"}: "{speakable.cut(one.subject, 40)}"'
            for one in self._box.heads(inbox[:_NAMED]) if one is not None
        ]
        self._box.ask(
            "gmail_batchModify", {"messageIds": inbox, "removeLabelIds": [_INBOX]}, changing=True,
        )
        event = emit_event(
            ctx.conn, type="mail.inbox_changed",
            payload={"message_ids": inbox, "in_inbox": False, "action_id": ctx.action_id},
            source_event_id=_get_running_event_uid(ctx.conn, ctx.action_id),
            correlation={"action_id": ctx.action_id},
        )
        more = f" and {len(inbox) - _NAMED} more" if len(inbox) > _NAMED and named else ""
        count = _noun(len(inbox), "mail")
        undo = event.event_uid[:_UNDO_CHARS]
        summary = (
            f"Took {count} out of his inbox ({'; '.join(named)}{more}); they are in All Mail."
            f"{notes} Put them back with action=archive and undo={undo}."
        )
        return Receipted(
            {"summary": summary, "archived": inbox, "undo": undo},
            Receipt(
                "done", reads_only=False, line=f"took {count} out of his inbox",
                object=f"mail:{inbox[0]}", undo=event.event_uid,
            ),
        )

    def _put_back(self, given: Mapping[str, Any], ctx: ToolContext) -> Receipted:
        """An archive's undo: the same mails get the inbox label again, once."""
        if "message" in given:
            msg = "mail: give undo, or message, not both"
            raise ToolError(msg, code="invalid_arguments")
        events = list(iter_events_of_types(ctx.conn, ["mail.inbox_changed"]))
        event = _archive_named(events, str(given["undo"]))
        uid = event.event_uid
        ids = [str(one) for one in event.payload["message_ids"]]
        if any(one.payload.get("undoes") == uid for one in events):
            said = "Those mails were put back in his inbox already; nothing changed."
            return Receipted(
                {"summary": said, "restored": []},
                Receipt(
                    "done", reads_only=False, line="already back in his inbox: nothing changed",
                    object=f"mail:{ids[0]}",
                ),
            )
        self._box.ask(
            "gmail_batchModify", {"messageIds": ids, "addLabelIds": [_INBOX]}, changing=True,
        )
        emit_event(
            ctx.conn, type="mail.inbox_changed",
            payload={
                "message_ids": ids, "in_inbox": True, "action_id": ctx.action_id, "undoes": uid,
            },
            source_event_id=_get_running_event_uid(ctx.conn, ctx.action_id),
            correlation={"action_id": ctx.action_id},
        )
        count = _noun(len(ids), "mail")
        return Receipted(
            {"summary": f"Put {count} back in his inbox.", "restored": ids},
            Receipt(
                "done", reads_only=False, line=f"put {count} back in his inbox",
                object=f"mail:{ids[0]}",
            ),
        )


def build_mail_tool(box: Mailbox) -> tuple[Tool, ...]:
    """``mail`` over his Gmail: ``box`` is the one :class:`Mailbox` the runtime wired."""
    return (
        Tool(
            name="mail",
            description=_DESCRIPTION,
            input_schema=_SCHEMA,
            handler=_Mail(box).run,
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L1",
            read_only=False,
            effect=_effect,
            supersedes=_SUPERSEDES,
        ),
    )


__all__ = ["ARCHIVE_MAX", "READ_MAX", "build_mail_tool"]
