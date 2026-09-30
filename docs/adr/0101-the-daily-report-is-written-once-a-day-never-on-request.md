# ADR 0101 — The daily report is written once a day, never on request

**Status:** Accepted
**Date:** 2026-09-30
**Supersedes:** none

## Context

- ADR 0024 put the daily report behind one flat tool, `daily_work_report`,
  that the decision model calls when Allen asks about a day; ADR 0028 kept
  that entry and made each report read the whole day and check every
  completion claim. A saved report is reused, so only the first request for
  a day pays.
- Measured 2026-09-30 10:18: Allen asked by voice what he did the day
  before; the model called the tool, no report for 2026-09-29 existed, and
  the run took 11 `gpt-6-sol` calls, 174k–193k input tokens on the large
  ones, $0.59, and 59 s during which the voice turn said nothing.
- Allen, the same morning: the model must not write a report whenever it
  likes, and a report should be written once a day on a fixed schedule.
- `get_briefing` already reads a saved report page by page without a model.

## Decision

The daemon writes the report of the day before once a day, at its first
check after `daily_report.at` (local time, shipped as 05:00), and the
conversation only reads saved reports through `get_briefing`; the decision
model is no longer offered `daily_work_report`. One attempt per day per
daemon run: a saved report is reused, and a failed one waits for the next
day or a restart.

## Alternatives rejected

- **Keep the tool, reuse-only** — a day with no saved report would still
  need a path that writes one, and that path is the $0.59, 59 s turn above.
- **Write at night in the night run (ADR 0093)** — the night run only runs
  when Allen starts it; most nights it does not, and the report would not
  exist the next morning.

## Consequences

A day's report exists only from the morning after; asking about today, or
about a day before the schedule existed, gets no report (today stays with
`refresh_work_state`). Every day with any evidence costs one report whether
or not Allen reads it.
