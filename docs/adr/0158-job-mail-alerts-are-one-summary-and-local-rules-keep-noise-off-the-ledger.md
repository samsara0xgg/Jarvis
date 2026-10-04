# ADR 0158 — Job mail alerts are one summary, and local rules keep noise off the ledger

**Status:** Accepted
**Date:** 2026-10-04
**Supersedes:** none

## Context

- Allen, 2026-10-04: the first live backfill left 15 `job_mail` rows and 10
  pending `job_alert` rows (7 `card_sound`, 3 `speak`). At quiet `off` that is
  ten cards and up to three spoken lines, because the digest of ADR 0155 only
  forms from alerts that waited, and a burst made together is not "waited". He
  does not want a burst to be a stream of cards or speech.
- Of those 15 rows, eight are not what the ledger is for: LinkedIn posting
  digests ("X at Y", "Y is hiring"), LinkedIn social news ("X recently posted",
  "X hired near you") and one account notice ("CGI - User Information"). Jev's
  header question lets them through to the body read and types them as
  `job_other`, which the rule table of ADR 0155 turns into a card with sound.
  Its prompts, thresholds and model are not to change.
- The regex extraction of ADR 0155 stores a person's name as the company
  ("Jill Crowe", a recruiter writing from `app.bamboohr.com` and
  `reliablecontrols.com`) and the generic word "job" as a role (from "Job
  Application Acknowledgement"), and reads a whole subject as a role after
  "Re:". The same mail from the same company is split over several groups.
- The stored rows hold the sender name, domain and subject, so company and
  role can be recomputed with no Gmail read. The body is not stored.

## Decision

Jarvis shows everything pending as one summary card that never speaks, decides
by local rules over sender and subject what is job mail at all, and repairs
existing rows from what they already store.

Its limits:

- **Summary.** Mail alerts are one notice when two or more waited more than 30
  seconds, or when any three were made within 90 seconds: "N job emails in the
  last two days, X about interviews" (offers and interviews), at most
  `card_sound`, never `speak`, with a link that opens the Dashboard's job ledger.
  Rows already in the database are merged when read; nothing is migrated or
  deleted. A third alert within 90 seconds is made at `card_sound`, so only the
  first two of a burst can be spoken, and a single live interview or offer still
  speaks at quiet `off`. The channel-health alert stays its own card and is not
  counted.
- **Company and role.** A sender display name that reads as a person (two or
  three capitalised words, no organisation word) yields to the organisation of
  the sender domain; when the sender domain is an applicant-tracking system the
  company is read from the subject first. A role is read from subject or body
  patterns and is empty when none matches; a stoplist of generic words is never
  stored. A repair pass at job-mail start recomputes both for every row from the
  stored name, domain and subject; a stored role that appears in the subject but
  that the rules no longer read from it is cleared, a role that is not in the
  subject (read from the body) is kept unless generic.
- **Routing**, by local rules over the sender domain, display name and subject,
  applied after Jev's typing and never changing what Jev is asked:
  - LinkedIn social activity is held back as `not_job` at the header step, so
    its body is not even read; it shows in the held-back list, not the ledger.
    A repair pass hides such rows already in the ledger (`deleted = 1`), and
    writes a `job_seen` verdict `not_job` and a `job_decision` row (stage
    `rule`) so the audit list shows them. Allen's flag still wins and brings a
    mail back.
  - An account or system notice from a job site, which Jev typed `job_other`,
    is kind `other`: in the ledger, never an alert.
  - Allen's rule (2026-10-04): a LinkedIn job-alert digest never raises a card,
    a sound or speech. It is `job_other` as before and ledger only while
    `job_mail.linkedin_alerts` is `ledger_only`, the default and the standing
    rule; `card_sound` is a switch for a later decision of his, not a mode in
    use. The setting is part of the context pack's situation, so the logged pack
    says which was in force, and `GET /inherent/jobs` returns it as `rules`, which
    the ledger page shows as one line. Interviews and offers from LinkedIn
    senders are not downgraded.
- **Pending alerts** of a mail the repair pass turns ledger-only, `other` or
  `not_job` are marked done (not deleted), so they never join a summary.

## Alternatives rejected

- **Raise the digest threshold only** — the digest already forms at two held
  alerts and still left the burst as ten cards at the moment the level dropped
  and as five cards before any alert had waited 30 seconds (the existing
  integration test showed five separate notices at `quiet`).
- **Let the judge see the other pending alerts** — the judge is a function of
  one pack so that `replay` reproduces it; a pack that includes the queue
  stops being a fact about one mail and cannot be replayed from the log.
- **Change Jev's prompts or thresholds for LinkedIn mail** — the header answer
  of 2026-10-04 is logged per mail in `job_decision`; changing wording or bars
  for eight senders would shift the verdicts of every other mail in the same
  version, and a sender rule is visible and refutable from one subject line.
- **Recompute roles from the stored subject only** — it erases roles that came
  from the body (a body-derived "Firmware QA Analyst Co-op" has no trace in its
  subject), which no later pass can restore because bodies are never stored.
- **Delete the noise rows** — `deleted` is Allen's own action and a delete
  loses the snapshot; hiding with a `not_job` verdict keeps the mail auditable
  and reversible by one flag.

## Consequences

- A burst of interviews or offers is not spoken after the second one, and a
  second-later offer arrives as a summary, not as a card of its own.
- A person's name that happens to look like an organisation (two capitalised
  words from a free-mail domain) is still read as the company, and company names
  read from a domain are guesses: the ledger can still group wrongly.
- The LinkedIn rules are a list of subject patterns; LinkedIn wording changes
  will let new social mail through to the ledger until the list is extended.
- Alert rules in ADR 0155 and the spec change in three rows: the digest forms
  from three alerts as well as from two that waited and carries the interview
  count, a third alert in 90 seconds does not speak, and LinkedIn alert digests
  and account notices no longer raise alerts.
