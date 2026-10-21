"""Invented raw mail for the tests: messages as mail programs build them, and PDFs.

Every address is on example.com. ``raw`` gives the bytes Gmail's ``format=raw`` carries once
decoded; the parts a test names are the parts the message holds.
"""

from __future__ import annotations

from email.message import EmailMessage
from email.policy import default
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

PADDING = "".join(map(chr, (0x34F, 0x200C, 0xA0))) * 30
"""A preheader's padding: grapheme joiners, zero-width non-joiners and no-break spaces."""
ALEX = "Alex Wu <alex@example.com>"
SAM = "Sam Lee <sam@example.com>"


def raw(  # noqa: PLR0913 — the parts of a message, each optional.
    subject: str,
    *,
    sender: str = ALEX,
    to: str = SAM,
    cc: str = "",
    date: str = "Mon, 05 Oct 2026 15:04:00 -0700",
    html: str | None = None,
    plain: str | None = None,
    headers: Mapping[str, str] | None = None,
    attachments: Sequence[tuple[str, str, bytes]] = (),
    flowed: bool = False,
) -> bytes:
    """One message: ``plain``, ``html`` or both (as an alternative), then ``attachments``."""
    msg = EmailMessage(policy=default)
    msg["From"] = sender
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    msg["Date"] = date
    msg["Message-ID"] = "<m1@example.com>"
    for name, value in (headers or {}).items():
        msg[name] = value
    if plain is not None and html is not None:
        msg.set_content(plain)
        msg.add_alternative(html, subtype="html")
    elif html is not None:
        msg.set_content(html, subtype="html")
    else:
        msg.set_content(plain or "")
    if flowed:
        msg.replace_header("Content-Type", 'text/plain; charset="utf-8"; format=flowed; delsp=no')
    for name, mime, data in attachments:
        maintype, _, subtype = mime.partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return msg.as_bytes()


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def pdf(*pages: Sequence[str]) -> bytes:
    """A PDF with a text layer: each page's lines top to bottom in Helvetica."""
    objects: list[str] = [""] * 3  # 1 catalog, 2 pages, 3 font; pages follow
    kids: list[str] = []
    for lines in pages:
        page_id = len(objects) + 1
        kids.append(f"{page_id} 0 R")
        shown = " ".join(f"({_escape(line)}) Tj T*" for line in lines)
        stream = f"BT /F1 11 Tf 14 TL 50 740 Td {shown} ET"
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {page_id + 1} 0 R "
            "/Resources << /Font << /F1 3 0 R >> >> >>",
        )
        objects.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
    objects[0] = "<< /Type /Catalog /Pages 2 0 R >>"
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    objects[2] = "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    out = "%PDF-1.4\n"
    offsets: list[int] = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out.encode("latin-1")))
        out += f"{number} 0 obj\n{body}\nendobj\n"
    xref = len(out.encode("latin-1"))
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n"
    out += "".join(f"{offset:010d} 00000 n \n" for offset in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    return out.encode("latin-1")


def scanned_pdf() -> bytes:
    """A one-page PDF that holds no text: what a scan is to a text reader."""
    return pdf([])
