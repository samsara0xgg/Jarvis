"""ADR 0365: a bill read in the `mail` tool says which receipt paid it.

The facts a mail states come from :mod:`jarvis.state.mail_bill`; a row shows them from the text the
server gave, and a read shows them from the whole raw message, its markup and its PDFs. Reading a
bill (a mail that asks for a payment) also searches that sender's mail from the bill's day to its
due day: ids only, then the candidates read in the server's text form, and the first that
:func:`jarvis.state.mail_bill.paid` accepts is the receipt. The intake that makes his things asks
the same question through :func:`find_receipt`.

Layer rules: execution; ``jarvis.execution.mail_gmail`` and ``jarvis.state.mail_bill``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Final

from jarvis.execution import speakable
from jarvis.state.mail_bill import Facts, Seen, paid, registrable, text_facts, window

if TYPE_CHECKING:
    from datetime import tzinfo

    from jarvis.execution.mail_gmail import Head, Mailbox

SEARCH_CAP: Final = 200
"""How many of a sender's mails in the window are paged as ids."""
READ_CAP: Final = 40
"""How many of them are read, the oldest first: a receipt follows its bill."""
_SUBJECT_CHARS: Final = 60


@dataclass(frozen=True)
class Verdict:
    """What looking for a bill's receipt found."""

    receipt: Head | None
    """The mail that paid it, with what it states in ``receipt_facts``."""
    receipt_facts: Facts | None
    domain: str
    first: date
    """The first day a receipt could be dated on: the bill's."""
    last: date
    """The last day: the bill's due day, else a week after it."""
    looked: int
    """How many of the sender's mails in that time were read."""
    confirmations: int
    """Of those, how many state a payment."""
    unread: int
    """How many more were in that time and were not read."""


def _midnight(day: date, zone: tzinfo) -> int:
    return int(datetime.combine(day, time.min, tzinfo=zone).timestamp())


def row_facts(head: Head) -> Facts | None:
    """The facts of a mail known by the server's own shape: its subject and the body it gave."""
    return text_facts(head.subject, head.body, head.at)


def find_receipt(box: Mailbox, message: str, bill: Seen, zone: tzinfo = UTC) -> Verdict:
    """The receipt of ``bill`` (the mail ``message``) among its sender's mail; or that none came."""
    domain = registrable(bill.sender)
    first, last = window(bill, zone)
    query = (
        f"from:{domain} after:{_midnight(first, zone)}"
        f" before:{_midnight(last + timedelta(days=1), zone)}"
    )
    found = box.search(query, cap=SEARCH_CAP)
    ids = [one.id for one in found.hits if one.id != message][::-1]  # oldest first
    heads = [one for one in box.heads(ids[:READ_CAP], full=True) if one is not None]
    confirmations = 0
    for head in heads:
        said = row_facts(head)
        if said is None or head.at is None:
            continue
        confirmations += said.kind == "paid"
        if paid(bill, Seen(said, head.sender.address, head.at), zone):
            return Verdict(
                head, said, domain, first, last, len(heads), confirmations,
                max(0, len(ids) - READ_CAP) + int(found.more),
            )
    return Verdict(
        None, None, domain, first, last, len(heads), confirmations,
        max(0, len(ids) - READ_CAP) + int(found.more),
    )


def _day(day: date, today: date) -> str:
    return speakable.day_name(day, today)


def line(verdict: Verdict, now: datetime, zone: tzinfo) -> str:
    """The verdict as a sentence for the model."""
    if verdict.receipt is not None:
        head = verdict.receipt
        who = head.sender.address or verdict.domain
        subject = speakable.cut(head.subject, _SUBJECT_CHARS) or "(no subject)"
        when = f", {speakable.when(head.at, now, zone)}" if head.at else ""
        said = f'Paid: the receipt "{subject}" from {who}{when} matches it'
        if verdict.receipt_facts is not None:
            said += f" ({verdict.receipt_facts.line})"
        return said + "."
    today = now.astimezone(zone).date()
    through = min(verdict.last, today)
    if verdict.looked == 0:
        seen = f"no mail from {verdict.domain} came in that time"
    else:
        seen = f"looked at {verdict.looked} of their mails through {_day(through, today)}"
        if verdict.confirmations:
            seen += (
                f"; {verdict.confirmations} confirm a payment but not this bill's number or"
                " amount"
            )
        else:
            seen += "; none confirms a payment"
        if verdict.unread:
            seen += f"; {verdict.unread} more were not read"
    return f"No receipt from {verdict.domain} since {_day(verdict.first, today)} ({seen})."


def window_text(verdict: Verdict) -> str:
    """What the search covered, for the receipt's ``covered``."""
    return f"mail from {verdict.domain}, {verdict.first.isoformat()} to {verdict.last.isoformat()}"


__all__ = ["READ_CAP", "SEARCH_CAP", "Verdict", "find_receipt", "line", "row_facts", "window_text"]
