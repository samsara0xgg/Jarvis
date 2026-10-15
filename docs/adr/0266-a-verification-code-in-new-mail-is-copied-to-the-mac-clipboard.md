# ADR 0266 — A verification code in new mail is copied to the Mac clipboard

**Status:** Accepted
**Date:** 2026-10-15
**Supersedes:** none

## Context

- On 2026-10-15 Allen asked to get verification codes from his mail without opening the
  mailbox: the code lands on the Mac clipboard and a system notification says so.
- The Gmail client lives on the brain (the shared `gmail` MCP client, ADR 0155). The Mac holds
  none, and the clipboard and notifications are the Mac's (ADR 0170).
- A code is worth nothing after a few minutes, and a code mail is a secret: it must not be
  stored, logged or sent to a model.
- The job-mail poller already reads Gmail in the background with `gmail_search` and
  `gmail_get` only (ADR 0155).

## Decision

The brain polls Gmail for mail of the last few minutes and has the terminal copy any code it
finds. Its limits:

- **Read-only Gmail.** `gmail_search` (`after:<epoch seconds> -in:sent -in:drafts`) and
  `gmail_get` through `gmail_read`; nothing is changed, labelled or sent.
- **Local rules, no model.** `find_code` needs a keyword (code, verification, OTP, 验证码 ...)
  near a 4-8 digit number, or a 6-8 character capital mix under a strong keyword. Years,
  amounts, phone and order numbers, URLs and promo codes are refused.
- **Only recent mail.** A mail whose Date is older than `mail_codes.window_min` (default 10)
  minutes is skipped; each mail is handled once (in-memory set, 500 ids).
- **The code is never stored or logged.** Logs hold the message id and the sender's domain.
- **One new hidden terminal operation**, `copy_code` (`code`, `sender`): not a menu tool, so
  the model cannot call it. The terminal runs `pbcopy`, then a notification whose text is
  passed to `osascript` as arguments, never spliced into the script. With no terminal
  (one machine) the same function runs locally. A terminal that is not connected is retried
  each cycle while the mail is in the window.
- **Config:** `mail_codes: {enabled, poll_s, window_min}` in `config/jarvis.yaml`, owned by the
  brain; bad values stop boot.

## Alternatives rejected

- **Jev or another model extracts the code** — the mail body would leave the machine for a
  provider on every poll, and a model round trip (about 1-2 s) is added to a code that expires
  in minutes; a regex over 4-8 digits next to a keyword reads the common senders in the
  samples of `tests/fixtures/mail_codes/samples.json` without either cost.
- **The Mac polls Gmail itself** — the Mac holds no Gmail client or login; the brain's is the
  only one (ADR 0155), so a second one would be a second credential to keep.

## Consequences

- The clipboard is overwritten when a code arrives, whatever Allen had copied.
- A code in a mail whose wording the keywords do not cover is not found; the rule list grows
  by adding samples to the fixture.
- Another background Gmail search every `poll_s` seconds, on top of job mail's.
- A code that arrives while the Mac is not connected is lost once the mail leaves the window.
