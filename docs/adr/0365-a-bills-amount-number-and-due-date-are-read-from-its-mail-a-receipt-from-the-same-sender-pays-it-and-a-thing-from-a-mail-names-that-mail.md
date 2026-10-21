# ADR 0365 — A bill's amount, number and due date are read from its mail, a receipt from the same sender pays it, and a thing from a mail names that mail

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** none

## Context

- 2026-10-18, Cloudflare:
  - A thing "pay Cloudflare" stood in his schedule, made from the invoice mail.
  - Cloudflare's payment confirmation had come the same minute.
  - Over four turns she said paid, then not paid, then asked him to check. Each turn searched
    again, and the invoice number matched only the invoice, which said the amount was due. No
    body was read.
- 2026-10-16: he asked how much a card statement wanted. The amount was only in its PDF.
- A thing made from a mail keeps the mail's id (`source_ref`). Schedule shows only "(from a mail,
  not confirmed)", so looking at the mail meant composing a search.
- Where the facts are:
  - many invoices and receipts carry schema.org markup (`Invoice`, `Order`) with the amount, the
    number and the payment status;
  - the rest print them in their text or their PDF (ADR 0363 reads both).
- He gets 15 to 53 mails a day (ADR 0274). The commitment intake already spends one haiku call
  on each mail that names a date.
- On 2026-10-21 Allen approved (「就按你说的去吧」):
  - read a mail's transaction facts locally: amount, number, paid or due;
  - let a confirmation from the same sender mark the bill paid;
  - give a thing its source mail, so she opens it in one step.

## Decision

Read a mail's bill or payment facts by code, let a receipt from the same sender pay the bill it
matches, and make a thing from a mail name that mail.

- **Facts.**
  - From a mail it reads, code takes:
    - the amount and currency;
    - the invoice or order number;
    - the due date;
    - whether the mail asks for a payment, confirms one, or refunds one.
  - The mail's schema.org markup comes first, then its text, then its PDF.
  - A fact the mail does not state is left out, never guessed. No model reads it.
- **Matched.**
  - A bill is paid by a receipt from the same sender's domain that carries the same number, or
    the same amount and currency.
  - The receipt must be dated from the bill to its due date, or within a week after the bill
    when it names no due date.
  - A mail read as a bill says which receipt paid it and when, or that none has come from that
    sender since the bill.
- **His things.**
  - A thing made from a bill's mail is marked paid and done when its receipt arrives, and names
    the receipt.
  - A thing made from any mail shows that mail's id, so she reads it with one call.

## Alternatives rejected

- **Let her compare the mails each time.** That is 10-18: each turn read what its query returned,
  and the verdict followed the query.
- **A model call per mail to pull out the facts.**
  - Up to 53 mails a day would each cost a call.
  - The facts are printed in markup or in text that code can find, and code reads a digit the
    same way every time.
- **Only a receipt on the same day.** A bill paid three days later would stay unpaid in his
  schedule.

## Consequences

- A receipt from the same sender for another purchase of the same amount can mark the wrong bill
  paid. The thing names the receipt, so he can see which one it was.
- A bill whose amount is only in an image is not read.
- The match runs in two places, the mail tool and the intake that makes his things, and both
  must call the same code.
