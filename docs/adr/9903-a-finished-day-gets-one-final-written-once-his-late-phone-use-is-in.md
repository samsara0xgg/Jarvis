# ADR 9903 — A finished day gets one final, written once his late phone use is in

**Status:** Accepted
**Date:** 2026-10-23
**Supersedes:** none

## Context

- ADR 0302 and ADR 0311 write a final run after the day ends, and another whenever late rows
  change its inputs. His phone and tablet app use reaches Jarvis hours late (ADR 0301), so the
  first final was often written before it, and each late batch paid for a new Opus final.
- A final he reads in the morning and that changes under him later is worse than one that waits.

## Decision

Write one final per finished day, and keep it.

- **Wait.** A final is written once the brief says his phone and tablet use is known past the
  end of the waking window, or twelve hours after that end, whichever comes first.
- **Once.** A day that has a final is not written again unless he asks (理一下 / tidy_day).
- **A refused final** waits until its inputs change before it is asked again, not on every check.

## Alternatives rejected

- **Rewrite on every change** — the cost repeats for each late batch, and the page changes under
  him.
- **Wait for the phone with no cap** — a phone that stays off would leave the day without a
  final.

## Consequences

- A day's final lands later, up to twelve hours after he goes to bed when the phone is slow.
- Late rows after the final do not change it; he asks when he wants it rewritten.
- This replaces the rewrite-on-change part of ADR 0302 and ADR 0311.
