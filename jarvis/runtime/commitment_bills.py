"""ADR 0365: a receipt that pays a bill closes the thing his mail made from it.

The commitment intake (ADR 0274) calls these for each mail it reads, with the same two functions
the `mail` tool uses: the facts a mail states (:func:`jarvis.state.mail_bill.mail_facts`) and
whether a receipt pays a bill (:func:`jarvis.state.mail_bill.paid`). A receipt that arrives marks
the open things made from its sender's bills paid, naming the receipt; a bill that makes a thing
after its receipt came is settled at once by the same search the tool makes. The write is the
ledger's own (``things.close``: a ``thing.closed`` event that ``reopen`` undoes). Only Gmail's
search and get are used (the guard of :func:`jarvis.runtime.job_mail.gmail_read`).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

from jarvis.execution import mail_bills
from jarvis.execution.tools import ToolError
from jarvis.state import things
from jarvis.state.mail_bill import Facts, Seen, mail_facts, paid, registrable

if TYPE_CHECKING:
    import sqlite3
    from datetime import datetime, tzinfo

    from jarvis.execution.mail_gmail import Mailbox

LOGGER = logging.getLogger(__name__)

_SUBJECT_CHARS: Final = 80


def stated(box: Mailbox, message_id: str) -> Seen | None:
    """What a mail states of a bill or a payment, with its sender and date; None when nothing.

    A message Gmail no longer has, or that cannot be read just now, states nothing here: the
    bill step is an extra and never stops the intake.
    """
    try:
        read = box.read(message_id)
    except ToolError:
        LOGGER.info("commitments: bill facts of %s not read", message_id, exc_info=True)
        return None
    found = mail_facts(read.mail)
    mail = read.mail
    if found is None or mail.at is None or not mail.sender.address:
        return None
    return Seen(found, mail.sender.address, mail.at)


def _from_a_bill_mail(one: things.Thing) -> bool:
    """A thing a mail made, whose source is a Gmail message (an interview's ``job:<n>`` is not)."""
    return (
        one.source == "mail" and bool(one.source_ref) and ":" not in one.source_ref
        and not one.weekly_until  # a series is closed a date at a time, not by one receipt
    )


def _evidence(subject: str, said: Facts) -> str:
    return f'receipt "{subject[:_SUBJECT_CHARS]}": {said.line}'


def _close(  # noqa: PLR0913 — the thing, the receipt it names and the clock.
    conn: sqlite3.Connection, one: things.Thing, receipt: str, evidence: str,
    zone: tzinfo, now: datetime,
) -> bool:
    try:
        things.close(
            conn, one.thing_id, "paid", by="mail", evidence=evidence, ref=receipt, zone=zone,
            now=now,
        )
    except (ValueError, LookupError):
        LOGGER.info("commitments: %s was closed already", one.thing_id, exc_info=True)
        return False
    return True


def pay_open_things(  # noqa: PLR0913 — the receipt, where it came from and the clock.
    conn: sqlite3.Connection, box: Mailbox, receipt: Seen, message_id: str, subject: str,
    zone: tzinfo, now: datetime,
) -> int:
    """Mark paid the open things made from bills this receipt pays; how many it closed.

    ``receipt`` is a mail that confirms a payment. Each open thing from a mail of the same
    registrable domain has its source mail read, and the thing is paid when that mail is a bill
    the receipt pays.
    """
    if receipt.facts.kind != "paid":
        return 0
    domain, closed = registrable(receipt.sender), 0
    for one in things.open_things(conn):
        if not _from_a_bill_mail(one) or one.source_ref == message_id:
            continue
        if registrable(one.sender) != domain:
            continue
        bill = stated(box, one.source_ref)
        if bill is not None and paid(bill, receipt, zone):
            closed += _close(
                conn, one, message_id, _evidence(subject, receipt.facts), zone, now,
            )
    return closed


def settle_bill(  # noqa: PLR0913 — the bill, where it came from and the clock.
    conn: sqlite3.Connection, box: Mailbox, bill: Seen, message_id: str,
    zone: tzinfo, now: datetime,
) -> int:
    """Mark paid the open things made from this bill when its receipt came before them.

    A receipt dated before the bill's mail was read finds no thing to close, so the bill's own
    reading looks for it, as the tool does; how many things it closed.
    """
    if bill.facts.kind != "asks":
        return 0
    mine = [
        one for one in things.open_things(conn)
        if _from_a_bill_mail(one) and one.source_ref == message_id
    ]
    if not mine:
        return 0
    found = mail_bills.find_receipt(box, message_id, bill, zone)
    if found.receipt is None or found.receipt_facts is None:
        return 0
    evidence = _evidence(found.receipt.subject, found.receipt_facts)
    return sum(_close(conn, one, found.receipt.id, evidence, zone, now) for one in mine)
