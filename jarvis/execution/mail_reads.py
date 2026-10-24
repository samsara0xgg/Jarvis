"""ADR 0362, 0363: a mail, a conversation and an attachment, read for the model.

A mail is read from its raw message (``jarvis.execution.mail_gmail``): its cleaned text in parts
of about 3,500 characters cut at paragraphs, or the paragraphs that match what he is looking for
(``jarvis.execution.paragraphs``), and what was left out said in words. A conversation has no read
on the server, so its messages are found by a search on its subject and kept when they carry its
thread id. A PDF attachment is text paged the same way; a scan says it is one.

Layer rules: execution; stdlib, ``jarvis.execution`` siblings and ``jarvis.state.mail_message``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from jarvis.execution import speakable
from jarvis.execution.paragraphs import parts, pick
from jarvis.execution.tools import ToolError
from jarvis.state.mail_message import Attachment, Person, attachment_text

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime, tzinfo

    from jarvis.execution.mail_gmail import Mailbox, Read

RESULT_CHARS: Final = 4000
"""About how many characters one result of this tool takes."""
THREAD_SHOWN: Final = 6
"""Messages of a conversation one page shows, the newest first page; the rest are on later pages."""
THREAD_SEARCH_CAP: Final = 200
"""How many matches of a subject search are looked through for a conversation's messages."""
THREAD_READ_MAX: Final = 24
"""How many of a conversation's messages are read, the newest first."""
_PEOPLE_SHOWN: Final = 5
_SUBJECT_CHARS: Final = 200
_QUERY_CHARS: Final = 40
_MIN_TEXT: Final = 1200
_SUMMARY_ROOM: Final = 500
"""What the tool's own summary and notes take of a result."""
_ROW_NOTES: Final = 110
"""What a conversation row's ``text`` and ``text_cut`` keys take besides the text."""
_KB: Final = 1024
_PREFIX: Final = re.compile(
    r"^\s*(?:re|fwd?|fw|回复|答复|回覆|转发)\s*[:：]\s*",  # noqa: RUF001 — Chinese mail's colon.
    re.IGNORECASE,
)
_CUT_WORDS: Final = {"quote": "an older message it quotes", "signature": "its signature"}


def _size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False))


def _person(one: Person) -> str:
    return f"{one.name} <{one.address}>" if one.name else one.address


def _people(people: Sequence[Person]) -> str:
    shown = [_person(one) for one in people[:_PEOPLE_SHOWN]]
    more = len(people) - _PEOPLE_SHOWN
    return ", ".join(shown) + (f" (+{more} more)" if more > 0 else "")


def _bytes(count: int) -> str:
    if count < _KB:
        return f"{count} B"
    if count < _KB * _KB:
        return f"{round(count / _KB)} KB"
    return f"{count / (_KB * _KB):.1f} MB"


def _kind(one: Attachment) -> str:
    """A file's type as a word: its extension, else its MIME type's subtype."""
    suffix = one.name.rpartition(".")[2] if "." in one.name else ""
    if 0 < len(suffix) <= 5:  # noqa: PLR2004 — an extension, not a name that happens to hold a dot.
        return suffix.upper()
    return (one.mime.rpartition("/")[2] or "file").upper()


def file_lines(files: Sequence[Attachment]) -> list[str]:
    """``1. march.pdf (PDF, 84 KB)``: each file with the number ``attachment`` takes."""
    return [
        f"{n}. {speakable.cut(one.name, 60)} ({_kind(one)}, {_bytes(one.size)})"
        for n, one in enumerate(files, 1)
    ]


def left_out(cut: Sequence[str]) -> list[str]:
    """What the reader left out of a body, in words."""
    return [_CUT_WORDS[one] for one in cut if one in _CUT_WORDS]


def view(
    text: str, *, page: int, looking_for: str, limit: int,
) -> tuple[str, dict[str, Any]]:
    """The part of ``text`` he asked for, and the keys that say which part it is.

    ``looking_for`` gives the opening and the paragraphs that match best, in text order, and says
    how many were left out; without it ``page`` (from 1) gives a part cut at paragraphs.
    """
    if not text:
        return "(no text)", {}
    if len(text) <= limit:
        return text, {}
    if looking_for:
        picked = pick(text, looking_for, limit)
        asked = speakable.cut(looking_for, _QUERY_CHARS)
        said = f'the opening and the paragraphs that match "{asked}"'
        if picked.left_out:
            said += f"; {picked.left_out} other paragraphs left out"
        return picked.text, {"showing": said}
    chunks = parts(text, limit)
    if page > len(chunks):
        msg = f"mail: this has {len(chunks)} parts; page={page} is past the end"
        raise ToolError(msg, code="invalid_arguments")
    return chunks[page - 1], {"part": f"part {page} of {len(chunks)}"}


def _head(
    read: Read, now: datetime, zone: tzinfo, *, n: int | None = None,
) -> dict[str, Any]:
    """A read mail's headers: who, to whom, when, what about, its files."""
    mail = read.mail
    out: dict[str, Any] = {}
    if n is not None:
        out["n"] = n
    out["message"] = read.id
    if mail.sender.address or mail.sender.name:
        out["from"] = _person(mail.sender)
    if mail.to:
        out["to"] = _people(mail.to)
    if mail.cc:
        out["cc"] = _people(mail.cc)
    out["subject"] = speakable.cut(mail.subject, _SUBJECT_CHARS) or "(no subject)"
    if mail.at is not None:
        out["when"] = speakable.when(mail.at, now, zone)
        # The ISO form is for the mail record (ADR 0063), which parses it; he hears `when`.
        out["at"] = mail.at.astimezone(zone).isoformat(timespec="minutes")
    if mail.attachments:
        out["files"] = file_lines(mail.attachments)
    out["thread"] = read.thread
    return out


def _notice(read: Read, raw_max_bytes: int) -> str:
    """Why a mail's attachments were not read, when they were not."""
    if read.files_read:
        return ""
    if read.why_not == "size":
        return (
            f"its attachments were not read: the message is {_bytes(read.size)}, over the "
            f"{_bytes(raw_max_bytes)} limit"
        )
    return "its attachments were not read: the server gave no raw message"


def mail_json(  # noqa: PLR0913 — what one read is told: the mail, the part and where he is.
    read: Read, *, page: int, looking_for: str, limit: int, raw_max_bytes: int,
    now: datetime, zone: tzinfo, extras: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """One mail read: its headers, the part of its text he asked for and what was left out.

    ``extras`` are what the tool knows of the mail besides its text (a bill's facts, ADR 0365):
    they come after the headers and before the text.
    """
    head = {**_head(read, now, zone), **(extras or {})}
    # The headers take room too: the text gets what a result of about 4,000 characters has left.
    room = max(_MIN_TEXT, min(limit, RESULT_CHARS - _size(head) - 300))
    shown, extra = view(read.mail.text, page=page, looking_for=looking_for, limit=room)
    out: dict[str, Any] = {**head, "text": shown, **extra}
    if cut := left_out(read.mail.body.cut):
        out["left_out"] = cut
    if note := _notice(read, raw_max_bytes):
        out["notice"] = note
    return out


def summary_of(read: Read, now: datetime, zone: tzinfo) -> str:
    """One sentence that says whose mail it is, when, and what about."""
    mail = read.mail
    who = mail.sender.shown or "an unknown sender"
    when = f", {speakable.when(mail.at, now, zone)}" if mail.at else ""
    return f"From {who}{when}: {speakable.cut(mail.subject, 100) or '(no subject)'}."


# --- an attachment -----------------------------------------------------------------------------


def choose_file(files: Sequence[Attachment], wanted: str) -> Attachment:
    """The attachment ``wanted`` names: its number, its name, or a part of its name."""
    if not files:
        msg = "mail: this message has no attachments"
        raise ToolError(msg, code="not_found")
    text = wanted.strip()
    if text.isdigit() and 1 <= int(text) <= len(files):
        return files[int(text) - 1]
    lowered = text.lower()
    exact = [one for one in files if one.name.lower() == lowered]
    near = exact or [one for one in files if lowered and lowered in one.name.lower()]
    if len(near) == 1:
        return near[0]
    listed = "; ".join(file_lines(files))
    why = "names more than one of them" if near else "names none of them"
    msg = f"mail: file {wanted!r} {why}; its attachments are {listed}"
    raise ToolError(msg, code="invalid_arguments")


def attachment_json(  # noqa: PLR0913 — one attachment read: the file, the part and its limits.
    read: Read, one: Attachment, *, page: int, looking_for: str, limit: int, raw_max_bytes: int,
) -> dict[str, Any]:
    """One attachment as text, in parts as a mail is; a scan or an unreadable file says so."""
    if not read.files_read:
        msg = f"mail: {_notice(read, raw_max_bytes)}"
        raise ToolError(msg, code="attachments_not_read", kind="impossible")
    out: dict[str, Any] = {
        "message": read.id, "file": one.name, "type": _kind(one), "size": _bytes(one.size),
    }
    got = attachment_text(one)
    if got.kind == "text":
        shown, extra = view(got.text, page=page, looking_for=looking_for, limit=limit)
        return {**out, "text": shown, **extra}
    notes = {
        "scan": f"{one.name} is a scan: it has no text layer, so its words cannot be read",
        "locked": f"{one.name} is locked with a password, so it cannot be read",
        "unreadable": f"{one.name} could not be read as a document",
        "named": f"{one.name} is a {_kind(one)} file: only its name, type and size can be given",
    }
    return {**out, "notice": notes[got.kind]}


# --- a conversation ----------------------------------------------------------------------------


def subject_of(subject: str) -> str:
    """A conversation's subject without the ``Re:`` and ``Fwd:`` its replies add, and quotes."""
    text = subject
    while (stripped := _PREFIX.sub("", text, count=1)) != text:
        text = stripped
    return " ".join(text.replace('"', " ").split())


def _oldest_first(read: Read) -> tuple[float, str]:
    at = read.mail.at
    return (at.timestamp() if at is not None else float("inf"), read.id)


@dataclass(frozen=True)
class Thread:
    """A conversation found by its subject: its messages oldest first, and what the search saw."""

    messages: tuple[Read, ...]
    searched: bool
    """Its subject was searched (a message with no subject is only itself)."""
    more: bool
    """The search was cut at its cap, or more messages matched than are read: older ones may be
    missing."""


def find_thread(box: Mailbox, seed: Read) -> Thread:
    """The messages of ``seed``'s conversation: a search on its subject, kept by thread id.

    The server has no thread read. Messages that share the subject (after ``Re:``/``Fwd:``) and
    the thread id are found; a message whose subject was changed mid-conversation is not. The
    seed itself is always in.
    """
    subject = subject_of(seed.mail.subject)
    if not subject:
        return Thread((seed,), searched=False, more=False)
    found = box.search(f'subject:"{subject}"', cap=THREAD_SEARCH_CAP)
    ids = [one.id for one in found.hits if one.thread == seed.thread and one.id != seed.id]
    others = [one for one in box.reads(ids[: THREAD_READ_MAX - 1]) if one is not None]
    ordered = sorted([seed, *others], key=_oldest_first)
    return Thread(
        tuple(ordered), searched=True, more=found.more or len(ids) > THREAD_READ_MAX - 1,
    )


def _thread_row(read: Read, n: int, now: datetime, zone: tzinfo) -> dict[str, Any]:
    mail = read.mail
    row: dict[str, Any] = {
        "n": n, "message": read.id, "from": speakable.cut(mail.sender.shown, 40),
    }
    if mail.at is not None:
        row["when"] = speakable.when(mail.at, now, zone)
    if mail.attachments:
        row["files"] = [one.name for one in mail.attachments]
    return row


def thread_json(
    thread: Thread, *, page: int, now: datetime, zone: tzinfo,
) -> tuple[list[dict[str, Any]], int]:
    """One page of a conversation: the newest ``THREAD_SHOWN`` messages, oldest first among them.

    Page 2 is the ``THREAD_SHOWN`` before those. Each message gives its sender, time and own new
    text (quotes cut), the text cut to its share of the result. Also how many messages are on
    earlier pages (older than these).
    """
    total = len(thread.messages)
    end = total - (page - 1) * THREAD_SHOWN
    if end <= 0:
        msg = f"mail: this conversation has {total} messages; page={page} is past the end"
        raise ToolError(msg, code="invalid_arguments")
    start = max(0, end - THREAD_SHOWN)
    chosen = thread.messages[start:end]
    rows = [_thread_row(read, n, now, zone) for n, read in enumerate(chosen, start + 1)]
    room = max(
        _MIN_TEXT // 6,
        (RESULT_CHARS - _SUMMARY_ROOM - _size(rows)) // len(rows) - _ROW_NOTES,
    )
    for row, read in zip(rows, chosen, strict=True):
        mail = read.mail
        shown, extra = view(mail.text, page=1, looking_for="", limit=room)
        row["text"] = shown
        if extra:
            row["text_cut"] = f"cut; read it whole with message={row['n']}"
        if cut := left_out(mail.body.cut):
            row["left_out"] = cut
    return rows, start


__all__ = [
    "RESULT_CHARS",
    "THREAD_SHOWN",
    "Thread",
    "attachment_json",
    "choose_file",
    "file_lines",
    "find_thread",
    "left_out",
    "mail_json",
    "subject_of",
    "summary_of",
    "thread_json",
    "view",
]
