"""ADR 0365 in the commitment intake: a receipt pays the thing his mail made from a bill.

The real intake over a real event log and memory.db, the shared fake of the Gmail server
(``tests.support.gmail_fake``) and a fake model that makes one thing from the invoice. No key, no
network; every sender is on example.com.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

import pytest

from jarvis.runtime.commitment_intake import CommitmentIntake, CommitmentSettings
from jarvis.shared import lang
from jarvis.state import things
from tests.support.commitments import NOW, ZONE
from tests.support.gmail_fake import UPDATES, Gmail
from tests.support.runtime_log import RuntimeLog

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

CLOUD = "Example Cloud <billing@example.com>"
STRIPE = "Example Cloud <receipts@pay.example.net>"
INVOICE_SUBJECT = "Your Example Cloud invoice IN-4821"
INVOICE = "Invoice IN-4821\nTotal | $5.00 USD\nAmount due | $5.00 USD\nDue date | Oct 20, 2026\n"
PAID = "Thank you for your payment.\nAmount paid | $5.00 USD\nInvoice IN-4821\nReceipt R-5521\n"
OTHER_PAID = "Thank you for your payment.\nAmount paid | $9.99 USD\nReceipt R-7000\n"
TASK = {
    "action": "new", "title": "Pay Example Cloud", "start_at": "2026-10-20", "all_day": True,
    "private": False, "evidence": "Due date Oct 20, 2026",
}
SENT = datetime(2026, 10, 12, 15, 0, tzinfo=UTC)
A_MINUTE_LATER = datetime(2026, 10, 12, 15, 1, tzinfo=UTC)


class _Connections:
    def __init__(self, gmail: Gmail) -> None:
        self.gmail = gmail

    def client_for(self, server: str) -> Gmail:
        assert server == "gmail"
        return self.gmail


class _Model:
    """Makes a thing from the invoice and nothing from any other letter; remembers its asks."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def __call__(self, _system: str, user: str) -> str:
        subject = next(
            line for line in user.splitlines() if line.startswith("Subject: ")
        ).removeprefix("Subject: ")
        self.asked.append(subject)
        return json.dumps([TASK] if subject == INVOICE_SUBJECT else [])


@pytest.fixture
def log(tmp_path: Path) -> Iterator[RuntimeLog]:
    """A runtime root with a real event log and memory.db; the language is English."""
    before = lang.language()
    lang.set_language("en")
    one = RuntimeLog(tmp_path)
    yield one
    one.conn.close()
    lang.set_language(before)


def _intake(log: RuntimeLog, gmail: Gmail, model: _Model) -> CommitmentIntake:
    settings = CommitmentSettings(300, date(2026, 9, 10), 60, "haiku-bg", 60)
    intake = CommitmentIntake(
        settings, _Connections(gmail),  # type: ignore[arg-type]
        log.memory, log.paths.event_log, model,
    )
    intake.zone = "America/Los_Angeles"
    intake.now = lambda: NOW.astimezone(UTC)
    return intake


def _invoice(gmail: Gmail, *, at: datetime = SENT) -> None:
    gmail.add("inv", INVOICE_SUBJECT, sender=CLOUD, at=at, labels=UPDATES, plain=INVOICE)


def _receipt(
    gmail: Gmail, *, sender: str = "Example Cloud <no-reply@billing.example.com>",
    body: str = PAID, at: datetime = A_MINUTE_LATER,
) -> None:
    gmail.add("rec", "Receipt from Example Cloud", sender=sender, at=at, plain=body)


def _thing(log: RuntimeLog) -> things.Thing:
    (held,) = [one for one in things.fold(log.conn).values() if one.source_ref == "inv"]
    return held


def test_a_receipt_that_arrives_marks_the_thing_made_from_the_bill_paid_and_names_it(
    log: RuntimeLog,
) -> None:
    """The invoice made "Pay Example Cloud"; its receipt closes it as paid, by mail."""
    gmail, model = Gmail(now=NOW), _Model()
    _invoice(gmail)
    intake = _intake(log, gmail, model)
    assert intake.poll_once() == 1
    assert (_thing(log).state, _thing(log).source_ref, _thing(log).sender) == (
        "open", "inv", "example.com",
    )
    _receipt(gmail)
    assert intake.poll_once() == 1
    paid = _thing(log)
    assert paid.state == "paid"
    last = paid.history[-1]
    assert (last.what, last.by, last.ref) == ("paid", "mail", "rec")
    assert last.evidence == (
        'receipt "Receipt from Example Cloud": payment confirmed: 5.00 USD, no. R-5521'
    )
    assert model.asked == [INVOICE_SUBJECT]  # the receipt names no time: the model was not asked


def test_the_paid_thing_can_be_reopened_by_the_ledgers_own_undo(log: RuntimeLog) -> None:
    """The write is ``thing.closed``; ``reopen`` makes it open again."""
    gmail = Gmail(now=NOW)
    _invoice(gmail)
    _receipt(gmail)
    _intake(log, gmail, _Model()).poll_once()
    assert _thing(log).state == "paid"
    things.reopen(log.conn, _thing(log).thing_id, by="him", evidence="undo", zone=ZONE, now=NOW)
    assert _thing(log).state == "open"


def test_a_receipt_read_before_its_bill_made_a_thing_pays_it_when_the_thing_is_made(
    log: RuntimeLog,
) -> None:
    """Mail is read oldest first, so a receipt a minute before the bill's date comes first."""
    gmail = Gmail(now=NOW)
    _invoice(gmail)
    _receipt(gmail, at=SENT.replace(hour=14, minute=59))
    assert _intake(log, gmail, _Model()).poll_once() == 2
    paid = _thing(log)
    assert paid.state == "paid"
    assert paid.history[-1].ref == "rec"


def test_a_receipt_from_another_domain_or_another_amount_pays_nothing(log: RuntimeLog) -> None:
    """The match is the sender's registrable domain, then the number or the amount."""
    gmail = Gmail(now=NOW)
    _invoice(gmail)
    _receipt(gmail, sender=STRIPE)
    gmail.add("rec2", "Receipt", sender=CLOUD, at=SENT.replace(minute=2), plain=OTHER_PAID)
    _intake(log, gmail, _Model()).poll_once()
    assert _thing(log).state == "open"


def test_an_interviews_thing_and_a_said_thing_are_not_read_as_bills(log: RuntimeLog) -> None:
    """Only a thing whose source is a Gmail message is looked at; nothing else is fetched."""
    gmail = Gmail(now=NOW)
    for title, ref, source in (("Interview", "job:12", "mail"), ("Pay rent", "", "said")):
        things.add(
            log.conn,
            things.Thing(
                things.new_id(), title, "go", on_day="2026-10-20", source=source,
                source_ref=ref, sender="example.com",
            ),
            by="mail" if source == "mail" else "her", evidence="seeded", zone=ZONE, now=NOW,
        )
    _receipt(gmail)
    _intake(log, gmail, _Model()).poll_once()
    assert {one.title: one.state for one in things.fold(log.conn).values()} == {
        "Interview": "open", "Pay rent": "open",
    }
    assert {args["messageId"] for args in gmail.of("gmail_get")} == {"rec"}


def test_a_letter_that_mentions_no_bill_is_never_fetched_whole(log: RuntimeLog) -> None:
    """The raw message is read only for a mail that mentions a bill or a payment."""
    gmail = Gmail(now=NOW)
    gmail.add("hi", "Pottery Tuesday", sender=CLOUD, at=SENT, plain="Tuesday 7 PM at Studio 4.")
    _intake(log, gmail, _Model()).poll_once()
    assert {args.get("format") for args in gmail.of("gmail_get")} <= {"metadata", "full"}
    assert [args["messageId"] for args in gmail.of("gmail_get") if args["format"] == "raw"] == []
