# ADR 0367 — The job hunt is one list, and one tool on the menu reads, adds and updates it in his local time, as of the last mail read

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** in ADR 0155, the conversation reading the job ledger's per-company mail groups (`job_ledger`); in ADR 0213, `record_application` as a tool of its own, and block D's company lists being built from job mail apart from the applications

## Context

- `job_ledger` reads the per-company mail groups (`list_ledger`), not the applications the Jobs
  page shows (`list_applications`, ADR 0177). So it:
  - misses his edits, his hidden rows and the rows `record_application` wrote;
  - lists an applicant-tracking system's name as a company;
  - still claims, in its description, to be the Jobs page.
- `record_application` waits behind tool search (ADR 0247).
  - When he said 「我投了 X」, the fact was kept as a memory, and the Jobs list got no row.
  - It never checks for an existing row, and its date is the model's arithmetic.
- `job_ledger`'s rows have gaps:
  - times are UTC ISO strings;
  - at most 15 rows, with no filter and no "as of";
  - a dead Gmail token leaves old rows that look current;
  - a Mac that does not answer turns his time on job sites into 0 s.
- Block D, the state block's job hunt, builds its company lists from job mail by company
  (`_job_hunt`), apart from the statuses the Jobs page shows. Its count of submitted applications
  is ADR 0213's.
- The job tools exist only while job mail is on (ADR 0185).
- On 2026-10-21 Allen approved (「就按你说的去吧」):
  - one jobs tool on the menu (list, find, add, update);
  - it reads the Jobs page's list;
  - same company and role kept once;
  - local times, and "as of".

## Decision

Give the job hunt one tool on the menu, `jobs`, over the one list the Jobs page shows, and build
the state block's lists from that list too.

- **One list.**
  - `jobs` lists, finds, adds and updates the applications as the Jobs page shows them (ADR 0177):
    his edits and hidden rows applied, and each tracking system mapped to its company.
  - Its count of submitted applications is ADR 0213's, the number block D states.
  - Block D's lists of confirmed, rejected, interviewing and offered applications come from the
    same applications' statuses.
- **Add once.**
  - An application he tells is added unless one with the same company (compared as ADR 0213
    compares companies) and the same role, or no role, is already there. Then the result names
    the existing one.
  - The date is taken in his words and resolved by code (ADR 0356's day resolver).
- **Update.** He can set an application's status, date or note, or hide it. The change is the
  Jobs page's own edit (ADR 0177), with an undo.
- **His time, and how fresh.**
  - Every time is local, with its weekday.
  - The result says when job mail was last read, and says so when reading has failed since.
  - Time on a company's site is a number only when the Mac answered. Otherwise it is unknown.
- `job_ledger` and `record_application` go.

## Alternatives rejected

- **Keep `job_ledger` and fix its rows.** The page and the tool would still read two different
  groupings, and his edits live only in the page's (ADR 0177's edit rows).
- **Keep `record_application` behind tool search.** It was not found when he said 「我投了 X」, as
  ADR 0247 itself predicted.
- **Keep the applications he tells as memories.** Neither block D's count nor the Jobs page reads
  memories, so the application would exist nowhere he looks.

## Consequences

- `jobs` is on every turn's menu while job mail is on, at a few hundred tokens.
- Block D's lists now follow his edits on the Jobs page, as well as new mail.
- Time on job sites stays unknown whenever the Mac is away.
