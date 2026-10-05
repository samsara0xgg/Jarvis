# ADR 0162 — Job mail snapshots keep the sender address and body of every scanned mail

**Status:** Accepted
**Date:** 2026-10-04
**Supersedes:** none

## Context

- Allen, 2026-10-04: labelling and replaying the job-mail decisions showed mails
  with "no body" and "sender unknown". ADR 0157 kept a body only for a mail that
  passed the header question, and kept the sender as display name and domain, so
  a mail the header held back (a newsletter, say) could not be read, re-judged
  or labelled by who wrote it.
- A bare "no body" is ambiguous: the body may never have been read, or the read
  may have failed. A labelling set cannot tell the two apart from a NULL.
- The header question to Jev must stay as ADR 0155 and 0157 set it: display
  name, domain and subject, so no address and no body leave the Mac in it.

## Decision

Jarvis keeps the sender address and the plain-text start of the body on every
`job_decision` row of every scanned mail, read from Gmail once per mail, and says
in a `body_status` column whether that body was read.

Its limits:

- A cycle reads each new mail in full once with `gmail_get`, header-dropped mail
  included, within the per-cycle message cap. The typing step reuses that read;
  no mail is read in full twice in a cycle. Only `gmail_search` and `gmail_get`
  are ever called.
- Every row of the mail (header, rule and body stages, error rows too) holds the
  address, the first 3000 characters of the body (HTML stripped as for the typing
  step) and `body_status`: `read`, `unavailable` (the read failed, body NULL) or
  `not_read` (no read was tried, e.g. the header itself could not be read).
- A failed body read is not a failed mail: a mail the header held back stays
  `not_job`, only its body is missing. A mail that needs its body to be typed and
  has none is an `error` as before.
- Egress is unchanged. The header question still carries only display name,
  domain and subject; the body question still carries the first
  `max_body_chars` of the body of a mail that passed the header. The address and
  the extra bodies are written to `memory.db` and nowhere else.
- The address stays out of `job_mail`, `job_seen`, `job_alert`, `job_feedback`,
  `attention_log` and every client route; `GET /inherent/jobs` is unchanged.
- Rows are only appended, never pruned, and erased with `memory.db` by the
  erase-all-data path. Rows written before this change keep their NULL address
  and body; `body_status` is NULL on them, which is also not `read`.

ADR 0157's sentence "a header row never does" (hold the body) and its limit
"sender domain (never the address)" no longer hold for `job_decision`; both still
hold for every other table. Its other limits stand.

## Alternatives rejected

- **Read the body only for mails that reach the body question** — this is ADR
  0157 and leaves exactly the header-dropped mails, the ones a miss hides in,
  without a body.
- **Add the address and body to `job_seen`** — it holds one row per mail and
  overwrites by stage, and the replay data lives in `job_decision`; two copies
  of the address would double the places erase-all must cover.
- **Leave the body NULL and no status** — a reader cannot tell a mail whose read
  failed from one never read, so labelling would silently drop or mislabel the
  failed ones.

## Consequences

- Every new mail costs one more `gmail_get` full read than before when the header
  holds it back, and `memory.db` grows by up to 3000 characters per row, with
  three rows possible per mail and no pruning.
- `memory.db` now holds the address and body start of mail Allen never sent to
  Jev. ADR 0155's "never the body" and 0157's "never the address" hold only
  outside `job_decision`; the spec's retention row says so.
- Mail scanned before this change has no address or body here; filling those in
  is separate work.
