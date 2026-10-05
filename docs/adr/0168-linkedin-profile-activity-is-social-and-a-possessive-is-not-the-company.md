# ADR 0168 — LinkedIn profile activity is social mail, and a possessive is not part of the company

**Status:** Accepted
**Date:** 2026-10-05
**Supersedes:** none

## Context

- Allen, 2026-10-05: two live misreads. LinkedIn's "You appeared in 5 searches"
  reached the ledger as job mail; ADR 0158's social rule lists "recently posted"
  and "hired near you" but not a notice about the profile's own activity.
- A real rejection from "Maren Ingle", subject "Cambio Earth - Update", body
  "Thank you for applying to Cambio Earth's QA & Test Automation Developer Co-op
  position", was stored with company "Cambio Earth's QA" and no role. The company
  pattern of ADR 0158 takes up to four capitalised words after "to" and keeps the
  possessive and the role word; no role pattern reads "applying to X's ROLE
  position".
- Jev's prompts, thresholds and model are not to change (ADR 0158); both fixes are
  local rules over text already read or stored.

## Decision

Jarvis routes LinkedIn profile-activity subjects to the social rule, and reads the
company and role of "applying to X's ROLE position" by local rules, and repairs
the rows already stored.

Its limits:

- **Routing.** On a LinkedIn sender, the subjects "appeared in N searches" (or
  search appearances), "profile was viewed", "N new profile views" and "viewed
  your profile" are social: held back at the header step, body not read, shown in
  the held-back list, never in the ledger. Job-alert digests and interview or
  offer InMail do not match and are unchanged.
- **Company.** A capitalised name read after "at, from, with, to, join" loses a
  trailing possessive and the role or department words after it ("Cambio Earth's
  QA" is "Cambio Earth"). A possessive followed by anything else, or none, stays.
- **Role.** "applying to/for [the] [Company's] ROLE position|role|opening|job" and a
  capitalised "the ROLE position|role" read the role, with `&` and capitals kept.
  Existing patterns and the generic-word stoplist stand.
- **Repair.** The startup pass of ADR 0158 also reads the body start kept in
  `job_decision` (ADR 0162) for company and role, and hides a stored LinkedIn
  profile-activity row exactly as it hides social rows. It is idempotent, never
  reads Gmail, and never touches a mail Allen flagged.

## Alternatives rejected

- **Ask Jev about profile notices** — prompts and thresholds are fixed, and a sender
  rule is visible from one subject line.
- **Drop every word after a possessive** — it would shorten real names such as
  "Lowe's Burgers"; only role and department words are cut.
- **Re-read the bodies from Gmail to repair** — the repair is offline by ADR 0158;
  the stored body start suffices where it exists.

## Consequences

- LinkedIn wording changes will still let new profile notices through until the
  pattern list is extended.
- A role or department word that is missing from the list leaves a possessive
  company ("Foo's Marketing Ops") until it is added.
- Rows older than ADR 0162 have no stored body, so their company and role are
  repaired from subject and sender alone, as before.
