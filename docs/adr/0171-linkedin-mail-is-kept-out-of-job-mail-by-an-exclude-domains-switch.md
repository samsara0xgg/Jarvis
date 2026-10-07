# ADR 0171 — LinkedIn mail is kept out of job mail by an exclude_domains switch

**Status:** Accepted
**Date:** 2026-10-06
**Supersedes:** the clause of ADR 0158 that interviews and offers from LinkedIn senders are not downgraded, while `linkedin.com` is on `exclude_domains`. Everything else in ADR 0158 stands.

## Context

- Allen, 2026-10-06: all LinkedIn mail is to be filtered out of the job-mail feature
  (ledger, Jobs page, alerts and speech), as a config switch, without deleting data.
- ADR 0158 keeps LinkedIn digests and social news off the cards by sender and subject
  rules, and states that interviews and offers from LinkedIn senders are not
  downgraded. Recruiter InMail arrives from a linkedin.com address, so a sender
  rule on the domain contradicts that clause.
- Jev's prompts, thresholds and model are not to change (ADR 0158), and every scanned
  mail's body is kept locally (ADR 0162).

## Decision

Jarvis keeps mail from the domains in `job_mail.exclude_domains` out of job mail by a
local rule, and ships the list as `[linkedin.com]`.

Its limits:

- **Match.** A sender domain equals an entry or ends with "." and the entry.
- **New mail.** A matching header is held back before Jev is asked anything (no
  header or body call), as `not_job` by the judge `local-rule/exclude-v1`. Its body
  is still read and kept in the decision snapshot, like any scanned mail. It shows in
  the held-back list and Allen's flag still brings it back, as with social mail.
- **Existing rows.** The startup repair pass of ADR 0158 hides a visible, unflagged
  ledger row whose domain matches, and ends its pending alerts, exactly as it does
  social rows. Rows are hidden, never removed.
- **Page.** `GET /inherent/jobs` returns the list as a rule when it is not empty, and
  the ledger page shows one line for it.
- **Recruiter InMail** through linkedin.com is excluded too, while it is on the list.

## Alternatives rejected

- **Extend the social rule to every LinkedIn subject** — the social rule is a subject
  pattern list and would still let a new LinkedIn wording through; a domain match
  catches all of it from one line of config.
- **Delete the stored LinkedIn rows** — Allen asked for no data loss, and a hidden row
  can be flagged back with one request.

## Consequences

- A real recruiter's InMail through LinkedIn no longer alerts; Allen must see it
  in the held-back list or on LinkedIn itself.
- Setting the list to `[]` stops excluding new mail, but rows already hidden stay
  hidden until Allen flags them.
