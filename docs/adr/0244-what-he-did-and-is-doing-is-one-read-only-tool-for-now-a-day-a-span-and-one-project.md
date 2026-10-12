# ADR 0244 — What he did and is doing is one read-only tool for now, a day, a span and one project

**Status:** Accepted
**Date:** 2026-10-11
**Supersedes:** the clauses of ADR 0023 and ADR 0180 that have the decision model call `refresh_work_state` when asked what he is doing, of ADR 0029 that give the model `query_activity`'s table, and of ADR 0101 that give it `get_briefing`. Everything else in those ADRs stands.

## Context

- The model had four tools for "what did I do": `get_briefing` (a saved daily report, in chunks
  with a cursor), `query_activity` (a table of TimeSink rows, 48,000 characters a page, cursor),
  `read_activity` (one row by a reference of the form `s<id>:<rev>`) and `refresh_work_state` (an
  L1 write that runs a paid analyst over the day's evidence and saves a new work state, ADR 0023).
- Cursors and references are strings the model must copy exactly. On the Pi brain, 2026-10-11,
  27 cursor errors were counted across `get_briefing`, `recall`, `read_records` and
  `search_records` (ADR 0243).
- "What am I doing now" and "what did I do today" called the analyst: seconds of wait and a model
  call each time. Nothing re-analyses in the background on a brain (ADR 0180: its moment has no
  TimeSink) and `work_state.auto` is off by default, so the saved state is as old as the last
  explicit refresh.
- Every turn's prompt already carries the ledger (ADR 0201): per day the union of TimeSink spans
  as active time, top projects and apps, calls, commits the Event Log saw, the phone's named
  stays. On a brain the ledger reads TimeSink through the terminal, never raises, and counts the
  asks that came back empty, so a caller knows a part was built without it.
- A brain has no TimeSink file; the spans it gets carry app, project and session document, not
  window titles.
- A saved daily report exists for past days only (ADR 0101), with its `## Summary` section first.

## Decision

What Allen did or is doing is one read-only built-in tool, `my_day`, on the menu from the first
request, computed from the ledger's own figures and the saved records, never by a model:
`my_day()` is today so far plus what he is doing now, taken from the latest saved work state with
its age and a warning when it is over three hours old; `date` is a past day's saved report summary
when one exists, else that day's computer time, top projects and apps, commits, calls and places;
`date` with `to` (at most 31 days) is each day's row and the span's totals; `about` narrows any of
these to one project or app, with the time inside it by window and `page` (8 rows) for the rest.
A result leads with a sentence he could say aloud and is at most 4000 characters. When the
terminal is not connected, or the machine has no TimeSink, the result names the parts it could
not read and still gives commits, places and the work state the brain holds. `get_briefing`,
`query_activity`, `read_activity` and `refresh_work_state` leave the menu and stay registered,
deferred.

## Alternatives rejected

- **`my_day()` runs `refresh_work_state` first.** It makes the cheapest and most frequent question
  ("what did I do today") a paid model call plus seconds of silence; the ledger answers it from
  data in milliseconds, and the analyst's claim is still shown, labelled with its age.
- **Keep the four tools and shorten their descriptions.** Every cursor error counted in ADR 0243
  happened with the cursor rules already in the descriptions; `query_activity` alone pages 48,000
  characters of rows no voice answer needs.
- **A new reader of TimeSink inside the tool.** It would count active time a second way, and a
  brain has no TimeSink file; the ledger already merges the spans, reads through the terminal and
  counts what it could not read, and the prompt and the tool then give the same numbers.
- **A model call that writes a summary of a span on demand.** It adds seconds and cost to a read
  that the saved reports (ADR 0101) and the figures already answer for every day.
- **`refresh_work_state` stays on the menu beside `my_day`.** Two entries for "what is he doing"
  is the choice that went wrong with six conversation tools (ADR 0243); the Dashboard's refresh
  route and the background re-analysis (ADR 0180) call the one refresh workflow themselves.

## Consequences

- On a brain with `work_state.auto` off, what he is doing now can be days old; `my_day` says how
  old and that it may not describe the present, and the model has no way to ask for a fresh
  analysis. The analysis is renewed from the Dashboard or by turning `work_state.auto` on.
- A saved report is read as its summary section, cut at 2400 characters; the rest of the text is
  not reachable by voice (`get_briefing` paged it). A row of `read_activity` is not reachable
  either: `about` gives the time inside a project by app and window, not one span's detail.
- On a brain `about` matches the project, app and session document, not window titles.
- Commits are the ones the Event Log saw: those made while no terminal observed the repository
  are not in a day's figures.
- The four old tools stay registered because tests dispatch them by name; a model that names one
  to `tool_search` gets ADR 0242's
  `Tool.supersedes` note pointing at the `my_day` call. The `daily_work_report` tool's
  description, which is the skill's, now sends "what am I doing" and saved reports to `my_day`.
