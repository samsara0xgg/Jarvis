"""ADR 0241: his mail is one read-only tool that searches, summarizes and reads."""

from __future__ import annotations

import html
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parseaddr, parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Final

from jarvis.execution.tools import Tool, ToolError
from jarvis.shared import CallerPrincipal

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from datetime import tzinfo

    from jarvis.execution.tools import ToolContext

_DESCRIPTION: Final = (
    "Use for any question about Allen's email: finding, summarizing, what needs a reply, what a "
    "message says. It only reads; to reply use write_mail_draft. No arguments: his unread "
    "Primary mail with reply marks (yes = needs an answer) and importance (0-3), junk left out. "
    "query: search all mail (Gmail syntax or plain words), up to 8 results with sender, "
    "subject, date and snippet, usually enough to answer; page=2 with the same query gives "
    "results 9-16, page=3 17-24. read: a message id, opens that one message as plain text, "
    "only when he asks what it says."
)
_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "Search words or Gmail syntax, e.g. from:lee co-op newer_than:30d.",
        },
        "page": {
            "type": "integer",
            "description": "With query: 2 gives results 9-16, 3 gives 17-24; omit for the first 8.",
        },
        "read": {
            "type": "string",
            "description": "A message id from an earlier result; returns that message's text.",
        },
    },
    "additionalProperties": False,
}
_PAGE: Final = 8
_NOON: Final = 12
_NAME_CHARS: Final = 60
_SUBJECT_CHARS: Final = 120
_SNIPPET_CHARS: Final = 200
_BODY_CHARS: Final = 4000
_ATTACHMENTS_SHOWN: Final = 10
_OWN_ERRORS: Final = frozenset({"invalid_arguments", "not_found"})
"""The codes of errors this tool words itself; any other failure is Gmail's."""
_NOT_FOUND: Final = re.compile(r"not found|404|invalid id", re.IGNORECASE)
_SIGN_IN: Final = re.compile(
    r"auth|log ?in|sign ?in|token|credential|expired|permission|\b40[13]\b", re.IGNORECASE,
)
# A reply's quoted lines, the "On <date>, <who> wrote:" line that opens the older message, the
# signature delimiter and the phone's signature. Forwards and Outlook headers are left alone: a
# forwarded letter is the content.
_QUOTE_FROM: Final = re.compile(
    r"^(?:On [^\n]{1,200}(?:\n[^\n]{0,100})?\bwrote:|-- ?$|Sent from my (?:iPhone|iPad|Android))",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass(frozen=True)
class _Held:
    """The last query: the token Gmail gave for each page reached and its last page once seen."""

    query: str = ""
    tokens: dict[int, str | None] = field(default_factory=dict)
    last: int | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def _cut(text: str, limit: int) -> str:
    """One line of at most ``limit`` characters."""
    line = " ".join(text.split())
    return line if len(line) <= limit else f"{line[: limit - 1]}…"


def _when(at: datetime, now: datetime, zone: tzinfo) -> str:
    """``at`` as he would say it: ``today (Thu) 2:40 pm``, else ``Mon Sep 22, 2:40 pm``."""
    local = at.astimezone(zone)
    today = now.astimezone(zone).date()
    hour = local.hour % _NOON or _NOON
    clock = f"{hour}:{local.minute:02d} {'am' if local.hour < _NOON else 'pm'}"
    day = {0: "today", 1: "yesterday"}.get((today - local.date()).days)
    if day:
        return f"{day} ({local:%a}) {clock}"
    year = f" {local.year}" if local.year != today.year else ""
    return f"{local:%a %b} {local.day}{year}, {clock}"


def _dated(header: object) -> datetime | None:
    """A Date header as an aware time; None when there is none or it does not parse."""
    try:
        at = parsedate_to_datetime(str(header))
    except (TypeError, ValueError):
        return None
    return at if at.tzinfo else at.replace(tzinfo=UTC)


def _iso(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _trim(body: str) -> tuple[str, bool]:
    """``body`` without quoted lines, the older message they quote and the signature.

    Also whether anything went; a body that is nothing but those is returned as it is.
    """
    kept = "\n".join(line for line in body.split("\n") if not line.lstrip().startswith(">"))
    if found := _QUOTE_FROM.search(kept):
        kept = kept[: found.start()]
    kept = re.sub(r"\n{3,}", "\n\n", kept).strip()
    return (kept, True) if kept and kept != body else (body, False)


def _found(query: str, seen: int, count: int, *, more: bool, total: object) -> str:
    """The line that opens a page of results: which of them these are, newest first."""
    if not seen and not more:
        return f'{count} message{"" if count == 1 else "s"} match "{query}", newest first.'
    # Gmail's total is an estimate: it is named only when it is more than has been shown.
    of = f" of about {total}" if more and isinstance(total, int) and total > seen + count else ""
    return f'Messages {seen + 1}-{seen + count}{of} for "{query}", newest first.'


def _failed(exc: Exception) -> ToolError:
    """Gmail's failure as the model should relay it; nothing to retry."""
    detail = _cut(str(exc), 200)
    if isinstance(exc, LookupError):
        msg = "Gmail is not connected; tell him it needs connecting."
        return ToolError(msg, code="unavailable")
    if _SIGN_IN.search(detail):
        msg = f"Gmail needs reconnecting ({detail}); tell him, mail cannot be read until he does."
        return ToolError(msg, code="unavailable")
    msg = f"Gmail did not answer ({detail}); tell him mail could not be read just now."
    return ToolError(msg, code="network_error")


def _argument(args: Mapping[str, Any], name: str) -> str:
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
        msg = "mail: page must be a whole number, e.g. page=2 for results 9-16"
        raise ToolError(msg, code="invalid_arguments")
    return value


class _Mail:
    """The tool's handler: the overview, a page of a search, one message."""

    def __init__(
        self,
        unread: Callable[[], Mapping[str, Any]],
        gmail: Callable[[str, Mapping[str, Any]], dict[str, Any]],
        text: Callable[[str], str],
        zone: tzinfo,
        clock: Callable[[], datetime],
    ) -> None:
        self._unread = unread
        self._gmail = gmail
        self._text = text
        self._zone = zone
        self._clock = clock
        self._lock = threading.Lock()
        self._held = _Held()

    def run(self, args: Mapping[str, Any], _ctx: ToolContext) -> dict[str, Any]:
        """Dispatch on the arguments; a Gmail failure is worded for the model."""
        query, read = _argument(args, "query"), _argument(args, "read")
        page = _page(args)
        if read and (query or page > 1):
            msg = "mail: pass read=<id> alone, or query=..., not both"
            raise ToolError(msg, code="invalid_arguments")
        if page > 1 and not query:
            msg = "mail: page continues a search; pass the same query=... as that search"
            raise ToolError(msg, code="invalid_arguments")
        try:
            if read:
                return self._read(read)
            return self._search(query, page) if query else self._overview()
        except LookupError as exc:  # the home's "not connected".
            raise _failed(exc) from exc
        except ToolError as exc:
            if exc.code in _OWN_ERRORS:
                raise
            raise _failed(exc) from exc

    def _overview(self) -> dict[str, Any]:
        """``Home.mail``'s unread list (reply marks and ratings are its) as compact rows."""
        letters = list(self._unread().get("unread") or [])
        kept = [one for one in letters if not one.get("junk")]
        now, rows = self._clock(), []
        for one in kept:
            row = {k: one[k] for k in ("id", "from", "subject") if one.get(k) is not None}
            if at := _iso(one.get("received")):
                row["when"] = _when(at, now, self._zone)
            rows.append({**row, **{
                k: one[k] for k in ("reply", "importance", "category") if one.get(k) is not None
            }})
        asking = list(dict.fromkeys(r["from"] for r in rows if r.get("reply") == "yes"))
        summary = f"{len(rows)} unread in Primary" if rows else "No unread mail in Primary"
        if asking:
            summary += f"; {len(asking)} {'needs' if len(asking) == 1 else 'need'} a reply"
            summary += f" ({', '.join(asking[:3])})"
        return {"summary": f"{summary}.", "unread": rows, "junk_left_out": len(letters) - len(kept)}

    def _search(self, query: str, page: int) -> dict[str, Any]:
        """Page ``page`` of ``_PAGE`` results, each with its headers and snippet read in parallel.

        Gmail's page tokens are held for the last query. A page not reached yet is walked to with
        id-only searches from the furthest page held, page 1 for a query that is not held.
        """
        with self._lock:
            held = self._held if self._held.query == query else _Held(query)
        tokens, last = {1: None, **held.tokens}, held.last
        at = max(n for n in tokens if n <= page)
        while at < page and (last is None or page <= last):
            found = self._gmail("gmail_search", self._ask(query, tokens[at]))
            if not found.get("nextPageToken"):
                last = at
                break
            at += 1
            tokens[at] = str(found["nextPageToken"])
        ids: list[str] = []
        found = {}
        if last is None or page <= last:
            found = self._gmail("gmail_search", self._ask(query, tokens[page]))
            ids = [str(one["id"]) for one in found.get("messages") or []]
            if found.get("nextPageToken"):
                tokens[page + 1] = str(found["nextPageToken"])
            else:
                last = page if ids or page == 1 else page - 1
        with self._lock:
            self._held = _Held(query, tokens, last)
        if not ids and page > 1:
            end = page - 1 if last is None else last
            msg = f'There is no page {page} of "{query}"; its results end at page {end}.'
            return {"summary": msg, "messages": [], "more": False}
        now = self._clock()
        rows = [self._row(head, now) for head in self._heads(ids)]
        more = last is None or page < last
        if not rows:
            return {"summary": f'No messages match "{query}".', "messages": [], "more": False}
        line = _found(
            query, (page - 1) * _PAGE, len(rows), more=more, total=found.get("resultSizeEstimate"),
        )
        return {"summary": line, "messages": rows, "more": more}

    @staticmethod
    def _ask(query: str, token: str | None) -> dict[str, Any]:
        ask: dict[str, Any] = {"query": query, "maxResults": _PAGE}
        if token:
            ask["pageToken"] = token
        return ask

    def _heads(self, ids: list[str]) -> list[dict[str, Any]]:
        if not ids:
            return []
        with ThreadPoolExecutor(len(ids), thread_name_prefix="mail-head") as pool:
            return list(pool.map(
                lambda one: self._gmail("gmail_get", {"messageId": one, "format": "metadata"}), ids,
            ))

    def _row(self, head: Mapping[str, Any], now: datetime) -> dict[str, Any]:
        name, address = parseaddr(str(head.get("from") or ""))
        row = {
            "id": str(head["id"]),
            "from": _cut(name or address, _NAME_CHARS),
            "subject": _cut(str(head.get("subject") or ""), _SUBJECT_CHARS),
        }
        if at := _dated(head.get("date")):
            row["when"] = _when(at, now, self._zone)
        # Gmail's snippet is HTML-escaped ("don&#39;t").
        if snippet := _cut(html.unescape(str(head.get("snippet") or "")), _SNIPPET_CHARS):
            row["snippet"] = snippet
        return row

    def _read(self, message_id: str) -> dict[str, Any]:
        """One message whole: headers, the body as clean text, attachment names."""
        try:
            message = self._gmail("gmail_get", {"messageId": message_id, "format": "full"})
        except ToolError as exc:
            if _NOT_FOUND.search(str(exc)):
                msg = f"No mail with id {message_id}; search with mail(query=...) first"
                raise ToolError(msg, code="not_found") from exc
            raise
        sender = str(message.get("from") or "")
        name, address = parseaddr(sender)
        subject = str(message.get("subject") or "")
        body, trimmed = _trim(self._text(str(message.get("body") or "")))
        cut = len(body) > _BODY_CHARS
        body = body[:_BODY_CHARS].rstrip() if cut else body
        body += "\n(truncated)" if cut else ""
        body += "\n(quoted replies and signature removed)" if trimmed else ""
        files = [
            str(one.get("filename") or one.get("name") or "") if isinstance(one, dict) else str(one)
            for one in message.get("attachments") or []
        ]
        files = [one for one in files if one][:_ATTACHMENTS_SHOWN]
        at = _dated(message.get("date"))
        when = _when(at, self._clock(), self._zone) if at else ""
        summary = f"From {name or address or 'an unknown sender'}{f', {when}' if when else ''}: "
        summary += f"{_cut(subject, _SUBJECT_CHARS) or '(no subject)'}."
        if files:
            summary += f" {len(files)} attachment{'' if len(files) == 1 else 's'}."
        optional = {
            "to": str(message.get("to") or ""),
            "when": when,
            # The ISO form is for the mail card (ADR 0063), which parses it; he hears `when`.
            "at": at.astimezone(self._zone).isoformat(timespec="minutes") if at else "",
            "attachments": files,
        }
        return {
            "summary": summary,
            "id": str(message.get("id") or message_id),
            "thread_id": str(message.get("threadId") or ""),
            "from": sender,
            "subject": subject,
            **{k: v for k, v in optional.items() if v},
            "body": body or "(no text)",
        }


def build_mail_tool(
    unread: Callable[[], Mapping[str, Any]],
    gmail: Callable[[str, Mapping[str, Any]], dict[str, Any]],
    text: Callable[[str], str],
    zone: tzinfo,
    clock: Callable[[], datetime] = _now,
) -> tuple[Tool, ...]:
    """``mail`` over ``Home``: its unread list, its ``gmail`` calls and the HTML-to-text read.

    Only ``gmail_search`` and ``gmail_get`` are ever called through ``gmail``.
    """
    return (
        Tool(
            name="mail",
            description=_DESCRIPTION,
            input_schema=_SCHEMA,
            handler=_Mail(unread, gmail, text, zone, clock).run,
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L0",
            read_only=True,
        ),
    )


__all__ = ["build_mail_tool"]
