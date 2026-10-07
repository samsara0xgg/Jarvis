# ADR 0182 — An application is a card with its timeline, interview details and links, read from kept mail

**Status:** Accepted
**Date:** 2026-10-06
**Supersedes:** none

## Context

- Allen, 2026-10-06, approved a text wireframe: the Jobs page of ADR 0177 is a 7-column table, and the
  Dashboard panel is 360 px wide, so a row is cramped and shows nothing about what happens next
  (when the interview is, where, who, where to check the application).
- ADR 0177 computes applications at read time from the ledger's mails, and Jev's prompts, thresholds
  and model are not to change (ADR 0158). ADR 0162 already keeps the first 3000 characters of every
  scanned body in `job_decision`, locally, so a body start can be read again with no Gmail call.
- A guessed interviewer or a wrong join link sends Allen to the wrong person or place, so a field the
  mail does not state outright must stay empty rather than be inferred.
- The L2 ledger may not import L3 (`lint-imports`); the readers of a body are L3 and the join of
  mails into applications is L2.

## Decision

Show each application as a card, and fill its timeline, interview details and links at read time
from the ledger's mails and their kept body starts, with no new Gmail read.

Its limits:

- **Card.** Collapsed: the company, a status pill that is also the status picker (the hand-set
  mark stays), and a second line with the role, the day applied and what matters for the status
  (the next interview; "no reply for N days"; the rejection day). Expanded: progress, interview,
  links, mails with a Gmail button each, the note and removal. Interviewing is blue, offer green,
  rejected and no reply grey, applied neutral. The order of ADR 0177 is unchanged.
- **Timeline.** The day applied, then each interview invitation, offer and rejection by the mail's
  date, then the interview itself: the next event ahead (hollow, with the days left; dropped once
  the application is rejected), else the newest past event time an interview mail read. A row
  Allen added by hand has only the day applied.
- **Interview details.** Read from the newest interview mail whose body start says anything; the
  fields of two mails are never mixed. Online is a join link on zoom.us, teams.microsoft.com,
  teams.live.com, meet.google.com or webex.com, a platform named, or virtual, video or online on a
  sentence about an interview; onsite is in person, on-site, an office, or a street address under a
  Location label or in such a sentence. A mail that says both says neither, unless it holds a join
  link. Interviewers are only names after `Interviewer(s):`, `Panel:` or `with` on an interview
  sentence, each passing the person test ADR 0155 uses for sender names; anything else is left empty.
- **Links.** The first https address on an applicant-tracking domain whose path reads as sign-in,
  status or candidate home is the portal; the first other https address whose path names a job,
  career, posting or requisition is the posting. http is ignored.
- **Gmail.** Each mail carries its thread id; the desktop opens Gmail on the thread (else the
  message) through the existing `open-mail` bridge, and every other address through `open-url`.
- **Seam.** The ledger takes the body reader as an argument (as it does `is_ats`), so L2 never
  imports L3; the runtime passes `jarvis.decision.job_mail.mail_details`.

## Alternatives rejected

- **Store the details when a mail is typed** — a rule change (a new platform, a better name test)
  would need a backfill pass, and the read-time derivation of ADR 0177 re-reads 3000-character
  bodies for 300 mails in 0.16 s; nothing stored can go stale.
- **Ask Jev for the interviewers and the mode** — Jev answers choice questions only (ADR 0155), and a
  generated name can be wrong with no way to tell; a name that fails the local person test is
  exactly what Allen asked not to see.
- **Draw the timeline vertically** — it fits any step count, but the approved wireframe is a single
  horizontal line, and an application with four invitations (six steps) still wraps readably in
  360 px with a growing connector on each step.
- **Build a Gmail URL in the renderer** — the bridge accepts a message id and builds the URL itself
  so the renderer cannot open an arbitrary address through it.

## Consequences

- Every `GET /inherent/jobs` re-reads the kept body of every visible mail with a handful of regexes.
- A mail from before ADR 0162 has no kept body, so its application shows no interview detail or link
  from it.
- A link in a marketing footer whose path says "jobs" can be shown as the posting; Allen only sees a
  button that opens it, never anything sent.
- A mail that reschedules an interview with no platform line hides the earlier mail's platform until
  its own detail is read: details are taken from one mail, not merged.
