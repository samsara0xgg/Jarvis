# ADR 0369 — A verification code is copied only when nothing names its number as something else, and each mail is copied at most once across restarts

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** in ADR 0266, a code being the number nearest a keyword with only years, amounts, phone and order numbers, URLs and promo codes refused, and each mail being handled once in an in-memory set

## Context

- The code reader (ADR 0266) takes the first 4-to-8-digit number after a keyword. It has taken:
  - an account number written just before the code ("…for account <number>: <code>");
  - promo codes;
  - order numbers.
  A wrong code on the clipboard is pasted into a login form and fails, and he has to open the
  mail anyway.
- A bare "code" or "PIN" is enough as a keyword, so an order code or a store PIN passes.
- Mails already handled are kept in memory, so after a restart a mail still inside
  `mail_codes.window_min` is copied again. On 2026-10-21 the brain restarted for several deploys.
- The code itself is never stored or logged (ADR 0266). That stays.
- On 2026-10-21 Allen approved tighter rules, and remembering the mails copied
  (「就按你说的去吧」).

## Decision

Copy a code only when a login word names it and nothing names its number as something else, and
copy from each mail at most once, across restarts.

- **A login word.**
  - A bare "code" or "PIN" counts only in a sentence about signing in, verifying or confirming.
  - The strong words stay: verification, one-time, passcode, OTP, security code, 验证码 and the
    rest.
- **Named as something else.** A number is refused when the words just before it make it another
  kind of number:
  - an account, order, invoice, reference, member, customer or tracking number;
  - a confirmation or booking number (a "confirmation code" for signing up stays a code);
  - a promo, coupon or gift code.
  - Of the numbers left, the nearest after the login word wins, as now.
- **Once per mail.** Each mail a code was copied from is recorded as an event: its message id and
  sender's domain, never the code. A mail with that record is not read again.

## Alternatives rejected

- **A model reads each new mail for its code.**
  - It would cost a call for every new mail at every poll.
  - The failures seen fall into word classes that a rule can name.
- **Keep the handled mails in memory.** A restart inside the window copies the code again, and
  the brain restarts at every deploy.

## Consequences

- A code introduced by an unusual word, or written right after the word "account", is no longer
  copied.
- The event log gains one small row per copied code.
