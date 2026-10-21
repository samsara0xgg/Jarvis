"""A fake of the Gmail Workspace server for the `mail` tests: invented mailboxes, Gmail's search.

``Gmail`` holds messages built as mail programs build them (``tests.support.mail_raw``) and answers
the server's tools the way ``Home.gmail`` receives them:

- ``gmail_search``: ids and thread ids newest first, a page token, ``resultSizeEstimate``. The
  query is evaluated: ``in:``, ``is:``, ``category:``, ``from:``, ``to:``, ``subject:``,
  ``after:`` and ``before:`` (epoch seconds), ``newer_than:``, ``has:attachment``, quoted phrases,
  words, ``-``, ``OR`` and parentheses. Words match whole tokens, as Gmail's do.
- ``gmail_get``: ``minimal`` and ``raw`` are Gmail's own object (``labelIds``, ``sizeEstimate``,
  ``internalDate``; ``raw`` base64url); ``metadata`` and ``full`` are the server's own shape, which
  carries no ``labelIds``, so nothing may rely on them there.
- ``gmail_batchModify``: label changes.

Any other tool is an ``AssertionError``: a test that expects no other Gmail call gets that.
Every address is on example.com.
"""

from __future__ import annotations

import base64
import email
import html as html_lib
import json
import re
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email import policy
from email.utils import format_datetime
from typing import TYPE_CHECKING, Any, Final

from jarvis.execution.tools import ToolError
from tests.support.mail_raw import ALEX, SAM, raw

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

PRIMARY: Final = ("INBOX", "UNREAD", "CATEGORY_PERSONAL")
PROMOTIONS: Final = ("INBOX", "UNREAD", "CATEGORY_PROMOTIONS")
UPDATES: Final = ("INBOX", "UNREAD", "CATEGORY_UPDATES")
SENT: Final = ("SENT",)
_PAGE_MAX: Final = 500
_WORD: Final = re.compile(r"\w+")
_TOKEN: Final = re.compile(r'\(|\)|(?:[^\s()"]*"[^"]*")+[^\s()"]*|[^\s()"]+')
_RELATIVE: Final = re.compile(r"(\d+)([hdmy])")
_UNITS: Final = {"h": timedelta(hours=1), "d": timedelta(days=1), "m": timedelta(days=30),
                 "y": timedelta(days=365)}


def stamp(at: datetime) -> str:
    """``at`` as a ``Date`` header."""
    return format_datetime(at)


@dataclass
class Stored:
    """One message in the fake mailbox."""

    id: str
    thread: str
    labels: set[str]
    data: bytes
    at: datetime
    size: int = 0

    def message(self) -> Any:  # noqa: ANN401 — the email package's message type.
        """The parsed message."""
        return email.message_from_bytes(self.data, policy=policy.default)


def _tokens(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def _has_run(haystack: Sequence[str], needle: Sequence[str]) -> bool:
    if not needle:
        return True
    span = len(needle)
    return any(
        list(haystack[i : i + span]) == list(needle) for i in range(len(haystack) - span + 1)
    )


def _unquote(value: str) -> str:
    return value.replace('"', "")


@dataclass
class Gmail:
    """The connected Workspace server over a scripted mailbox; ``calls`` records every call."""

    now: datetime = field(default_factory=lambda: datetime(2026, 10, 21, 18, 0, tzinfo=UTC))
    error: str | None = None
    """Every call answers ``{"error": ...}`` with this."""
    raw_missing: bool = False
    """``format=raw`` answers without a ``raw`` key (a shape the server may have)."""
    barrier: threading.Barrier | None = None
    """Every ``full`` read waits for the others: only a parallel fetch gets past."""
    messages: dict[str, Stored] = field(default_factory=dict)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # --- building the mailbox ------------------------------------------------------------------

    def add(  # noqa: PLR0913 — the parts of a message, each optional.
        self,
        id: str,  # noqa: A002 — Gmail's name for it.
        subject: str,
        *,
        sender: str = ALEX,
        to: str = SAM,
        at: datetime | None = None,
        thread: str | None = None,
        labels: Sequence[str] = PRIMARY,
        plain: str | None = None,
        html: str | None = None,
        headers: Mapping[str, str] | None = None,
        attachments: Sequence[tuple[str, str, bytes]] = (),
        cc: str = "",
        size: int | None = None,
        data: bytes | None = None,
    ) -> Stored:
        """Put one message in the mailbox; ``at`` is its ``Date`` (default: ten minutes ago)."""
        when = at or self.now - timedelta(minutes=10)
        body = data or raw(
            subject, sender=sender, to=to, cc=cc, date=stamp(when),
            plain=plain if plain is not None or html is not None else f"Text of {subject}.",
            html=html, headers=headers, attachments=attachments, message_id=f"<{id}@example.com>",
        )
        stored = Stored(id, thread or id, set(labels), body, when, size or len(body))
        self.messages[id] = stored
        return stored

    # --- the server ----------------------------------------------------------------------------

    def call(self, server: str, tool: str, args: Mapping[str, Any]) -> dict[str, Any]:
        """One call as ``McpServers.call`` answers it: the JSON in a text block."""
        assert server == "gmail"
        with self._lock:
            self.calls.append((tool, dict(args)))
        if self.error:
            return {"text": json.dumps({"error": self.error})}
        if tool == "gmail_search":
            body = self._search(args)
        elif tool == "gmail_get":
            body = self._get(args)
        elif tool == "gmail_batchModify":
            body = self._modify(args)
        else:
            msg = f"the fake Gmail does not know {tool}"
            raise AssertionError(msg)
        return {"text": json.dumps(body)}

    def tool(self, tool: str, args: Mapping[str, Any]) -> dict[str, Any]:
        """One call decoded as ``Home.gmail`` decodes it: a failure is a ``ToolError``."""
        answer = json.loads(self.call("gmail", tool, args)["text"])
        if "error" in answer:
            msg = f"{tool}: {answer['error']}"
            raise ToolError(msg, code="mcp_tool_error")
        return dict(answer)

    def names(self) -> list[str]:
        """The tools called, in order."""
        return [name for name, _ in self.calls]

    def of(self, tool: str) -> list[dict[str, Any]]:
        """The arguments of every call to ``tool``."""
        return [args for name, args in self.calls if name == tool]

    # --- search --------------------------------------------------------------------------------

    def ids(self, query: str) -> list[str]:
        """The ids ``query`` finds, newest first."""
        return [one.id for one in self._found(query, include_all=False)]

    def _found(self, query: str, *, include_all: bool) -> list[Stored]:
        tokens = _TOKEN.findall(query)
        tree, _ = self._expr(tokens, 0)
        wants_all = include_all or any(
            re.fullmatch(r"-?(?:in|label):(?:anywhere|trash|spam)", t) for t in tokens
        )
        hits = [
            one for one in self.messages.values()
            if (wants_all or not one.labels & {"TRASH", "SPAM"}) and tree(one)
        ]
        return sorted(hits, key=lambda one: (one.at, one.id), reverse=True)

    def _search(self, args: Mapping[str, Any]) -> dict[str, Any]:
        found = self._found(
            str(args.get("query") or ""), include_all=bool(args.get("includeSpamTrash")),
        )
        size = min(int(args.get("maxResults") or 100), _PAGE_MAX)
        start = int(args.get("pageToken") or 0)
        page = found[start : start + size]
        body: dict[str, Any] = {"resultSizeEstimate": len(found)}
        if page:
            body["messages"] = [{"id": one.id, "threadId": one.thread} for one in page]
        if start + len(page) < len(found):
            body["nextPageToken"] = str(start + len(page))
        return body

    # a tiny Gmail query: OR over AND over terms, each term a word, phrase, operator or group
    def _expr(self, tokens: list[str], at: int) -> tuple[Any, int]:
        parts = []
        node, at = self._all(tokens, at)
        parts.append(node)
        while at < len(tokens) and tokens[at] == "OR":
            node, at = self._all(tokens, at + 1)
            parts.append(node)
        return (lambda one: any(part(one) for part in parts)), at

    def _all(self, tokens: list[str], at: int) -> tuple[Any, int]:
        parts = []
        while at < len(tokens) and tokens[at] not in {"OR", ")"}:
            node, at = self._term(tokens, at)
            parts.append(node)
        return (lambda one: all(part(one) for part in parts)), at

    def _term(self, tokens: list[str], at: int) -> tuple[Any, int]:
        word = tokens[at]
        negate = word.startswith("-") and len(word) > 1
        word = word[1:] if negate else word
        if word == "(":
            inner, at = self._expr(tokens, at + 1)
            at += 1  # the closing bracket
        else:
            inner, at = self._leaf(word), at + 1
        return ((lambda one: not inner(one)) if negate else inner), at

    def _leaf(self, token: str) -> Any:  # noqa: ANN401 — a predicate on a stored message.
        key, colon, value = token.partition(":")
        if not colon or key not in {
            "in", "is", "label", "category", "from", "to", "cc", "subject", "after", "before",
            "newer_than", "older_than", "has", "rfc822msgid",
        }:
            phrase = _tokens(_unquote(token))
            return lambda one: _has_run(self._haystack(one), phrase)
        value = _unquote(value)
        return lambda one: self._operator(key, value, one)

    def _haystack(self, one: Stored) -> list[str]:
        msg = one.message()
        text = msg.get_body(("plain", "html"))
        content = text.get_content() if text is not None else ""
        return _tokens(" ".join(
            [str(msg["Subject"] or ""), str(msg["From"] or ""), str(msg["To"] or ""), content],
        ))

    def _operator(self, key: str, value: str, one: Stored) -> bool:  # noqa: C901, PLR0911
        msg = one.message()
        if key == "in":
            return value == "anywhere" or {
                "inbox": "INBOX", "sent": "SENT", "drafts": "DRAFT", "trash": "TRASH",
                "spam": "SPAM",
            }.get(value, "") in one.labels
        if key == "is":
            return {"unread": "UNREAD" in one.labels, "read": "UNREAD" not in one.labels,
                    "starred": "STARRED" in one.labels}.get(value, False)
        if key == "label":
            return value.upper().replace("-", "_") in one.labels
        if key == "category":
            name = "PERSONAL" if value == "primary" else value.upper()
            return f"CATEGORY_{name}" in one.labels
        if key in {"from", "to", "cc", "subject"}:
            return _has_run(_tokens(str(msg[key.title()] or "")), _tokens(value))
        if key == "after":
            return one.at.timestamp() >= _epoch(value)
        if key == "before":
            return one.at.timestamp() < _epoch(value)
        if key in {"newer_than", "older_than"}:
            found = _RELATIVE.fullmatch(value)
            assert found, value
            age = self.now - one.at
            limit = int(found[1]) * _UNITS[found[2]]
            return age <= limit if key == "newer_than" else age > limit
        if key == "has":
            return value == "attachment" and any(True for _ in msg.iter_attachments())
        if key == "rfc822msgid":
            return str(msg["Message-ID"] or "").strip("<>") == value.strip("<>")
        return False

    # --- get -----------------------------------------------------------------------------------

    def _get(self, args: Mapping[str, Any]) -> dict[str, Any]:
        one = self.messages.get(str(args["messageId"]))
        if one is None:
            return {"error": "Requested entity was not found."}
        form = args.get("format", "full")
        assert form in {"minimal", "metadata", "full", "raw"}, form
        msg = one.message()
        text = msg.get_body(("plain", "html"))
        content = text.get_content() if text is not None else ""
        flat = " ".join(re.sub(r"<[^>]+>", " ", content).split())[:100]
        snippet = html_lib.escape(flat, quote=True).replace("&#x27;", "&#39;")
        if form in {"minimal", "raw"}:
            answer: dict[str, Any] = {
                "id": one.id, "threadId": one.thread, "labelIds": sorted(one.labels),
                "snippet": snippet, "sizeEstimate": one.size, "historyId": "1",
                "internalDate": str(int(one.at.timestamp() * 1000)),
            }
            if form == "raw" and not self.raw_missing:
                answer["raw"] = base64.urlsafe_b64encode(one.data).decode().rstrip("=")
            return answer
        answer = {
            "id": one.id, "threadId": one.thread, "snippet": snippet,
            "subject": str(msg["Subject"] or ""), "from": str(msg["From"] or ""),
            "to": str(msg["To"] or ""), "date": str(msg["Date"] or ""),
        }
        if msg["Cc"]:
            answer["cc"] = str(msg["Cc"])
        if form == "full":
            if self.barrier is not None:
                self.barrier.wait(timeout=5)
            chosen = msg.get_body(("plain",)) or msg.get_body(("html",))
            answer["body"] = chosen.get_content() if chosen is not None else ""
            answer["attachments"] = [
                {
                    "filename": part.get_filename() or "", "mimeType": part.get_content_type(),
                    "attachmentId": f"att-{n}", "size": len(part.get_payload(decode=True) or b""),
                }
                for n, part in enumerate(msg.iter_attachments(), 1)
            ]
        return answer

    # --- label changes -------------------------------------------------------------------------

    def _modify(self, args: Mapping[str, Any]) -> dict[str, Any]:
        ids = [str(one) for one in args["messageIds"]]
        missing = [one for one in ids if one not in self.messages]
        if missing:
            return {"error": "Requested entity was not found."}
        for one in ids:
            self.messages[one].labels |= {str(x) for x in args.get("addLabelIds") or ()}
            self.messages[one].labels -= {str(x) for x in args.get("removeLabelIds") or ()}
        return {"modifiedCount": len(ids), "status": "success"}

    def labels(self, message: str) -> set[str]:
        """The labels a message holds now."""
        return set(self.messages[message].labels)

    def __iter__(self) -> Iterator[Stored]:
        return iter(self.messages.values())


def _epoch(value: str) -> float:
    """``after:`` and ``before:`` take epoch seconds or ``YYYY/MM/DD`` (UTC here)."""
    if value.isdigit():
        return float(value)
    return datetime.strptime(value, "%Y/%m/%d").replace(tzinfo=UTC).timestamp()
