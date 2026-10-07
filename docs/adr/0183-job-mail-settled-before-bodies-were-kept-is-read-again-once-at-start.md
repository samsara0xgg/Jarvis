# ADR 0183 — Interview and offer mail settled before bodies were kept is read again at start

**Status:** Accepted
**Date:** 2026-10-06
**Supersedes:** none

## Context

- Allen, 2026-10-06: the Jobs page (ADR 0177, 0182) shows no time and no join link for his Reliable
  Controls Teams invitation ("Virtual interview", kind interview, 2026-10-02), and his Outlook
  calendar does not hold it either, so the Jobs page is the only place he would find it.
- ADR 0182 reads interview details and links from the body start that ADR 0162 keeps in
  `job_decision`. Mail settled before ADR 0162 has no kept body, so it has nothing to read.
- A Teams invitation puts the time alone on a line ("Thursday, October 8, 2026 2:00 PM (PDT)"),
  and the event extraction of ADR 0155 only takes a sentence that also holds an event word, so even
  a re-read body would give that mail no time.
- Gmail is read only by `gmail_search` and `gmail_get` (ADR 0155), and Jev's prompts and thresholds
  do not change (ADR 0158).

## Decision

At job-mail start, after the repair pass, read again from Gmail the body of visible interview and
offer mail that has no kept body, keep it as ADR 0162 does, and fill the event time it names.

Its limits:

- **Which mail.** Ledger rows with `deleted = 0`, kind `interview` or `offer`, and no `job_decision`
  row holding a body start. Newest first, at most 20 per start. A mail with a kept body is never
  read again, so a second start reads nothing it already kept. Receipts and the other kinds are not
  read: they are the bulk of the ledger and have no time to recover.
- **Read.** `gmail_get` in full format only, no Jev call. A mail that cannot be read, or Gmail not
  being connected, is logged and skipped; the poller goes on, and the mail is tried again at the next
  start.
- **Kept.** One `job_decision` row per mail, stage `reread`, verdict `job`, judge
  `local-reread/body-v1`, `body_status` `read`, the first 3000 characters of the body, and the header
  fields stored in `job_mail`. It carries no probabilities, no moment and no sender address. It
  is a body copy, not a verdict: the `job_seen` held-back list, the alerts and the audit read
  `job_seen` and `job_alert`, which it never touches.
- **Time.** A row with no `event_at` gets the time and sentence the existing extraction reads from
  the body. For this call, a line that is only a clear date and a clock time counts without an event
  word, because the mail is already known to be an interview or offer. Extraction at settle time is
  unchanged. A row that already has a time keeps it.

## Alternatives rejected

- **Read the body at view time on the Jobs route** — a Gmail call per page open and a route that can
  fail on the network; ADR 0182 makes that route read only local rows.
- **Let the event extraction take any date-and-time line for every mail** — a rejection or a
  newsletter with a date and clock in it would then get an event time at settle time.
- **Read every kind, receipts included, without a cap** — the ledger holds hundreds of receipts, so
  a first start would issue hundreds of `gmail_get` calls for rows that have no time to recover.

## Consequences

- Each start spends up to 20 `gmail_get` calls while interview or offer mail without a body exists,
  and every start retries a mail Gmail can no longer return; more than 20 such mails would starve
  the older ones.
- `memory.db` holds one more body start for each old interview or offer mail.
- Company and role are not recomputed from the new body at once; the repair pass of the next start
  does that, as it does for any kept body (ADR 0158).
- ADR 0162's "rows are only appended" still holds; the new rows use a fourth stage name that nothing
  counts.
