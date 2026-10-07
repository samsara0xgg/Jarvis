# ADR 0180 — The work state is re-analysed in the background when Allen changes what he is doing

**Status:** Accepted
**Date:** 2026-10-06
**Supersedes:** none

## Context

- ADR 0023 made the work-state record on demand: only `refresh_work_state` in a
  conversation or the dashboard's refresh button analyses it. Allen's log on
  2026-10-06 holds 24 `work_state.revised` events in all, the last
  two at 15:31 (conversation) and 18:54 (dashboard), so the Right now page's
  Observed line is hours old unless he presses refresh.
- 2026-10-03/04, Allen, in the proactivity thread: Jarvis should keep a short
  "what you are doing now" note up to date in the background from TimeSink,
  each run continuing from the last one. The agreed shape: refresh on change
  (app or site switched, back at the desk), at most every 5 minutes, at least
  every 30 minutes while he is at the desk, nothing while he is away. On
  2026-10-06 he asked for it to be built.
- Cost, measured on his log: the 24 analyses on `gpt6-luna` cost $0.056 in
  all, about $0.0023 each. At most 12 an hour is a $0.03/h ceiling while he is
  at the desk; nothing runs while he is away.
- ADR 0161's moment already reads presence, the front app and its site from
  TimeSink in one read-only transaction with no model; ADR 0023's fingerprint
  already skips the model when the evidence is unchanged.
- The analysis sends app names, window titles and screen text to the
  configured provider without Allen asking at that moment. Features that send
  private material unasked are off by default (2026-09 privacy pass).

## Decision

When `work_state.auto` is true, the daemon checks every 5 minutes and runs the
one refresh workflow of ADR 0023 with trigger `auto` when the moment reads
`ok`, Allen is present (not idle, locked or asleep) and either the front app
and site differ from those at the last background run or 30 minutes have
passed since it. The default is false; Allen's own settings turn it on. A call
does not stop it: the run is silent. Everything else is ADR 0023's: the same
record, fingerprint reuse, cost accounting as `work_state`, failure keeping the
previous record. Where the moment is unknown (store off or stale, or a brain
under ADR 0170, whose moment has no TimeSink), nothing runs.

## Alternatives rejected

- **A fixed analysis every minute** (Allen's first suggestion, 2026-10-03) —
  840 calls in a 14-hour day, most over the same window, where the model has
  nothing new to say; on change with a 30-minute floor is a few dozen.
- **Trigger on every changed TimeSink head** — the open span's heartbeat
  extends its end every few seconds, so the head changes on every 5-minute
  poll while he works; it is the same 12 calls an hour with no change filter.
- **Run while he is away** — nothing new is observed, and the record's "now"
  would describe a screen nobody is looking at.

## Consequences

A switch between two apps and back inside one 5-minute check is not seen as a
change; the 30-minute floor catches it. The note does not yet reach a
conversation's context: adding it is a prompt change Allen decides separately.
A brain gets no background analysis until its moment reads the terminal's
TimeSink.
