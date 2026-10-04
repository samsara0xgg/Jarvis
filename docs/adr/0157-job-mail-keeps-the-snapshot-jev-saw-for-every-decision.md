# ADR 0157 — Job mail keeps the snapshot Jev saw for every decision

**Status:** Accepted
**Date:** 2026-10-04
**Supersedes:** none

## Context

- Allen, 2026-10-04: in the first live backfill cycle (25 messages) Jev's header
  question let about 15 newsletter-like mails through to the body read. The
  mails it cleared as not job were kept only as an id and a probability, and a
  mail held back with under 0.2 chance of being job mail kept no sender or
  subject at all (ADR 0155), so a miss could not be seen, and no other header
  wording, threshold or judge could be run over what Jev had been shown.
- ADR 0155 keeps the body out of `memory.db` and says only typed facts are
  kept. The body start Jev read already lands in `jev/decisions.jsonl` (ADR 0128),
  which is local, unpruned and not queryable by message.
- A mail the header question holds back is wrong in a way only Allen sees, so
  he needs a way to say so that also feeds the same record.

## Decision

Jarvis appends one `job_decision` row in `memory.db` for every Jev answer on a
job-mail letter, whatever the verdict, and lets Allen overrule a held-back mail
with one flag.

Its limits:

- A row holds the message id, time, stage (`header` or `body`), sender display
  name, sender domain (never the address), subject, received time, Jev's
  probabilities as JSON, the judge's name and wording version
  (`jev-1.13/header-v1`) and the verdict of that stage. A `body` row also holds
  the first `max_body_chars` of the plain-text body Jev read; a header row never
  does. A failed call is a row with the verdict `error` and no probabilities.
- Rows are only appended, never pruned, local only, and erased with `memory.db`
  by the erase-all-data path. They never leave the Mac.
- `job_seen` keeps its meaning. The audit list of held-back mail is now the
  newest 50 `not_job` mails by received time, whatever their probability, so
  `job_seen` keeps the sender, domain and subject of every held-back mail.
- `POST /inherent/jobs/{message_id}/flag` with `{"reaction":"should_alert"}`
  reads that mail again with `gmail_get` only, types its body with the header
  skip ignored, and delivers it as any job mail (the judge and the quiet level
  still apply). If Jev types it as not job it is stored as `job_other`, so
  Allen's flag wins. It writes `job_feedback` with `flag:should_alert`, and a
  second flag of the same mail does nothing.

## Alternatives rejected

- **Read the snapshot back out of `jev/decisions.jsonl`** — the file is
  unbounded (it was too large to scan in full on 2026-10-04), has no index by
  message and is written only when `jev_log.enabled`; a replay would be a grep
  over text rather than a query.
- **Keep every held-back mail's header in `job_seen` only** — it holds one row
  per message, so a later stage overwrites the earlier one, and it cannot hold
  the probabilities of both questions or the body read.
- **Store the body in the existing `job_mail` ledger row** — held-back mail
  never gets a ledger row, which is exactly the mail whose snapshot is missing.

## Consequences

- `memory.db` now holds the first 3000 characters of the body of every mail that
  passed the header step, not only typed facts. ADR 0155's "never the body"
  no longer holds for `job_decision`; it still holds for `job_mail`,
  `job_seen`, `job_alert` and `attention_log`. The spec's memory, retention and
  erase rows say so.
- The table grows by one or two rows per mail read, with no pruning.
- Mail read before this change has no snapshot: its header facts exist only
  where `job_seen` kept them (probability 0.2 or more).
- A flag costs one Jev call and one `gmail_get` read of the full message, and
  can raise an alert for a mail the rules would never have shown.
