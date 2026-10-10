# ADR 0204 — A pin holds up to three trips and rings for the earliest catchable

**Status:** Accepted
**Date:** 2026-10-09
**Supersedes:** none

## Context

- 2026-10-09, Allen: a `transit` answer can list several lines (the absurd ones are already
  dropped, ADR 0203) and he wants to choose which to pin; today each pin replaces the last (ADR
  0200), so he can keep only one of two buses a few minutes apart.
- One reminder per pin is what the ring and the countdown share (ADR 0200, 0179); three rings for
  three trips would speak over each other.
- Old `departure.pinned` events are in his log with one trip's fields at the top and must still
  fold.

## Decision

A pin is a set of at most three trips, each with its own id, route, stop, times and live status.
`departure.pinned` starts a set (`trips` list); `departure.trip_added`, `departure.trip_removed`
and `departure.updated` (per trip) change it and `departure.unpinned` ends all of it. The served
`departure` keeps its top-level fields for the current trip, the earliest whose bus has not left
(leave time plus the walk), and adds `trips` (all still catchable, each with delay and staleness)
and `more`; a missed trip drops off. One reminder rings at the current trip's leave time naming it
and the others, and is cancelled and scheduled again whenever the set or a time changes or the
current trip is missed. `POST /inherent/departure` gains `add` (adds the row to the set the same
offer started, else starts a new set), `remove` (one trip) and keeps `pin`, `next` (the current
trip) and `undo` (restores what the last removal took: one trip, or the whole set);
`pin_departure` takes an `options` list. Every trip is refreshed from BC Transit's feed on its own.

## Alternatives rejected

- **One reminder per trip** — three trips a few minutes apart would ring within one another's
  speech; the spoken line names the alternatives instead.
- **Replace the pin with a list in a pins table** — the log already folds the single pin; the same
  fold with per-trip events keeps ADR 0200's restart and sleep behaviour with no new storage.
- **Rewrite old `departure.pinned` events** — the log is append-only; the fold reading a payload
  without `trips` as a set of one costs three lines.

## Consequences

- Each trip costs its own feed download per refresh (three per 30 s at most), as the feed is parsed
  per lookup.
- The ring is recorded in each event as the trip it is for, so a ring that already rang for a trip
  is not rung twice when the set changes.
- A fourth trip is refused (`full`); the offer card never has more than three rows, so only an undo
  or a voice pin can meet the limit.
