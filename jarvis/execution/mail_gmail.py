"""ADR 0362 to 0364: the `mail` tool's Gmail layer, over the Workspace server's tools (ADR 0055).

A search gives ids and thread ids and pages on with a token. A read gives the message in the
server's own shape (``metadata``, ``full``) or as Gmail's own object (``minimal``; ``raw``, the
whole RFC 822 message, base64url). A draft is written, sent and moved to Trash through the three
write methods at the end of :class:`Mailbox`; nothing else here writes. Every failure is a
``ToolError`` worded for the model: Gmail not connected, Gmail needing a new sign-in and Gmail not
answering are three different sentences.

Layer rules: execution; stdlib and ``jarvis.state.mail_message``, ``jarvis.state.mail_text``.
"""

from __future__ import annotations

import base64
import binascii
import html
import re
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from jarvis.execution.speakable import cut
from jarvis.execution.tools import ToolError
from jarvis.state.mail_message import (
    Attachment,
    Envelope,
    Mail,
    Person,
    parse,
    parse_envelope,
    read_date,
    read_people,
)
from jarvis.state.mail_text import clean, is_forward

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

type Call = Callable[[str, Mapping[str, Any]], dict[str, Any]]
"""One call to the ``gmail`` server, answered as JSON: ``Home.gmail``."""

DEFAULT_LIST_CAP: Final = 500
"""How many matches a list pages through before it says "more than" (``tools.mail.list_cap``)."""
DEFAULT_RAW_MAX_BYTES: Final = 15 * 1024 * 1024
"""A message larger than this is read in the server's text form (``tools.mail.raw_max_bytes``)."""
_PAGE_IDS: Final = 100
"""Ids asked of one search page."""
_WORKERS: Final = 8
_OWN_SENT: Final = 20
"""How many of his newest sent messages tell which addresses are his."""
_OWN_KEPT: Final = timedelta(days=1)
_ENVELOPES_KEPT: Final = 2000
_DETAIL_CHARS: Final = 200
_NOT_FOUND: Final = re.compile(r"not found|404|invalid id", re.IGNORECASE)
_SIGN_IN: Final = re.compile(
    r"auth|log ?in|sign ?in|token|credential|expired|permission|\b40[13]\b", re.IGNORECASE,
)
_OWN_ERRORS: Final = frozenset({"invalid_arguments", "not_found"})
"""The codes of errors a call may already carry, worded for the model; any other is Gmail's."""


def failed(exc: Exception, *, changing: bool = False) -> ToolError:
    """Gmail's failure as the model should relay it; nothing to retry.

    ``changing``: the call was to change mail, so it says "changed" where a read says "read".
    """
    detail = cut(str(exc), _DETAIL_CHARS)
    done = "changed" if changing else "read"
    if isinstance(exc, LookupError):
        msg = "Gmail is not connected; tell him it needs connecting."
        return ToolError(msg, code="unavailable")
    if _SIGN_IN.search(detail):
        msg = f"Gmail needs reconnecting ({detail}); tell him, mail cannot be {done} until he does."
        return ToolError(msg, code="unavailable")
    msg = f"Gmail did not answer ({detail}); tell him mail could not be {done} just now."
    return ToolError(msg, code="network_error")


@dataclass(frozen=True)
class Hit:
    """One search result: a message and the conversation it belongs to."""

    id: str
    thread: str


@dataclass(frozen=True)
class Found:
    """A search's matches, newest first, and whether Gmail had more than the cap."""

    hits: tuple[Hit, ...]
    more: bool


@dataclass(frozen=True)
class Head:
    """One message in the server's own shape: headers and snippet, with ``full`` its text too."""

    id: str
    thread: str
    sender: Person
    to: tuple[Person, ...]
    cc: tuple[Person, ...]
    subject: str
    at: datetime | None
    snippet: str
    body: str = ""
    """What the server calls the body: the plain part when there is one, else the HTML."""
    files: tuple[Attachment, ...] = ()
    """The attachments' names, types and sizes, with no bytes."""


@dataclass(frozen=True)
class Draft:
    """A draft Gmail holds: its id, the id of its message and the conversation it is in."""

    id: str
    message: str
    thread: str


@dataclass(frozen=True)
class Read:
    """One message read whole: from its raw message when it could be, else from ``full``."""

    id: str
    thread: str
    labels: frozenset[str]
    mail: Mail
    files_read: bool
    """Whether the attachments' bytes were read (they are not when the raw message was not)."""
    why_not: str = ""
    """When they were not: ``size`` (over the limit) or ``no_raw`` (the server gave none)."""
    size: int = 0


def _names(value: object) -> tuple[Person, ...]:
    """The people in a header the server gave as one string or a list of them."""
    if isinstance(value, str):
        return read_people([value])
    if isinstance(value, list | tuple):
        return read_people([str(one) for one in value])
    return ()


def _files(value: object) -> tuple[Attachment, ...]:
    found: list[Attachment] = []
    for one in value if isinstance(value, list) else ():
        if isinstance(one, dict):
            name = str(one.get("filename") or one.get("name") or "")
            size = one.get("size")
            found.append(Attachment(
                name or "attachment", str(one.get("mimeType") or one.get("mime") or ""),
                size if isinstance(size, int) and not isinstance(size, bool) else 0,
            ))
        elif str(one):
            found.append(Attachment(str(one), "", 0))
    return tuple(found)


def _head(server: Mapping[str, Any]) -> Head:
    """The server's own shape (``metadata`` or ``full``) as a :class:`Head`."""
    senders = _names(server.get("from"))
    return Head(
        id=str(server["id"]),
        thread=str(server.get("threadId") or server["id"]),
        sender=senders[0] if senders else Person("", ""),
        to=_names(server.get("to")),
        cc=_names(server.get("cc")),
        subject=str(server.get("subject") or "").strip(),
        at=read_date(str(server.get("date") or "")),
        # Gmail's snippet is HTML-escaped ("don&#39;t").
        snippet=html.unescape(str(server.get("snippet") or "")),
        body=str(server.get("body") or ""),
        files=_files(server.get("attachments")),
    )


def _from_head(head: Head) -> Mail:
    """A message read in the server's text form: its text without the attachments' bytes."""
    return Mail(
        sender=head.sender, to=head.to, cc=head.cc, reply_to=(), subject=head.subject, at=head.at,
        message_id="", bulk=False, automated=False,
        body=clean(head.body, forwarded=is_forward(head.subject)),
        html="", plain=head.body, attachments=head.files,
    )


def _whole(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else (
        int(value) if isinstance(value, str) and value.isdigit() else 0
    )


def _decoded(value: object) -> bytes:
    """Gmail's ``raw`` (base64url, padding optional) as bytes; empty when it is not one."""
    if not isinstance(value, str) or not value:
        return b""
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError):
        return b""


class Mailbox:
    """His Gmail over one ``call``: searches, reads and what is worth keeping from them.

    It keeps, for the process's life: each message's headers by id (a message never changes
    them) and his own addresses for a day. ``cap`` is how far a search pages and ``raw_max_bytes``
    the size past which a message is not read raw.
    """

    def __init__(
        self,
        call: Call,
        *,
        cap: int = DEFAULT_LIST_CAP,
        raw_max_bytes: int = DEFAULT_RAW_MAX_BYTES,
    ) -> None:
        """Bind the server's call and the two bounds."""
        self._call = call
        self.cap = cap
        self.raw_max_bytes = raw_max_bytes
        self._lock = threading.Lock()
        self._envelopes: dict[str, Envelope | None] = {}
        self._own: tuple[str, ...] | None = None
        self._own_at = datetime.min.replace(tzinfo=UTC)

    # --- the server ----------------------------------------------------------------------------

    def ask(
        self, tool: str, args: Mapping[str, Any], *, message: str = "", changing: bool = False,
    ) -> dict[str, Any]:
        """One call; a failure is worded for the model, and ``message`` names an id Gmail lacks.

        ``changing`` marks a call that changes mail, so a failure says it could not be changed.
        """
        try:
            return self._call(tool, args)
        except (LookupError, ToolError) as exc:
            if isinstance(exc, ToolError) and exc.code in _OWN_ERRORS:
                raise
            if message and isinstance(exc, ToolError) and _NOT_FOUND.search(str(exc)):
                msg = f"No mail with id {message}; list or search first to get one"
                raise ToolError(msg, code="not_found") from exc
            raise failed(exc, changing=changing) from exc

    def search(self, query: str, *, cap: int | None = None) -> Found:
        """Every match of ``query``, newest first, paging ids only to ``cap`` (the default cap).

        ``more`` is true when Gmail had a match beyond the cap, so a count of the hits is exact
        exactly when it is false.
        """
        limit = self.cap if cap is None else cap
        hits: list[Hit] = []
        token = ""
        while len(hits) <= limit:
            ask: dict[str, Any] = {
                "query": query, "maxResults": min(_PAGE_IDS, limit + 1 - len(hits)),
            }
            if token:
                ask["pageToken"] = token
            page = self.ask("gmail_search", ask)
            found = page.get("messages") or []
            hits.extend(Hit(str(one["id"]), str(one.get("threadId") or one["id"])) for one in found)
            token = str(page.get("nextPageToken") or "")
            if not token or not found:
                break
        return Found(tuple(hits[:limit]), len(hits) > limit)

    # --- messages ------------------------------------------------------------------------------

    def head(self, message: str, *, full: bool = False) -> Head:
        """One message in the server's own shape: ``metadata``, or ``full`` with its text."""
        got = self.ask(
            "gmail_get", {"messageId": message, "format": "full" if full else "metadata"},
            message=message,
        )
        return _head(got)

    def heads(self, ids: Sequence[str], *, full: bool = False) -> list[Head | None]:
        """:meth:`head` of each id, in parallel; ``None`` for one Gmail no longer has."""
        if not ids:
            return []

        def one(message: str) -> Head | None:
            try:
                return self.head(message, full=full)
            except ToolError as exc:
                if exc.code == "not_found":
                    return None
                raise

        with ThreadPoolExecutor(min(len(ids), _WORKERS), thread_name_prefix="mail-head") as pool:
            return list(pool.map(one, ids))

    def read(self, message: str) -> Read:
        """One message read whole: its raw message, else (too big, or none given) its ``full``.

        Gmail's own object says the size first, so a message past ``raw_max_bytes`` is never
        downloaded raw.
        """
        small = self.ask("gmail_get", {"messageId": message, "format": "minimal"}, message=message)
        size = _whole(small.get("sizeEstimate"))
        labels = frozenset(str(one) for one in small.get("labelIds") or ())
        thread = str(small.get("threadId") or message)
        why = "size" if size > self.raw_max_bytes else ""
        if not why:
            raw = _decoded(
                self.ask("gmail_get", {"messageId": message, "format": "raw"}).get("raw"),
            )
            if raw:
                return Read(message, thread, labels, parse(raw), files_read=True, size=size)
            why = "no_raw"
        head = self.head(message, full=True)
        return Read(
            message, head.thread, labels, _from_head(head), files_read=False, why_not=why,
            size=size,
        )

    def reads(self, ids: Sequence[str]) -> list[Read | None]:
        """:meth:`read` of each id, in parallel; ``None`` for one Gmail no longer has."""
        if not ids:
            return []

        def one(message: str) -> Read | None:
            try:
                return self.read(message)
            except ToolError as exc:
                if exc.code == "not_found":
                    return None
                raise

        with ThreadPoolExecutor(min(len(ids), _WORKERS), thread_name_prefix="mail-read") as pool:
            return list(pool.map(one, ids))

    def labels(self, ids: Sequence[str]) -> dict[str, frozenset[str]]:
        """The labels each message holds now (``UNREAD``, ``INBOX``…), read in parallel."""
        if not ids:
            return {}

        def one(message: str) -> tuple[str, frozenset[str] | None]:
            try:
                small = self.ask(
                    "gmail_get", {"messageId": message, "format": "minimal"}, message=message,
                )
            except ToolError as exc:
                if exc.code == "not_found":
                    return message, None
                raise
            return message, frozenset(str(label) for label in small.get("labelIds") or ())

        with ThreadPoolExecutor(min(len(ids), _WORKERS), thread_name_prefix="mail-label") as pool:
            return {message: held for message, held in pool.map(one, ids) if held is not None}

    def envelope(self, message: str) -> Envelope | None:
        """One message's headers from its raw message (bulk and automated marks, its To).

        ``None`` when they cannot be read: the message is past ``raw_max_bytes`` or the server
        gave no raw message. Kept by id.
        """
        with self._lock:
            if message in self._envelopes:
                return self._envelopes[message]
        found: Envelope | None = None
        small = self.ask("gmail_get", {"messageId": message, "format": "minimal"}, message=message)
        if _whole(small.get("sizeEstimate")) <= self.raw_max_bytes:
            raw = _decoded(
                self.ask("gmail_get", {"messageId": message, "format": "raw"}).get("raw"),
            )
            found = parse_envelope(raw) if raw else None
        with self._lock:
            while len(self._envelopes) >= _ENVELOPES_KEPT:
                del self._envelopes[next(iter(self._envelopes))]
            self._envelopes[message] = found
        return found

    def envelopes(self, ids: Sequence[str]) -> dict[str, Envelope | None]:
        """:meth:`envelope` of each id, in parallel; an id Gmail no longer has is absent."""
        if not ids:
            return {}

        def one(message: str) -> tuple[str, Envelope | None, bool]:
            try:
                return message, self.envelope(message), True
            except ToolError as exc:
                if exc.code == "not_found":
                    return message, None, False
                raise

        with ThreadPoolExecutor(min(len(ids), _WORKERS), thread_name_prefix="mail-env") as pool:
            return {message: env for message, env, there in pool.map(one, ids) if there}

    def _mine(self, now: datetime) -> tuple[str, ...]:
        """His own addresses, the one he sends from most first, kept a day."""
        with self._lock:
            if self._own is not None and now - self._own_at < _OWN_KEPT:
                return self._own
        found = self.search("in:sent", cap=_OWN_SENT)
        sent = self.heads([one.id for one in found.hits])  # newest first
        used = Counter(one.sender.address.lower() for one in sent if one and one.sender.address)
        newest = {address: n for n, address in enumerate(used)}  # a tie goes to the newer
        mine = tuple(sorted(used, key=lambda address: (-used[address], newest[address])))
        with self._lock:
            self._own, self._own_at = mine, now
        return mine

    def own(self, now: datetime) -> frozenset[str]:
        """His own addresses: the distinct From addresses of his newest sent mail, kept a day."""
        return frozenset(self._mine(now))

    def me(self, now: datetime) -> str:
        """The address he sends from most; empty when he has sent nothing."""
        mine = self._mine(now)
        return mine[0] if mine else ""

    # --- writing -------------------------------------------------------------------------------

    def draft(  # noqa: PLR0913 — the parts of a letter Gmail takes.
        self, *, to: Sequence[str], cc: Sequence[str], bcc: Sequence[str], subject: str, body: str,
        thread: str = "",
    ) -> Draft:
        """Write a Gmail draft of a letter (``gmail_createDraft``); ``thread`` makes it a reply."""
        ask: dict[str, Any] = {"to": ", ".join(to), "subject": subject, "body": body}
        if cc:
            ask["cc"] = ", ".join(cc)
        if bcc:
            ask["bcc"] = ", ".join(bcc)
        if thread:
            ask["threadId"] = thread
        made = self.ask("gmail_createDraft", ask, changing=True)
        message = made.get("message")
        inner = message if isinstance(message, dict) else {}
        draft, message_id = str(made.get("id") or ""), str(inner.get("id") or "")
        if not draft or not message_id:
            msg = "Gmail did not say which draft it wrote; tell him the letter may not be saved"
            raise ToolError(msg, code="network_error")
        return Draft(draft, message_id, str(inner.get("threadId") or thread or message_id))

    def send_draft(self, draft: str) -> str:
        """Send a draft (``gmail_sendDraft``); the id of the message Gmail says it sent."""
        sent = self.ask("gmail_sendDraft", {"draftId": draft}, changing=True)
        labels = sent.get("labelIds")
        said = str(sent.get("id") or "")
        if not said or (isinstance(labels, list) and "SENT" not in labels):
            msg = "Gmail did not say the letter was sent; tell him it may not have gone"
            raise ToolError(msg, code="network_error")
        return said

    def trash(self, message: str) -> bool:
        """Move a message to Trash (``gmail_modify``); False when the server will not."""
        try:
            self.ask(
                "gmail_modify", {"messageId": message, "addLabelIds": ["TRASH"]}, changing=True,
            )
        except ToolError:
            return False
        return True


__all__ = [
    "DEFAULT_LIST_CAP",
    "DEFAULT_RAW_MAX_BYTES",
    "Call",
    "Draft",
    "Found",
    "Head",
    "Hit",
    "Mailbox",
    "Read",
    "failed",
]
