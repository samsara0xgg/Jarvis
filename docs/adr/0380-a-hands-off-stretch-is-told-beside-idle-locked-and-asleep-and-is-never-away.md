# ADR 0380 — A hands-off stretch is told beside idle, locked and asleep and is never away

**Status:** Accepted
**Date:** 2026-10-22
**Supersedes:** none

## Context

- TimeSink stops counting the Mac as in use at idle, lock and sleep. A call or a video keeps him
  present with no input for hours, so no such mark is logged and the doing writer (ADR 0302) sees
  a Mac at work.
- TimeSink now logs two state events for this: `hands_off` at his last input while a call or
  video keeps him present, `hands_on` when input returns while still present. Screen time keeps
  being recorded through it.
- The readers of state events read known kinds only: `timesink_moment.py` decides idle, locked
  or asleep from those kinds and `stop`, `start`, `tracking_*`; a new kind changes none of its
  answers. `query_state` passes events through as they are.

## Decision

Render a hands-off stretch in the Mac line of a segment's signals, and never treat it as away.

- A stretch begins at `hands_off` and ends at the next `hands_on`, or where idle, lock or sleep
  begins, or at TimeSink's own `stop`, `tracking_pause` (and `start`, `tracking_resume`), else it
  runs to the segment's end. It is told as `Mac hands-off 23:10-02:40`, by the pairing, ordering
  and minimum-length rules of the other kinds.
- Where it overlaps an idle, locked or asleep stretch, that stretch names the time: hands-off
  ranks last.
- `timesink_moment.py` is unchanged and does not read the new kinds, so `hands_off` is not away.

## Alternatives rejected

- **Count hands-off as idle in the signals** — a call he sits through is not a Mac he left;
  writing "idle" invites the writer to tell him as away from the Mac. The word differs on
  purpose.
- **Add a `hands_off` / `hands_on` group to `query_state`'s state at start** — the tools and the
  report list events as they are; no reader needs the state carried into a window, and the
  stretch also ends on idle, lock or sleep, which a two-kind group would not know.

## Consequences

- Reports and work-state tools show `hands_off` and `hands_on` as bare kinds in their state
  events; any prompt that explains kinds does not know them yet.
- `plan_facts` takes a Mac wake only from `active`, `unlock`, `wake`; `hands_on` is not one, so a
  return to the keyboard in a call is not a first sign of the day.
