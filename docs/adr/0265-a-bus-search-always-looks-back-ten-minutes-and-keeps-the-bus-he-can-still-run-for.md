# ADR 0265 — A bus search always looks back ten minutes and keeps the bus he can still run for

**Status:** Accepted
**Date:** 2026-10-14
**Supersedes:** the clause of ADR 0221 that pays the ten-minute look-back only when a route is named

## Context

- 2026-10-14 09:29, Allen asked for a bus with no route and no time. Google plans from now plus
  its own walking estimate, so the 09:31 bus dropped out; the answer held only the 09:54 and
  called it relaxed. After "you missed a bus" the same call returned the same answer.
- ADR 0221 rejected shifting every "now" request back ten minutes because it returns buses he
  cannot reach at a normal pace. Those buses carry their true `spare_min`, so the model can judge
  them; what was missing was a mark and a cap on how many of them reach the list.
- The routes request is paid per call. A search makes one TRANSIT request plus one DRIVE and one
  WALK (ADR 0205): 3 calls before and after; a named route Google lacks makes a fourth (ADR 0221),
  unchanged.
- The model lists at most three options, and a pin takes at most three (ADR 0200).

## Decision

Every bus search without `at` asks Google from ten minutes before now, sorts the options by
their first bus, and gives each a `verdict` (`easy`, `tight`, `missed`, from `spare_min` as ADR
0221 defined them). Of the buses he has missed by a normal walk, only the latest one that is
within five minutes of that walk stays, and it does not set the yardstick for the far-fetched
cut; the rest, and anything further behind, are dropped. A named route behaves as before.

## Alternatives rejected

- **Keep every bus the look-back returns** — Google returns every bus whose leave time is up to
  ten minutes gone, so a list capped at three can be filled by buses that left; with the cap, the
  catchable ones would be the ones cut.
- **A second request one minute before now** — it costs a fourth call on every search and
  returns the same 09:31 the single look-back already returns.
- **Fix it in the prompt ("also look for buses that just left")** — the tool could not return a
  bus Google had dropped; the 09:31 was absent from the result, not misread.

## Consequences

- A result can list a bus he can only catch by running; the card still offers only the catchable
  ones (`spare_min` above -2), and pinning a missed one is refused as its leave time is past.
- The five-minute limit is a heuristic: it is the most a run is assumed to make up on the walk.
- A search whose look-back finds no route at all is `not_found`; the plain "now" request is
  made again only for a named route, as before.
