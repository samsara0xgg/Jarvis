"""ADR 0365: the facts a mail states, and whether a receipt pays a bill.

Invented mail goes through ``parse`` (the real ``email`` package) and ``mail_facts``; the asserts
are the facts and the match, never how the words were found.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from jarvis.state.mail_bill import (
    Facts,
    Seen,
    facts,
    mail_facts,
    paid,
    registrable,
    text_facts,
    window,
)
from jarvis.state.mail_message import parse
from tests.support.mail_raw import pdf, raw, scanned_pdf

SENT = datetime(2026, 10, 18, 14, 0, tzinfo=UTC)
BILLING = "Example Cloud <billing@billing.example.com>"


def _facts(subject: str, body: str) -> Facts | None:
    return text_facts(subject, body, SENT)


# --- facts from the text -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("subject", "body", "line"),
    [
        (
            "Your Example Cloud invoice IN-4821 is ready",
            "Invoice number: IN-4821\nTotal | $5.00\nAmount due | $5.00\nDue date | Oct 25, 2026",
            "asks for payment: $5.00, no. IN-4821, due 2026-10-25",
        ),
        (
            "Receipt from Example Cloud #R-5521",
            "Thank you for your payment.\nAmount paid | $5.00\nInvoice IN-4821",
            "payment confirmed: $5.00, no. R-5521",
        ),
        (
            "Your statement is ready",
            "Statement balance | $1,234.56\nMinimum payment due | $35.00\n"
            "Payment due date | Nov 3, 2026",
            "asks for payment: $1,234.56, due 2026-11-03",
        ),
        (
            "Your refund",
            "We have refunded 12.40 CAD to your card. Order 123-4567",
            "refund: 12.40 CAD, no. 123-4567",
        ),
        (
            "Order #99887",
            "Charged $12.40 USD on Oct 18. Thanks for shopping.",
            "payment confirmed: 12.40 USD, no. 99887",
        ),
        (
            "您的发票 NO.20261018",
            "应付金额\uff1a¥128.00\n到期日期\uff1a2026年10月25日",
            "asks for payment: ¥128.00, no. 20261018, due 2026-10-25",
        ),
        (
            "付款成功", "合计 ￥59.00 订单编号\uff1aAB12345",
            "payment confirmed: ¥59.00, no. AB12345",
        ),
    ],
)
def test_the_facts_a_text_states(subject: str, body: str, line: str) -> None:
    """Amount, number, due day and kind are read from the words bills and receipts print."""
    found = _facts(subject, body)
    assert found is not None
    assert found.line == line


def test_a_mail_that_states_no_bill_has_no_facts() -> None:
    """A sale or a note about money is not a bill."""
    assert _facts("Weekly digest", "Save $20 on your next order! Total savings $50") is None
    assert _facts("Lunch?", "Are you free Thursday? I owe you $12.") is None


def test_a_zero_balance_asks_nothing_and_a_fact_not_stated_is_absent() -> None:
    """A fact the mail does not state, or states ambiguously, is absent."""
    found = _facts("Your invoice IN-77 is ready", "Amount due: $0.00\nDue date: 3/4/2026")
    assert found is not None
    assert found.kind == "asks"
    assert found.amount is None
    assert found.due is None  # 3/4 could be March 4 or April 3: not guessed
    assert found.number == "IN-77"
    assert _facts("Your invoice is ready", "Amount due: $0.00") is None


def test_a_day_with_no_year_is_the_mail_s_next_one() -> None:
    """A due day with no year is the next one after the mail's own date."""
    found = text_facts("Invoice", "Amount due $9.00. Due Jan 5", datetime(2026, 12, 20, tzinfo=UTC))
    assert found is not None
    assert found.due == date(2027, 1, 5)


def test_the_currency_symbol_is_kept_bare_when_the_mail_gives_no_more() -> None:
    """A bare ``$`` is not turned into one country's dollars."""
    found = _facts("Receipt", "Total paid $7.50")
    assert found is not None
    assert (found.amount, found.currency) == (Decimal("7.50"), "$")


# --- schema.org --------------------------------------------------------------------------------


def _ld(*objects: object) -> str:
    scripts = "".join(
        f'<script type="application/ld+json">{json.dumps(one)}</script>' for one in objects
    )
    return f"<html><head>{scripts}</head><body><p>Hello.</p></body></html>"


def test_json_ld_invoice_gives_amount_number_due_and_kind() -> None:
    """A JSON-LD ``Invoice`` states every fact."""
    html = _ld({
        "@context": "http://schema.org", "@type": "Invoice", "confirmationNumber": "INV-77",
        "paymentStatus": "http://schema.org/PaymentDue", "paymentDueDate": "2026-10-30T00:00:00Z",
        "totalPaymentDue": {"@type": "PriceSpecification", "price": "42.50",
                            "priceCurrency": "CAD"},
    })
    found = facts("Hello", "Hello.", markup=html, sent=SENT)
    assert found == Facts("asks", Decimal("42.50"), "CAD", "INV-77", date(2026, 10, 30))


def test_json_ld_invoice_marked_complete_is_a_payment() -> None:
    """A ``PaymentComplete`` invoice is a confirmation, and names no due day."""
    html = _ld({
        "@type": "Invoice", "identifier": "INV-77", "paymentStatus": "PaymentComplete",
        "totalPaymentDue": {"price": 42.5, "priceCurrency": "CAD"},
        "paymentDueDate": "2026-10-30",
    })
    found = facts("Thanks", "Thanks.", markup=html, sent=SENT)
    assert found == Facts("paid", Decimal("42.5"), "CAD", "INV-77")


def test_json_ld_order_in_a_graph_and_a_cancelled_order() -> None:
    """An ``Order`` inside ``@graph`` is read; a cancelled one is no payment."""
    graph = _ld({"@graph": [
        {"@type": "Organization", "name": "Example Shop"},
        {"@type": "Order", "orderNumber": "A-1009", "price": "19.99", "priceCurrency": "USD",
         "orderStatus": "http://schema.org/OrderProcessing"},
    ]})
    assert facts("Your order", "Thanks.", markup=graph, sent=SENT) == Facts(
        "paid", Decimal("19.99"), "USD", "A-1009",
    )
    gone = _ld({"@type": "Order", "orderNumber": "A-1", "orderStatus": "OrderCancelled"})
    assert facts("Your order", "Order cancelled.", markup=gone, sent=SENT) is None


def test_microdata_invoice_is_read_like_json_ld() -> None:
    """Microdata, including a nested price, gives the same facts."""
    html = (
        '<div itemscope itemtype="http://schema.org/Invoice">'
        '<meta itemprop="confirmationNumber" content="INV-88">'
        '<span itemprop="paymentStatus" content="http://schema.org/PaymentDue"></span>'
        '<time itemprop="paymentDueDate" datetime="2026-11-02">Nov 2</time>'
        '<div itemprop="totalPaymentDue" itemscope itemtype="http://schema.org/PriceSpecification">'
        '<span itemprop="price">12.00</span><meta itemprop="priceCurrency" content="USD">'
        "</div></div>"
    )
    assert facts("Bill", "Bill.", markup=html, sent=SENT) == Facts(
        "asks", Decimal("12.00"), "USD", "INV-88", date(2026, 11, 2),
    )


def test_markup_comes_first_and_the_text_fills_only_what_it_left_out() -> None:
    """The text never overrides the markup; it fills what the markup lacks."""
    html = _ld({"@type": "Invoice", "totalPaymentDue": {"price": "5.00", "priceCurrency": "USD"}})
    found = facts(
        "Your invoice IN-9", "Amount due $99.00\nDue date: Nov 3, 2026", markup=html, sent=SENT,
    )
    assert found == Facts("asks", Decimal("5.00"), "USD", "IN-9", date(2026, 11, 3))


# --- the PDF -----------------------------------------------------------------------------------


def test_a_statement_s_amount_and_due_date_are_read_from_its_pdf() -> None:
    """A statement whose figures are only in its PDF is read from it."""
    statement = pdf([
        "Example Bank card statement", "Statement date: Oct 18, 2026",
        "Total amount due: $230.45", "Payment due date: Nov 12, 2026",
    ])
    mail = parse(raw(
        "Your October statement is ready", plain="Your statement is attached.",
        attachments=[("statement.pdf", "application/pdf", statement)],
        date="Sun, 18 Oct 2026 14:00:00 +0000",
    ))
    found = mail_facts(mail)
    assert found is not None
    assert found.line == "asks for payment: $230.45, due 2026-11-12"


def test_a_scanned_pdf_gives_no_facts_it_cannot_read() -> None:
    """A PDF with no text layer adds nothing."""
    mail = parse(raw(
        "Your October statement is ready", plain="Your statement is attached.",
        attachments=[("statement.pdf", "application/pdf", scanned_pdf())],
    ))
    found = mail_facts(mail)
    assert found is None or (found.amount is None and found.due is None)


# --- the match ---------------------------------------------------------------------------------


def _bill(**given: object) -> Seen:
    base: dict[str, object] = {
        "kind": "asks", "amount": Decimal("5.00"), "currency": "USD", "number": "IN-4821",
        "due": date(2026, 10, 25),
    }
    return Seen(Facts(**{**base, **given}), "billing@example.com", SENT)  # type: ignore[arg-type]


def _receipt(
    at: datetime = SENT, sender: str = "no-reply@billing.example.com", **given: object,
) -> Seen:
    base: dict[str, object] = {
        "kind": "paid", "amount": Decimal("5.00"), "currency": "USD", "number": "R-5521",
        "also": ("IN-4821",),
    }
    return Seen(Facts(**{**base, **given}), sender, at)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("address", "domain"),
    [
        ("billing.example.com", "example.com"),
        ("Alex <alex@mail.billing.example.com>", "example.com"),
        ("shop@example.co.uk", "example.co.uk"),
        ("a@billing.example.com.au", "example.com.au"),
        ("example.com", "example.com"),
    ],
)
def test_the_registrable_domain(address: str, domain: str) -> None:
    """A subdomain is its registrable domain, with a country second level kept."""
    assert registrable(address) == domain


def test_a_receipt_from_the_same_domain_with_the_same_number_pays_the_bill() -> None:
    """A shared number is enough, whatever the amounts say."""
    assert paid(_bill(), _receipt(amount=99))


def test_the_same_amount_and_currency_pay_it_without_a_number() -> None:
    """With no shared number, the same amount in the same currency matches."""
    assert paid(_bill(number=""), _receipt(also=(), number="X-1"))
    assert not paid(_bill(number=""), _receipt(also=(), number="X-1", amount=Decimal("5.01")))
    assert not paid(_bill(number=""), _receipt(also=(), number="X-1", currency="EUR"))


def test_a_bare_dollar_sign_is_dollars_of_any_code() -> None:
    """``$`` against ``CAD`` is the same money; ``EUR`` is not."""
    assert paid(_bill(number="", currency="$"), _receipt(also=(), number="X-1", currency="CAD"))


def test_another_sender_does_not_pay_it() -> None:
    """A receipt from another domain, however alike, does not pay it."""
    assert not paid(_bill(), _receipt(sender="receipts@stripe.com"))
    assert not paid(_bill(), _receipt(sender="receipts@example.com.evil.test"))


def test_only_a_confirmation_pays_and_only_a_request_is_paid() -> None:
    """A bill is paid by a confirmation, never by a request or a refund."""
    assert not paid(_bill(kind="paid"), _receipt())
    assert not paid(_bill(), _receipt(kind="asks"))
    assert not paid(_bill(), _receipt(kind="refund"))


def test_a_receipt_is_dated_from_the_bill_to_its_due_date() -> None:
    """Last month's receipt and a late one do not pay this bill."""
    assert paid(_bill(), _receipt(SENT))
    assert paid(_bill(), _receipt(SENT + timedelta(days=7)))  # the due day itself
    assert not paid(_bill(), _receipt(SENT + timedelta(days=8)))
    assert not paid(_bill(), _receipt(SENT - timedelta(days=30)))  # last month's receipt


def test_with_no_due_date_a_receipt_has_a_week_after_the_bill() -> None:
    """A bill with no due day waits a week for its receipt."""
    assert paid(_bill(due=None), _receipt(SENT + timedelta(days=7)))
    assert not paid(_bill(due=None), _receipt(SENT + timedelta(days=8)))


def test_the_day_is_his_day_not_utc() -> None:
    """Days are counted in his zone, so a late-evening bill is that evening's."""
    vancouver = ZoneInfo("America/Vancouver")
    late = datetime(2026, 10, 18, 2, 0, tzinfo=UTC)  # 7 pm on the 17th in Vancouver
    bill = Seen(_bill().facts, "billing@example.com", late)
    assert window(bill, vancouver)[0] == date(2026, 10, 17)
    assert paid(bill, _receipt(late + timedelta(minutes=1)), vancouver)


def test_a_short_number_is_not_a_match_on_its_own() -> None:
    """A two-digit number is too common to name one invoice."""
    assert not paid(_bill(number="12", amount=Decimal("5.01")), _receipt(number="12", also=()))
