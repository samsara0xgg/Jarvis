# ADR 0213 — The job hunt block counts submitted applications from mail, the screen and his word

**Status:** Accepted
**Date:** 2026-10-10
**Supersedes:** none

## Context

- Block D of the prompt ledger (ADR 0201) counted companies that sent job mail. Allen applies
  through many routes, and a portal often sends no confirmation, so "how many applications have I
  sent" was answered from the mail alone and came out low.
- Two other records of a submission exist on the Mac: TimeSink's `span` table (the page title and
  URL of the browser tab, which the ledger already reads) and Allen's own words. A success page
  shows in a span; "I applied to X" can be stored as a `job_application` row, the table the Jobs
  page keeps (ADR 0177).
- Each source also repeats itself. A company can send the same receipt twice; a success page stays
  open in a tab for hours; a page and its receipt are one submission; he may tell Jarvis about an
  application whose receipt arrives later. A count that adds the sources is wrong unless these
  collapse.
- The count is arithmetic over rows (ADR 0201), so it states its basis, and it must say what it
  cannot see, so that the model does not read a low number as a fact.

## Decision

Block D states a count of applications submitted, "at least N", as the sum of three sources, each
submission counted once, with the basis of every part and a note that a portal application with
no mail and no success page seen is not visible unless he tells Jarvis.

- **Mail.** Each `receipt` row not deleted is one submission. A receipt within half an hour of a kept one of the
  same company is the same submission when exactly one of the two came through an
  applicant-tracking system (Semios and Workable on 2026-10-09, Zaber and Workable on 09-26);
  two from the same sender stay two (Apera's two roles 8 minutes apart, CGI's two postings), and
  the same company 70 minutes apart stays two (Moment Energy).
- **Screen.** Computed on read from `span`, with no table. A span is a success page when its
  domain is not a webmail domain and its title reads as a submission ("thank you for applying",
  "application submitted") or its URL ends in a success segment (`/confirmation`,
  `/apply/success`, `applythankyou`, `/thanks`). A posting is its domain, its path without that
  segment, and the values of query parameters whose name contains "job"; a posting counts once, at
  its first sighting, so a tab left open does not count again. Company and role come from the
  URL's `company` and `jobTitle`, else from a Greenhouse title ("Job Application for R at C"),
  else from the host label and the last real page title of the two hours before. Spans are read
  120 days back.
- **Said.** `record_application` (a conversation tool next to `job_ledger`, present only while job
  mail is on) writes a `job_application` row with `source='said'`; `manual` rows are what he
  added on the Jobs page. Both are listed on the Jobs page and count as "you told me or added".
  Hidden rows do not count.
- **One submission across sources.** A success page and a receipt are the same submission when the
  receipt came within 30 minutes of the page and the companies match or the roles are equal. Two
  companies match when their letters and digits, case-folded, are equal or one holds the first four
  letters of the other ("aperaaiinc" and "Apera Ai"). Each receipt matches one page. A said or
  manual row is not counted again when its company matches a receipt or page and its date is
  within one day of it.
- Days are cut at the ledger's local midnight; the line also gives the last seven days and names
  the screen-only and told submissions.

## Alternatives rejected

- **A `job_application` row written for every page seen.** The Jobs page would list unreviewed
  guesses next to his rows, and a wrong guess would need a delete path; computing on read leaves
  no rows to be wrong.
- **Counting only mail receipts, as before.** A portal that sends no mail is invisible to it by
  construction, so the count can only miss those applications.
- **Having the model add the sources up from `job_ledger` and its memory.** Block D exists so a
  count is arithmetic over rows (ADR 0201); a model adding three lists and spotting duplicates
  across them is the error it was built to remove.

## Consequences

- Block D now opens TimeSink's database each turn (a narrowed query, 120 days back) where it read
  only memory.db; a missing or unreadable TimeSink leaves the screen part at zero.
- The count is still a lower bound. The UVic co-op portal is a single-page app whose URL and title
  do not change on submit, so a submission there is invisible to the screen source; it counts only
  as a receipt or if he tells Jarvis. It is not covered yet.
- Pattern matching on titles and URLs can miss a portal's wording, and an embed URL with no
  job-named parameter and a shared path merges two postings into one.
- A told application whose company is spelled differently from its later receipt is counted twice.
