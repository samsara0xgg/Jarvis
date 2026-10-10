# ADR 0200 — A pinned bus trip is event-log state, refreshed from BC Transit's live feed

**Status:** Accepted
**Date:** 2026-10-09
**Supersedes:** none

## Context

- 2026-10-09, Allen: after `transit` answers (ADR 0189), he wants to say "pin it" and have the
  trip sit on the notch as a countdown to when he must leave, and be told when that time comes.
- A transit option already carries `leave_at` (first bus minus his walk to the stop), so the
  countdown needs no new computation; the ring is a reminder at that time (ADR 0179).
- Buses run late, and a countdown to a bus that has already moved is worse than none. Google
  Routes is a planner, and whether it reflects delays for BC Transit is unverified. BC Transit's
  own GTFS-realtime `TripUpdates` (no key) names every trip and stop with a delay, refreshed about
  every 30 s, but spells stop names differently from Google ("Uvic Exchange Bay M" against
  "UVic Exch Bay M") and gives trip and stop ids that only the static GTFS export (about 16 MB)
  resolves to a route number and a position.
- The wing right of the camera holds four groups in the 64 pt 点线环 look, and about 87.5 pt
  remain beside the notch while the Dashboard is open (ADR 0187).

## Decision

A pinned trip is the newest `departure.pinned` event, written by the model-only quiet tool
`pin_departure` when Allen asks and never otherwise, moved by `departure.updated` and ended by
`departure.unpinned`; pinning also schedules an ordinary reminder at `leave_at`, and a newer pin,
an unpin, or a moved `leave_at` cancels and replaces it. While its bus has not left, the
reminder tick (every 15 s) asks BC Transit's live feed every 30 s when that bus really leaves,
finding the stop by position (the stop Google gave, within 40 m) and the trip by route number and
the timetable time closest to the pinned one (within 10 minutes), and shifts `leave_at` and
`arrive_at` by the difference. No match, no stop position or a feed that does not answer keeps
the times and serves the pin `stale: true`; a failed refresh never drops it. `GET
/inherent/notices` carries the pin as `departure` until its `departs` has passed, and the notch's
✕ is `POST /inherent/notices/{id}` with `dismissed`. The notch draws it as a pill after the marks
that counts down from the absolute times and the local clock, amber from 5 minutes.

## Alternatives rejected

- **Re-run the Google Routes request every refresh** — a paid call per round (about 120 an hour
  for a pin held that long) for a planner whose live-delay coverage of BC Transit is unverified;
  BC Transit's own feed is free and carries the delay field.
- **Match the stop by name** — the two feeds already disagree on the first stop probed ("Uvic
  Exchange Bay M" against "UVic Exch Bay M"); position within 40 m does not depend on spelling.
- **A fifth wing group for the pin** — five groups need about 96 pt, more than the 87.5 pt left
  with the Dashboard open, so the whole wing would fold (ADR 0187). The pill instead steps aside
  alone when marks plus pill do not fit, and the marks stay.
- **A pins table, or a timer in the daemon** — the log is already the brain's state and reminders
  already survive a restart or a sleep by folding it (ADR 0170, ADR 0179).

## Consequences

- The static export is kept under `cache/` in the runtime root and fetched at most once a day;
  the daemon holds two lookups built from it (trip to route number, stop to position), a few MB.
- Only a trip looked up since the daemon last started has its stop's position (it is kept from the
  `transit` answer); a pin made from an older answer is served `stale` for as long as it lasts.
- Refresh stops at `departs`, which is also when the walk to the stop has run out; a bus that
  leaves early is still served until its pinned minute.
- With the Dashboard open the pin shows only when the marks leave room for it; beside four marks
  it is hidden, not squeezed.
- If the feed has no delay for a trip it reports the timetable, so the pin looks live and is not.
