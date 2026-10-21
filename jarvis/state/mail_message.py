"""ADR 0363: one raw message, read.

``parse`` turns an RFC 822 message into a :class:`Mail`: its headers, its body as text
(:mod:`jarvis.state.mail_text`: the HTML part when there is one, else the plain part) and its
attachments, all in memory. Nothing is written to disk.
:func:`attachment_text` reads a PDF with pypdf (as ``state/import_parsers.py`` does), a text, CSV
or calendar file as text, and names anything else.

Layer rules: stdlib, pypdf and :mod:`jarvis.state.mail_text`.
"""

from __future__ import annotations

import email
import io
import re
from dataclasses import dataclass, field
from datetime import UTC
from email import policy
from email.utils import getaddresses, parsedate_to_datetime
from typing import TYPE_CHECKING, Final, Literal

from pypdf import PdfReader

from jarvis.state.mail_text import Body, is_forward, read_html, read_plain, tidy_lines

if TYPE_CHECKING:
    from collections.abc import Collection, Iterator
    from datetime import datetime
    from email.message import Message, MIMEPart

# --- the message -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Person:
    """A name and an address, as a header gives them."""

    name: str
    address: str

    @property
    def shown(self) -> str:
        """How he would say who: the name, else the address."""
        return self.name or self.address


@dataclass(frozen=True)
class Attachment:
    """One attached file.

    Its bytes are in memory for this read only, and absent when the server only named the file.
    """

    name: str
    mime: str
    size: int
    data: bytes = field(default=b"", repr=False)


@dataclass(frozen=True)
class Mail:
    """One message, read."""

    sender: Person
    to: tuple[Person, ...]
    cc: tuple[Person, ...]
    reply_to: tuple[Person, ...]
    subject: str
    at: datetime | None
    message_id: str
    bulk: bool
    """Its headers say bulk: ``List-Unsubscribe``, ``List-Id`` or ``Precedence: bulk|list|junk``."""
    automated: bool
    """Its ``Auto-Submitted`` header is anything but ``no``."""
    body: Body
    html: str = field(repr=False)
    plain: str = field(repr=False)
    attachments: tuple[Attachment, ...]

    @property
    def text(self) -> str:
        """The body as text."""
        return self.body.text

    def addressed_to(self, own: Collection[str]) -> bool:
        """Whether one of ``own`` (lower-case addresses) is in its To."""
        return any(one.address.lower() in own for one in self.to)


def _header(msg: Message[str, str], name: str) -> str:
    try:
        return str(msg.get(name, "") or "")
    except Exception:  # noqa: BLE001 — a header the parser cannot read is an absent one.
        return ""


def _people(msg: Message[str, str], name: str) -> tuple[Person, ...]:
    try:
        values = [str(one) for one in msg.get_all(name, [])]
    except Exception:  # noqa: BLE001 — as above.
        return ()
    return tuple(
        Person(shown.strip(), address.strip())
        for shown, address in getaddresses(values) if address.strip()
    )


def _when(value: str) -> datetime | None:
    try:
        at = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    return at if at.tzinfo else at.replace(tzinfo=UTC)


def _content(part: MIMEPart) -> str:
    try:
        return str(part.get_content())
    except (LookupError, ValueError, KeyError):
        payload = part.get_payload(decode=True)
        return payload.decode("utf-8", "replace") if isinstance(payload, bytes) else ""


def _leaves(part: MIMEPart) -> Iterator[MIMEPart]:
    """The parts that are not containers; an attached message is one, not its own parts."""
    if part.get_content_maintype() == "message" or not part.is_multipart():
        yield part
        return
    for sub in part.iter_parts():
        yield from _leaves(sub)


def _attached_message(part: MIMEPart) -> tuple[str, bytes]:
    """The subject and bytes of a message attached as a whole."""
    inner = part.get_payload()
    if not isinstance(inner, list) or not inner:
        return "", b""
    first = inner[0]
    if isinstance(first, str):
        return "", b""
    try:
        return _header(first, "Subject"), first.as_bytes()
    except Exception:  # noqa: BLE001 — an attached message the parser cannot write out.
        return _header(first, "Subject"), b""


def _attachments(
    msg: MIMEPart, bodies: tuple[MIMEPart | None, ...],
) -> tuple[Attachment, ...]:
    """The files: named parts and attached messages, not the body or a picture the HTML embeds."""
    found: list[Attachment] = []
    for part in _leaves(msg):
        if any(part is body for body in bodies):
            continue
        mime = part.get_content_type()
        name = part.get_filename() or ""
        how = part.get_content_disposition()
        if mime.startswith("image/") and how == "inline" and part.get("Content-ID"):
            continue  # a picture the HTML shows in place
        if part.get_content_maintype() == "message":
            subject, data = _attached_message(part)
            found.append(Attachment(name or f"{subject or 'message'}.eml", mime, len(data), data))
            continue
        if not name and mime == "text/calendar":
            name = "invite.ics"
        if not name and how != "attachment":
            continue  # a part of the text
        payload = part.get_payload(decode=True)
        data = payload if isinstance(payload, bytes) else b""
        found.append(Attachment(name or "attachment", mime, len(data), data))
    return tuple(found)


def parse(raw: bytes) -> Mail:
    """One raw RFC 822 message, read."""
    msg = email.message_from_bytes(raw, policy=policy.default)
    subject = _header(msg, "Subject")
    forwarded = is_forward(subject)
    html_part = msg.get_body(("html",))
    plain_part = msg.get_body(("plain",))
    html = _content(html_part) if html_part is not None else ""
    plain = _content(plain_part) if plain_part is not None else ""
    body = read_html(html, forwarded=forwarded) if html.strip() else Body("")
    if not body.text and plain.strip():
        body = read_plain(
            plain,
            flowed=plain_part is not None and plain_part.get_param("format") == "flowed",
            delsp=plain_part is not None and plain_part.get_param("delsp") == "yes",
        )
    senders = _people(msg, "From")
    precedence = _header(msg, "Precedence").strip().lower()
    auto = _header(msg, "Auto-Submitted").strip().lower()
    return Mail(
        sender=senders[0] if senders else Person("", ""),
        to=_people(msg, "To"),
        cc=_people(msg, "Cc"),
        reply_to=_people(msg, "Reply-To"),
        subject=subject.strip(),
        at=_when(_header(msg, "Date")),
        message_id=_header(msg, "Message-ID").strip(),
        bulk=bool(_header(msg, "List-Unsubscribe") or _header(msg, "List-Id"))
        or precedence in {"bulk", "list", "junk"},
        automated=auto not in {"", "no"},
        body=body,
        html=html,
        plain=plain,
        attachments=_attachments(msg, (html_part, plain_part)),
    )


# --- attachments -------------------------------------------------------------------------------

_PDF_PAGES: Final = 60
_WIDE: Final = re.compile(r" {3,}")
_TEXT_NAMES: Final = (".txt", ".csv", ".tsv", ".ics", ".vcf", ".md", ".log")
_TEXT_TYPES: Final = frozenset({"application/ics", "application/csv", "application/x-ics"})
_SCAN_CHARS: Final = 20
"""A PDF whose pages hold fewer characters than this has no text layer: it is a scan."""


@dataclass(frozen=True)
class AttachmentText:
    """What reading an attachment gave: its text, or why it has none."""

    kind: Literal["text", "scan", "locked", "unreadable", "named"]
    text: str = ""


def _decoded(data: bytes) -> str:
    """A text file's characters: UTF-8, UTF-16 when it opens with its mark, else Windows-1252."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", "replace")
    try:
        return data.decode("utf-8-sig")
    except UnicodeError:
        return data.decode("cp1252", "replace")


def _pdf(data: bytes) -> AttachmentText:
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            return AttachmentText("locked")
        pages = [
            page.extract_text(extraction_mode="layout") or "" for page in reader.pages[:_PDF_PAGES]
        ]
    except Exception:  # noqa: BLE001 — a PDF is untrusted bytes; any way it fails is "not readable".
        return AttachmentText("unreadable")
    lines = [_WIDE.sub("  ", line.rstrip()) for page in pages for line in [*page.split("\n"), ""]]
    text = tidy_lines(lines)
    return AttachmentText("text", text) if len(text) >= _SCAN_CHARS else AttachmentText("scan")


def attachment_text(attachment: Attachment) -> AttachmentText:
    """A PDF's text (a scan says so), a text, CSV or calendar file as text, else just named."""
    name, mime = attachment.name.lower(), attachment.mime.lower()
    if mime == "application/pdf" or name.endswith(".pdf"):
        return _pdf(attachment.data)
    if mime == "text/html" or name.endswith((".html", ".htm")):
        return AttachmentText("text", read_html(_decoded(attachment.data)).text)
    if mime.startswith("text/") or mime in _TEXT_TYPES or name.endswith(_TEXT_NAMES):
        return AttachmentText("text", tidy_lines(_decoded(attachment.data).splitlines()))
    return AttachmentText("named")


__all__ = [
    "Attachment",
    "AttachmentText",
    "Mail",
    "Person",
    "attachment_text",
    "parse",
]
