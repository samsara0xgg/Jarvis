"""ADR 0365 in the `mail` tool: a row or a read shows a bill's facts, and a bill says what paid it.

The tool runs through the real flat dispatcher over a fake Workspace server that holds invented
mail from example.com senders. Each check asserts what the model is sent, the Gmail calls the tool
made and the receipt it left; no network, no key, no model.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.support.gmail_fake import PRIMARY, UPDATES, Gmail
from tests.support.mail_raw import pdf
from tests.support.mail_rig import ME, ZONE, Rig, ago, mailbox, receipt_of

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

CLOUD = "Example Cloud <billing@example.com>"
BILLING = "Example Cloud <no-reply@billing.example.com>"
STRIPE = "Example Cloud <receipts@pay.example.net>"
INVOICE = (
    "Hello Allen,\n\nYour invoice IN-4821 is ready.\n\nTotal | $5.00 USD\nAmount due | $5.00 USD\n"
    "Due date | Oct 28, 2026\n"
)
PAID = "Thank you for your payment.\n\nAmount paid | $5.00 USD\nInvoice IN-4821\nReceipt R-5521\n"


def _bill(gmail: Gmail, *, at: float = 3, sender: str = CLOUD, body: str = INVOICE) -> None:
    gmail.add(
        "inv", "Your Example Cloud invoice IN-4821", sender=sender, to=ME,
        at=ago(days=at), labels=UPDATES, plain=body,
    )


def _paid(
    gmail: Gmail, *, days: float = 3, sender: str = BILLING, body: str = PAID, name: str = "rec",
) -> None:
    subject = "Receipt from Example Cloud"
    gmail.add(name, subject, sender=sender, to=ME, at=ago(days=days, minutes=-1), plain=body)


@pytest.fixture
def gmail() -> Gmail:
    """A mailbox with one old sent message, so his own address is known."""
    return mailbox()


@pytest.fixture
def rig(tmp_path: Path, gmail: Gmail) -> Iterator[Rig]:
    """The tool over ``gmail``."""
    made = Rig(tmp_path, gmail)
    yield made
    made.close()


def test_a_bill_read_says_which_receipt_paid_it_and_when(gmail: Gmail, rig: Rig) -> None:
    """The same sender's receipt, a minute after the invoice, with the number on both."""
    _bill(gmail)
    _paid(gmail)
    out = rig.ok(action="read", message="inv")
    assert out["bill"] == "asks for payment: 5.00 USD, no. IN-4821, due 2026-10-28"
    assert out["summary"].startswith("From Example Cloud, Sun Oct 18, 11:00 am: ")
    assert (
        'Paid: the receipt "Receipt from Example Cloud" from no-reply@billing.example.com,'
        " Sun Oct 18, 11:01 am matches it (payment confirmed: 5.00 USD, no. R-5521)."
    ) in out["summary"]


def test_a_bill_with_no_receipt_says_none_has_come_from_that_sender_since(
    gmail: Gmail, rig: Rig,
) -> None:
    """The window is the bill's day to its due day, and the count of what was looked at."""
    _bill(gmail)
    gmail.add("note", "Maintenance notice", sender=CLOUD, to=ME, at=ago(days=2), plain="Tonight.")
    out = rig.ok(action="read", message="inv")
    assert "No receipt from example.com since Sun Oct 18 (looked at 1 of their mails through" in (
        out["summary"]
    )
    assert "none confirms a payment" in out["summary"]
    assert "Paid" not in out["summary"]


def test_the_search_is_ids_first_over_the_senders_domain_and_the_bills_window(
    gmail: Gmail, rig: Rig,
) -> None:
    """``from:`` the registrable domain, the bill's local midnight to the day after its due day."""
    _bill(gmail)
    _paid(gmail)
    rig.ok(action="read", message="inv")
    query = rig.searches()[-1]
    first = int(ago(days=3).astimezone(ZONE).replace(hour=0, minute=0, second=0).timestamp())
    assert query.startswith("from:example.com after:")
    assert f"after:{first}" in query
    assert f"before:{first + 11 * 86400}" in query  # Oct 18 to Oct 28 inclusive
    full = [one for one in gmail.of("gmail_get") if one.get("format") == "full"]
    assert [one["messageId"] for one in full] == ["rec"]


def test_a_receipt_from_a_subdomain_pays_and_one_from_another_domain_does_not(
    gmail: Gmail, rig: Rig,
) -> None:
    """``billing.example.com`` is ``example.com``; ``pay.example.net`` is somebody else."""
    _bill(gmail)
    _paid(gmail, sender=STRIPE)
    other = rig.ok(action="read", message="inv")
    assert "No receipt from example.com" in other["summary"]
    _paid(gmail, name="rec2")
    same = rig.ok(action="read", message="inv")
    assert "Paid: the receipt" in same["summary"]


def test_a_last_months_receipt_of_the_same_amount_does_not_pay_this_months_bill(
    gmail: Gmail, rig: Rig,
) -> None:
    """The receipt is dated before the bill, so it is outside the window and is not searched."""
    _bill(gmail)
    _paid(gmail, days=33, body=PAID.replace("R-5521", "R-4410").replace("IN-4821", "IN-4101"))
    out = rig.ok(action="read", message="inv")
    assert "No receipt from example.com" in out["summary"]
    assert "looked at" not in out["summary"]
    assert "no mail from example.com came in that time" in out["summary"]


def test_a_receipt_after_the_due_day_is_not_the_bills_and_says_so(gmail: Gmail, rig: Rig) -> None:
    """A confirmation that came too late is counted, and named as not matching."""
    _bill(gmail, at=12)
    _paid(gmail, days=0.5, body=PAID.replace("IN-4821", "IN-9999").replace("5.00", "9.00"))
    out = rig.ok(action="read", message="inv")
    assert "No receipt from example.com" in out["summary"]


def test_the_same_amount_pays_it_when_the_receipt_names_no_number_of_the_bills(
    gmail: Gmail, rig: Rig,
) -> None:
    """A receipt that only states its own number and the amount still matches by amount."""
    _bill(gmail)
    _paid(gmail, body="Payment received.\nAmount paid | $5.00 USD\nReceipt R-5521\n")
    out = rig.ok(action="read", message="inv")
    assert "Paid: the receipt" in out["summary"]


def test_a_confirmation_that_matches_neither_number_nor_amount_is_counted_not_used(
    gmail: Gmail, rig: Rig,
) -> None:
    """Another purchase of another amount does not pay it."""
    _bill(gmail)
    _paid(gmail, body="Payment received.\nAmount paid | $9.99 USD\nReceipt R-7000\n")
    out = rig.ok(action="read", message="inv")
    assert out["summary"].count("No receipt from example.com") == 1
    assert "1 confirm a payment but not this bill's number or amount" in out["summary"]


def test_the_receipt_of_a_bill_read_covers_the_mail_and_the_search(gmail: Gmail, rig: Rig) -> None:
    """The window and the count of the sender's mail looked at are in ``covered``."""
    _bill(gmail)
    _paid(gmail)
    result = rig.run(action="read", message="inv")
    row = receipt_of(result)
    assert row["status"] == "done"
    assert row["line"].endswith(", receipt found")
    assert row["covered"]["window"] == (
        f"one mail, inv; mail from example.com, {ago(days=3).astimezone(ZONE).date().isoformat()}"
        f" to 2026-10-28"
    )
    assert row["covered"]["shown"] == 2


def test_reading_a_receipt_shows_its_facts_and_looks_for_nothing(gmail: Gmail, rig: Rig) -> None:
    """A confirmation is not a bill: no search for another mail."""
    _paid(gmail, days=1)
    before = len(gmail.of("gmail_search"))
    out = rig.ok(action="read", message="rec")
    assert out["bill"] == "payment confirmed: 5.00 USD, no. R-5521"
    assert len(gmail.of("gmail_search")) == before
    assert "receipt" not in out["summary"].lower().replace("receipt from", "")


def test_reading_several_mails_shows_each_ones_facts_and_says_its_receipt_was_not_looked_for(
    gmail: Gmail, rig: Rig,
) -> None:
    """Three mails share the result; a bill among them is not searched for."""
    _bill(gmail)
    _paid(gmail)
    gmail.add("hi", "Hello", sender=CLOUD, to=ME, at=ago(days=1), plain="Hi.")
    before = len(gmail.of("gmail_search"))
    out = rig.ok(action="read", message="inv,rec,hi")
    bills = [one.get("bill") for one in out["mails"]]
    assert bills == [
        "asks for payment: 5.00 USD, no. IN-4821, due 2026-10-28; read this mail alone to look for"
        " its receipt",
        "payment confirmed: 5.00 USD, no. R-5521",
        None,
    ]
    assert len(gmail.of("gmail_search")) == before


def test_rows_show_a_mails_facts_and_a_plain_mail_has_none(gmail: Gmail, rig: Rig) -> None:
    """A list's row carries ``bill`` only when the mail states one."""
    _bill(gmail)
    _paid(gmail, days=1)
    gmail.add("hi", "Hello", sender=CLOUD, to=ME, at=ago(days=1, hours=1), plain="Hi.")
    out = rig.ok(action="search", query="example")
    rows = {one["message"]: one for one in out["rows"]}
    assert rows["inv"]["bill"] == "asks for payment: 5.00 USD, no. IN-4821, due 2026-10-28"
    assert rows["rec"]["bill"] == "payment confirmed: 5.00 USD, no. R-5521"
    assert "bill" not in rows["hi"]


def test_a_statement_whose_figures_are_only_in_its_pdf_is_read_from_it(
    gmail: Gmail, rig: Rig,
) -> None:
    """The mail says a statement is attached; the amount and the due day are in the PDF."""
    statement = pdf([
        "Example Bank card statement", "Total amount due: $230.45",
        "Payment due date: Nov 12, 2026",
    ])
    gmail.add(
        "stmt", "Your October statement is ready", sender="Example Bank <cards@bank.example.com>",
        to=ME, at=ago(days=1), labels=PRIMARY, plain="Your statement is attached.",
        attachments=[("statement.pdf", "application/pdf", statement)],
    )
    out = rig.ok(action="read", message="stmt")
    assert out["bill"] == "asks for payment: $230.45, due 2026-11-12"
    assert out["files"][0].startswith("1. statement.pdf (PDF, ")


def test_an_invoice_that_carries_schema_org_markup_is_read_from_it(gmail: Gmail, rig: Rig) -> None:
    """JSON-LD in the HTML part gives the facts the visible text does not."""
    html = (
        '<html><head><script type="application/ld+json">{"@type": "Invoice",'
        ' "confirmationNumber": "INV-77", "paymentStatus": "PaymentDue",'
        ' "paymentDueDate": "2026-10-30", "totalPaymentDue": {"@type": "PriceSpecification",'
        ' "price": "42.50", "priceCurrency": "CAD"}}</script></head>'
        "<body><p>Your bill is ready. Open it online.</p></body></html>"
    )
    gmail.add("ld", "Your bill", sender=CLOUD, to=ME, at=ago(days=1), html=html)
    out = rig.ok(action="read", message="ld")
    assert out["bill"] == "asks for payment: 42.50 CAD, no. INV-77, due 2026-10-30"
