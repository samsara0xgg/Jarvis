"""ADR 0362, 0364: a mail list asked in his words, counted exactly, read a page at a time.

What he asks (words, ``when``, ``from``, ``to``, ``unread``, ``waiting``) becomes one Gmail query.
Its matches are paged as ids, to a cap, so the count is exact or says "more than"; a page of them
is read in the server's text form for each row's own opening line; what is not shown is counted by
Gmail's categories. Each shown row says whether its conversation's last message is his, and when it
is not, whether its headers mark it bulk or automated and whether he is in its To. One rule, here,
says a thread is waiting on him (``Last.waiting``): the mail tool and the Mac home share it.

Layer rules: execution; stdlib, ``jarvis.execution.mail_gmail`` and ``jarvis.state.mail_text``.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Final

from jarvis.execution import speakable
from jarvis.state.mail_text import clean, is_forward

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence
    from datetime import tzinfo

    from jarvis.execution.mail_gmail import Found, Head, Hit, Mailbox
    from jarvis.state.mail_message import Person

PAGE_ROWS: Final = 8
"""Rows a page of a list shows: with every field bounded, eight stay within the result's size."""
INBOX_UNREAD: Final = "in:inbox is:unread"
"""The query of a list asked for with nothing to narrow it."""
WAITING_DAYS: Final = 14
"""How far back ``waiting`` looks when he gives no ``when``."""
WAITING_CHECKED: Final = 40
"""How many threads ``waiting`` reads the headers of, newest first; it says how many it left."""
_DAY_S: Final = 86400
_NAME_CHARS: Final = 40
_SUBJECT_CHARS: Final = 100
_OPENING_CHARS: Final = 140
_QUERY_CHARS: Final = 40
_FILES_SHOWN: Final = 3
_FILE_CHARS: Final = 40
_OPENING_LINES: Final = 3
_OPENING_ENOUGH: Final = 60
_CATEGORIES: Final = (
    ("primary", "Primary"), ("social", "Social"), ("promotions", "Promotions"),
    ("updates", "Updates"), ("forums", "Forums"),
)


# --- what he asked -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Ask:
    """A list asked for, in Gmail's terms and in the words that say its window."""

    query: str
    window: str
    unread_only: bool
    """The scope is unread mail, so a row need not say whether it is."""
    waiting: bool
    since: int | None = None
    """Epoch seconds the window opens at, when it has a start."""


def _text(args: Mapping[str, Any], name: str) -> str:
    value = args.get(name)
    return " ".join(value.split()) if isinstance(value, str) else ""


def _person(key: str, value: str) -> str:
    """A name or an address as a Gmail ``from:`` or ``to:`` term."""
    return f'{key}:"{value}"'


def _midnight(day: date, zone: tzinfo) -> int:
    return int(datetime.combine(day, time.min, tzinfo=zone).timestamp())


def ask_of(
    args: Mapping[str, Any], now: datetime, zone: tzinfo, *, searching: bool,
) -> Ask:
    """The query for ``args``: his words, resolved to Gmail's terms in his zone.

    A list is his received mail (not sent, not drafts); with nothing to narrow it, his unread
    inbox. A search is all his mail. ``when`` is read as a stretch back from today and becomes
    ``after:`` and ``before:`` at his local midnights.
    """
    words, when = _text(args, "query"), _text(args, "when")
    sender, to = _text(args, "from").replace('"', ""), _text(args, "to").replace('"', "")
    waiting, unread = args.get("waiting") is True, args.get("unread") is True
    today = now.astimezone(zone).date()
    terms: list[str] = []
    said: list[str] = []
    since: int | None = None
    if words:
        terms.append(words)
        said.append(f'"{speakable.cut(words, _QUERY_CHARS)}"')
    for key, who in (("from", sender), ("to", to)):
        if who:
            terms.append(_person(key, who))
            said.append(f"{key} {who}")
    if when or waiting:
        if when:
            first, last = speakable.resolve_when(when, today, back=True, tool="mail")
            said.append(speakable.span(first, last, today))
        else:
            first, last = today - timedelta(days=WAITING_DAYS - 1), today
            said.append(f"the last {WAITING_DAYS} days")
        since = _midnight(first, zone)
        terms += [f"after:{since}", f"before:{_midnight(last + timedelta(days=1), zone)}"]
    bare = not (words or sender or to or when or waiting)
    if bare and not searching:
        return Ask(INBOX_UNREAD, "unread in his inbox", unread_only=True, waiting=False)
    if not searching:
        terms += ["-in:sent", "-in:drafts"]
    if unread:
        terms.append("is:unread")
        said.append("unread")
    return Ask(" ".join(terms), ", ".join(said), unread_only=unread, waiting=waiting, since=since)


# --- who wrote last ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Last:
    """The last message of a conversation: his, or someone else's and what its headers say."""

    mine: bool
    at: datetime | None
    by: Person | None = None
    bulk: bool | None = None
    """Its headers say bulk; ``None`` when they could not be read."""
    automated: bool | None = None
    to_him: bool | None = None
    """He is in its To; ``None`` when his own addresses are not known."""

    @property
    def waiting(self) -> bool:
        """Someone else wrote last, neither bulk nor automated, to him: the thread waits on him."""
        return (
            not self.mine and self.bulk is False and self.automated is False
            and self.to_him is not False
        )


def _addressed(people: Collection[Person], own: Collection[str]) -> bool | None:
    return any(one.address.lower() in own for one in people) if own else None


def _answered(head: Head, own: Collection[str], sent: Head | None) -> datetime | None:
    """When his own message ends this conversation: the head is his, or a later sent one is."""
    if head.sender.address.lower() in own:
        return head.at
    if sent is not None and (head.at is None or (sent.at is not None and sent.at > head.at)):
        return sent.at
    return None


def last_writers(
    box: Mailbox, heads: Sequence[Head], newest: Mapping[str, str], now: datetime,
) -> dict[str, Last]:
    """Who wrote last in the conversation of each head that is its newest listed message.

    ``newest`` maps a thread id to the id of its newest message in the list. His sent mail is one
    ids-only search from the oldest of those messages on, then the heads of his newest sent
    message in each of their threads, compared by time; the headers of the others are read raw.
    """
    rows = [one for one in heads if newest.get(one.thread) == one.id]
    if not rows:
        return {}
    own = box.own(now)
    threads = {one.thread for one in rows}
    dated = [one.at for one in rows if one.at is not None]
    after = f" after:{int(min(dated).timestamp()) - _DAY_S}" if dated else ""
    first_sent: dict[str, str] = {}
    for hit in box.search(f"in:sent{after}").hits:
        if hit.thread in threads:
            first_sent.setdefault(hit.thread, hit.id)
    sent = {one.thread: one for one in box.heads(list(first_sent.values())) if one is not None}
    out: dict[str, Last] = {}
    pending: list[Head] = []
    for one in rows:
        mine_at = _answered(one, own, sent.get(one.thread))
        if mine_at is not None:
            out[one.thread] = Last(mine=True, at=mine_at)
        else:
            pending.append(one)
    envelopes = box.envelopes([one.id for one in pending])
    for one in pending:
        env = envelopes.get(one.id)
        out[one.thread] = Last(
            mine=False, at=one.at, by=one.sender,
            bulk=None if env is None else env.bulk,
            automated=None if env is None else env.automated,
            to_him=_addressed(one.to, own),
        )
    return out


def state_line(last: Last, now: datetime, zone: tzinfo) -> str:
    """What a row says of its conversation: his last word, or what marks it for him."""
    if last.mine:
        when = f" ({speakable.when(last.at, now, zone)})" if last.at else ""
        return f"you wrote last{when}"
    if last.waiting:
        return "waiting on him"
    marks = [
        word for word, found in (
            ("bulk mail", last.bulk), ("automated", last.automated),
            ("he is not in To", last.to_him is False),
        ) if found
    ]
    return ", ".join(marks)


# --- the rows ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    """One message of a list, as the model sees it."""

    n: int
    head: Head
    opening: str
    unread: bool | None
    last: Last | None


def opening(head: Head) -> str:
    """The message's own first words, from its cleaned text and never Gmail's snippet."""
    text = clean(head.body, forwarded=is_forward(head.subject)).text if head.body else ""
    taken: list[str] = []
    for line in (one.strip() for one in text.split("\n")):
        if not line:
            continue
        taken.append(line)
        if len(taken) >= _OPENING_LINES or len(" ".join(taken)) >= _OPENING_ENOUGH:
            break
    return speakable.cut(" ".join(taken), _OPENING_CHARS)


def _files(head: Head) -> list[str]:
    names = [speakable.cut(one.name, _FILE_CHARS) for one in head.files]
    shown = names[:_FILES_SHOWN]
    return [*shown, f"+{len(names) - _FILES_SHOWN} more"] if len(names) > _FILES_SHOWN else shown


def row_json(row: Row, now: datetime, zone: tzinfo) -> dict[str, Any]:
    """A row with the keys that have a value."""
    head = row.head
    out: dict[str, Any] = {
        "n": row.n,
        "message": head.id,
        "from": speakable.cut(head.sender.shown or "(unknown sender)", _NAME_CHARS),
    }
    if head.at is not None:
        out["when"] = speakable.when(head.at, now, zone)
    out["subject"] = speakable.cut(head.subject, _SUBJECT_CHARS) or "(no subject)"
    if row.opening:
        out["opening"] = row.opening
    if head.files:
        out["files"] = _files(head)
    if row.unread:
        out["unread"] = True
    if row.last is not None and (line := state_line(row.last, now, zone)):
        out["state"] = line
    return out


def newest_by_thread(hits: Sequence[Hit]) -> dict[str, str]:
    """Each thread's newest message in a list (the list is newest first)."""
    newest: dict[str, str] = {}
    for hit in hits:
        newest.setdefault(hit.thread, hit.id)
    return newest


def page_rows(  # noqa: PLR0913 — the list, the page, what was asked, the time and who wrote last.
    box: Mailbox, hits: Sequence[Hit], start: int, ask: Ask, now: datetime,
    *, lasts: Mapping[str, Last] | None = None,
) -> tuple[list[Row], int]:
    """The rows of ``hits[start:start + PAGE_ROWS]`` and how many of those Gmail no longer has.

    Each is read in the server's text form for its opening; ``n`` is its place in the whole list,
    so a number means the same on every page. ``lasts`` (by thread id) are the last writers when
    the list already knows them, as ``waiting`` does; else they are looked up here.
    """
    chosen = list(hits[start : start + PAGE_ROWS])
    heads = box.heads([one.id for one in chosen], full=True)
    kept = [(start + i + 1, one) for i, one in enumerate(heads) if one is not None]
    labels = {} if ask.unread_only else box.labels([one.id for _, one in kept])
    newest = newest_by_thread(hits)
    last = lasts if lasts is not None else last_writers(
        box, [one for _, one in kept], newest, now,
    )
    return [
        Row(
            n, one, opening(one),
            None if ask.unread_only else "UNREAD" in labels.get(one.id, frozenset()),
            last.get(one.thread) if newest.get(one.thread) == one.id else None,
        )
        for n, one in kept
    ], len(chosen) - len(kept)


# --- what is not shown -------------------------------------------------------------------------


def not_shown(box: Mailbox, query: str, hidden: Sequence[Hit]) -> dict[str, int]:
    """How many of ``hidden`` Gmail files under each category, by one ids-only search per category.

    The search is the list's own query narrowed to a category; a message is counted in the
    category whose matches hold it, and the rest are ``other``. Empty counts are left out.
    """
    if not hidden:
        return {}
    wanted = {one.id for one in hidden}

    def count(category: str) -> int:
        found = box.search(f"{query} category:{category}")
        return sum(one.id in wanted for one in found.hits)

    keys = [key for key, _ in _CATEGORIES]
    with ThreadPoolExecutor(len(keys), thread_name_prefix="mail-category") as pool:
        counts = dict(zip(keys, pool.map(count, keys), strict=True))
    counts["other"] = max(0, len(wanted) - sum(counts.values()))
    names = dict(_CATEGORIES) | {"other": "other"}
    return {names[key]: n for key, n in counts.items() if n}


# --- waiting on him ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Waiting:
    """The threads waiting on him: the newest message of each, and what could not be settled."""

    hits: tuple[Hit, ...]
    threads: int
    """Conversations with received mail in the window."""
    answered: int
    """Of those, the ones whose last message is his."""
    skipped: int
    """Conversations left unchecked, past ``WAITING_CHECKED`` (the oldest)."""
    unreadable: int
    """Conversations whose headers could not be read, so they are not claimed either way."""
    more: bool
    """The window held more received mail than the cap."""
    lasts: Mapping[str, Last]
    """The last writer of each waiting thread, by thread id."""


def _first_per_thread(found: Found) -> dict[str, Hit]:
    firsts: dict[str, Hit] = {}
    for hit in found.hits:
        firsts.setdefault(hit.thread, hit)
    return firsts


def find_waiting(box: Mailbox, ask: Ask, now: datetime) -> Waiting:
    """The threads in ``ask``'s window whose last message is someone else's, to him, not bulk.

    Received mail in the window is paged as ids; his sent mail from the window's start on is one
    more search. A thread with both is settled by the dates of its two newest messages. The rest
    are read raw for their headers, newest first, up to ``WAITING_CHECKED`` of them.
    """
    received = box.search(ask.query)
    newest = _first_per_thread(received)
    sent_query = "in:sent" + (f" after:{ask.since}" if ask.since is not None else "")
    sent = _first_per_thread(box.search(sent_query))
    both = [thread for thread in newest if thread in sent]
    dated = {
        one.id: one for one in box.heads(
            [newest[thread].id for thread in both] + [sent[thread].id for thread in both],
        ) if one is not None
    }
    answered: set[str] = set()
    for thread in both:
        mine, theirs = dated.get(sent[thread].id), dated.get(newest[thread].id)
        if mine is not None and mine.at is not None and (
            theirs is None or theirs.at is None or mine.at > theirs.at
        ):
            answered.add(thread)
    open_threads = [thread for thread in newest if thread not in answered]
    checked = open_threads[:WAITING_CHECKED]
    own = box.own(now)
    envelopes = box.envelopes([newest[thread].id for thread in checked])
    waiting: list[Hit] = []
    lasts: dict[str, Last] = {}
    unreadable = 0
    for thread in checked:
        env = envelopes.get(newest[thread].id)
        if env is None:
            unreadable += 1
            continue
        last = Last(
            mine=False, at=env.at, by=env.sender, bulk=env.bulk, automated=env.automated,
            to_him=_addressed(env.to, own),
        )
        if last.waiting:
            waiting.append(newest[thread])
            lasts[thread] = last
    return Waiting(
        tuple(waiting), len(newest), len(answered), len(open_threads) - len(checked), unreadable,
        received.more, lasts,
    )


def waiting_line(found: Waiting, now: datetime) -> str:
    """The summary's fact: who is waiting and for how long."""
    parts = []
    for last in list(found.lasts.values())[:3]:
        who = last.by.shown if last.by else "someone"
        parts.append(f"{who} ({speakable.age(last.at, now)})" if last.at else who)
    count = len(found.hits)
    noun = "thread" if count == 1 else "threads"
    return f"{count} {noun} where someone else wrote last and is waiting on him: {', '.join(parts)}"


__all__ = [
    "INBOX_UNREAD",
    "PAGE_ROWS",
    "WAITING_CHECKED",
    "WAITING_DAYS",
    "Ask",
    "Last",
    "Row",
    "Waiting",
    "ask_of",
    "find_waiting",
    "last_writers",
    "newest_by_thread",
    "not_shown",
    "opening",
    "page_rows",
    "row_json",
    "state_line",
    "waiting_line",
]
