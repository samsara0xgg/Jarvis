# ADR 9902 — A key that serves different things is told span by span

**Status:** Accepted
**Date:** 2026-10-23
**Supersedes:** none

## Context

- ADR 0331 has the writer tell each new key once, a thread or "whatever is around it"; ADR 0330
  then gave such a key to the thread that held most within fifteen minutes of it.
- Keys like WeChat, an untitled Claude window or a browser's start page serve different things
  hour by hour. In the 10-19 replay WeChat was folded into the stretches around it: the fifteen
  minute rule guessed, and one answer per key could not say which conversation served what.
- The writer already words each span, and can read the screen for it (ADR 9901).

## Decision

A key the writer says has no one thread is a key that varies. Every later run shows it under
VARYING KEYS (`v1`, `v2`, ...), and each span the writer writes says, in `serves`, what each such
key served there: a thread and its category, or both null when nothing shows it.

- **Gated with the spans.** Each entry names a label that may be told there, at most once, with
  a thread as in ADR 0331; every key that varies and held him at least a minute inside the span
  must be told. A bad entry refuses the spans and asks again.
- **Summed as told.** When a stretch is read, a key that varies is cut at the spans that told it
  and summed as their threads; outside any, it stays itself. Nothing is guessed from what is
  around it, and the fifteen minute rule is gone.

## Alternatives rejected

- **Keep the fifteen minute rule** — it is a guess the writer does not make.
- **Ask again per key each day** — still one answer for a whole day of a key that changes by the
  hour.

## Consequences

- Each span's answer grows by a line per varying key in it.
- A varying key shows as itself where no span told it: before the first run of the day, and in
  stretches written before this ADR.
- This replaces the "around it" half of ADR 0331 and the fifteen minute rule of ADR 0330.
