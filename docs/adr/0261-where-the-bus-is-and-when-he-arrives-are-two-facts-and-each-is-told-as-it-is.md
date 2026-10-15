# ADR 0261 — Where the bus is and when he arrives are two facts, and each is told as it is

**Status:** Accepted
**Date:** 2026-10-14
**Supersedes:** The clauses of ADR 0224 that give the ride one `stale` flag for "no live bus" and "the arrival is a timetable time", that gate the one-stop warning and the `passed_stop` ending on it, and that say a failed feed leaves the ride on the timetable "and says so" in one word. Everything else in it stands.

## Context

- Live evidence, 2026-10-14 09:30-09:42 PT, route 28 followed (ADR 0224, 0246). Across consecutive
  turns she said "times are from BC Transit's live bus position", then "no live bus position now,
  timetable times", then corrected herself back and forth; once she said the tool result "marked
  both live and stale". The next stop advanced in real time (Kenmore, Torquay, McKenzie,
  Mortimer), so the vehicle position was live throughout.
- Cause: `Progress.stale` meant two things. It was set when no live vehicle matched the trip, and
  also when his stop was missing from the TripUpdates predictions (`alight_stop_id not in
  _preds`). BC Transit's TripUpdates often lack the stop he gets off at, so a ride with a live
  position read as stale, and the state-block line and the `bus` result then said "no live bus
  position", which was false. `GET /inherent/ride` also sent `live` (the feed is up) beside
  `stale`, so a reader got two flags that disagreed.
- The same flag gated two rules: the one-stop warning needed `not stale`, and the `passed_stop`
  ending needed `not stale`. With his stop unpredicted, neither could ever hold, though the
  bus's own position knew he was one stop away or past it.
- `Rides.set_stop` refreshed the snapshot with no vehicle, so for up to one tick after
  `bus(get_off=...)` a live ride was described as timetable-only.
- Nothing reads `stale` or `live` outside the brain: the iOS app has no ride Live Activity view
  yet, and its only read of `GET /inherent/ride` takes `ride_id`.

## Decision

The ride carries two independent facts, each told in its own words everywhere the ride is told:
`position_live` (a live vehicle matching the trip gave where the bus is, else the timetable did)
and `arrival_live` (the TripUpdates feed predicted the arrival at his stop, else it is a timetable
estimate). `stale` and `live` are removed from the ride's tool result, `GET /inherent/ride` and
the Live Activity content state, and the state-block line says, for example, "The bus position is
live from BC Transit; the arrival at his stop is a timetable estimate and may be off by a few
minutes." Its limits:

- **Predictions need a live vehicle.** With no live position the timetable alone gives the
  arrival, and `arrival_live` is false; an old prediction is not reused.
- **The warning and the `passed_stop` ending need only `position_live`.** The warning fires when
  one stop is left on the bus's own position, whether or not his arrival is predicted.
- **With no live position at all, there is no warning and no `passed_stop` ending.** The
  timetable can be minutes off, and a warning one stop early is a wrong action, not a late
  answer. The ending still comes from `left_vehicle`, he saying so, or `schedule_end`.
- **`set_stop` answers from the last seen vehicle**, not from the timetable.

## Alternatives rejected

- **Keep `stale` and add a second flag beside it.** A reader would still have to know that
  `stale` is true for two reasons. The 10-14 ride had exactly that reader, the model, saying both
  in one answer.
- **Warn on the timetable when there is no live position.** On a bus that is five minutes late,
  the clock puts him one stop out five minutes early; he would get off at the wrong stop.
  A missed warning with no feed for a few ticks costs less, and the feed returning in the next
  tick (15 s) restores it.
- **Treat a missing prediction as no live position and leave the words alone.** That is the bug:
  the position is live (the next stop advanced in real time).
- **Keep `live` (feed is up) for the phone.** A feed that is up but has no vehicle for the trip is
  still no live position; the field answers a different question than the one the phone asks.

## Consequences

- A ride on the timetable gets no one-stop warning and no `passed_stop` ending until the feed
  returns; a long outage leaves the ride to `schedule_end` or `left_vehicle`.
- The phone's Live Activity content state loses `stale` and gains `position_live` and
  `arrival_live`; the Live Activity view, when built, reads those.
- Every answer on a live ride now carries one sentence on where its times come from, a few more
  words in each state block; the line's cap of 520 characters still applies.
