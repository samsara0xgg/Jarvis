# ADR 0177 — The Jobs page tracks every application, and mail is read back to a fixed day

**Status:** Accepted
**Date:** 2026-10-06
**Supersedes:** the clause of ADR 0171 that all linkedin.com mail is kept out of job mail. Everything else in ADR 0171 and ADR 0155 stands.

## Context

- Allen, 2026-10-06: the Jobs page is to list every job he applied to, one row per
  application with its status, from his Gmail back to 2026-09-10, plus rows he adds by
  hand.
- The poller (ADR 0155) searches `newer_than:{backfill_days}d` with `maxResults` 100 and
  never pages, so only the newest 100 mails were ever scanned; the ledger's oldest row is
  2026-10-01. A fixed day is also not a sliding window: it must not drift with the date.
- The ledger (ADR 0155) is one row per mail and its page groups by company. It has no
  notion of an application, a status, or a row with no mail.
- LinkedIn Easy Apply sends "your application was sent to X" from linkedin.com. ADR 0171
  keeps linkedin.com out of job mail, so these confirmations, the only mail that proves
  an Easy Apply, are held back today.
- Jev's prompts, thresholds and model are not to change (ADR 0158); a mail is typed as
  before and the company and role are read locally.

## Decision

Jarvis computes the applications at read time from the ledger's visible mails and a small
table of Allen's own rows, and reads mail back to `job_mail.backfill_since` by paging the
Gmail search.

Its limits:

- **Backfill.** `job_mail.backfill_since` (a date, or null) replaces `backfill_days` in
  the query as `after:YYYY/MM/DD`; null keeps `newer_than`. Each cycle pages with
  `pageToken` / `nextPageToken`, newest first, until it holds `max_messages_per_cycle`
  unseen ids or the pages end, and reads at most 30 pages.
- **LinkedIn exception.** A linkedin.com mail whose subject reads "your application was
  sent to X" is not excluded (the poller and the repair pass of ADR 0158 treat it as
  normal mail), and its company is X, not "LinkedIn". All other linkedin.com mail stays
  excluded.
- **Grouping.** Visible mails group by company, case-folded: one application per
  company, however many roles its mails name. Its role is the longest role any of them
  read, and the hash of the case-folded company is its id. A mail whose company is only an
  applicant-tracking system's name (the fallback when no employer reads, such as
  "Bamboohr") joins the other company's application with the same role, else it stays its
  own. A role equal to the company, a leading "our " or "the ", and a trailing
  " - Applications" are not part of a role.
- **Status.** Walking the mails oldest to newest from `applied`: receipt gives `applied`,
  interview `interviewing`, offer `offer`, rejection `rejected`; other kinds change
  nothing. An `applied` application whose newest mail is over 21 days old is `no_reply`.
  The statuses are exactly those five.
- **Allen's rows.** The `job_application` table holds `manual` rows (an application with
  no mail) and `edit` rows (overrides of a mail-derived application of the same company: status,
  applied date, note, hidden). An edit's status stands until a mail of kind receipt,
  interview, offer or rejection arrives after he set it. On an edit row `updated_at` is
  when he last set the status, so a note edit does not renew it.
- **Routes.** `GET /inherent/jobs` adds `applications` and keeps everything else.
  `POST /inherent/jobs/applications` adds a manual row; `POST
  /inherent/jobs/applications/{id}` edits one (for a mail-derived id it upserts the edit
  row). A bad status or date is 400, an unknown id 404.

## Alternatives rejected

- **Store applications as rows written by the poller** — a status stored at write time
  goes stale when the 21-day line passes with no mail, and a deleted or re-typed mail
  would need a second code path to repair it; reading from the mails needs none.
- **Keep `backfill_days` and raise it** — it slides with the date, so the 2026-09-10
  floor would be wrong within days; `after:` is fixed. Raising it also did nothing for
  the 100-mail cap, which is what hid mail before 2026-10-01.
- **Let Jev read the company from "sent to X"** — Jev answers choice questions only
  (ADR 0155); the subject has a fixed shape, and a regex reads it with no call.

## Consequences

- While the backfill runs, Jev's daily call cap (`max_calls_per_day`) sets its pace:
  one cycle takes at most 25 mails, and a large backlog can take several days.
- After the backfill, every poll re-lists the pages of mail already seen, newest first,
  before it finds an unseen id: up to 30 cheap searches a cycle. Remembering the oldest
  scanned date would remove that.
- LinkedIn mail scanned before this change was either hidden (rows) or marked seen as
  not job mail; an Easy Apply confirmation among them is not brought back and needs
  Allen's flag.
- Two postings at one company are one application, and a company the local rules read two
  ways (or an ATS name no role ties to an employer) is two; Allen's only repair is to hide
  one.
