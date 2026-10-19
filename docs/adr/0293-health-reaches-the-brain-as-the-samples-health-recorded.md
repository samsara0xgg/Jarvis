# ADR 0293 — Health reaches the brain as the samples Health recorded

**Status:** Accepted
**Date:** 2026-10-18
**Amends:** ADR 0197 (what the phone sends from Health)

## Context

- ADR 0197 sends from Health three things already made into answers: hourly step totals, a
  workout as minutes plus a name, and sleep as merged asleep blocks sent 45 minutes after they
  settle. The phone merges and decides what a night is.
- He wants every source kept as the source gave it, with the source's own ids and times, so a
  question not yet thought of is not blocked by a number computed too early.
- An Apple Watch arrived on 2026-10-18. It writes heart rate, heart rate variability, blood
  oxygen, respiratory rate, wrist temperature, sleep stages, heartbeat series and workouts.
  No sleep or workout row has ever reached the log, so nothing is stored that must change shape.
- Health merges iPhone and Watch only inside statistics queries; the raw samples of a quantity
  both devices count (steps, energy, distance) exist twice, so summing them overstates.
- Health keeps a stable UUID for every sample, and an anchored query returns exactly the
  samples added since a saved anchor, which makes a resend impossible to confuse with a new one.
- Heart rate alone is a few hundred samples on a rest day and thousands in a workout; a
  heartbeat series is a few hundred beat times. The day fold (ADR 0199) reads every
  `phone.health_observed` row of two days on each request.
- A frame is capped at 64 KiB and a batch at 1 MiB (ADR 0197).

## Decision

The phone sends each Health sample as Health recorded it, with its UUID, span, value, unit,
source and device, and sends cumulative quantities only as hourly totals; every fold the brain
makes from them is made on the brain.

- Measured values (heart rate and its resting, walking and variability kin, blood oxygen,
  respiratory rate, wrist temperature, VO2 max, audio exposure, heartbeat series) are one event
  each in a new type, so the dense stream is not read by folds that never use it.
- Spans (sleep stages, workouts, mindful sessions, stand hours) are `phone.health_observed`
  events, one per sample, none merged. A workout carries what Health knows of it, not its route.
- Steps, energy, exercise, stand time, distance, flights and daylight stay hourly totals.
- The brain makes asleep blocks from the stages, joining overlaps and gaps of 30 minutes as ADR
  0199 did, and counts workout minutes from the workout's length.
- The phone reads by anchored queries with a saved anchor per type, so nothing is missed or
  sent twice, and asks Health for background delivery of each type.

## Alternatives rejected

- **Keep the phone's merged sleep blocks and add the stages beside them** — two sleeps for one
  night disagree after the first late correction, and the block's 45-minute settle delay is
  the phone deciding when a night is over. The brain can fold the stages on every read.
- **Raw samples for steps, energy and distance too** — with a Watch and a phone both counting,
  the sum is roughly double (Health's own statistics exist to prevent that). Hourly totals from
  the statistics query are the only correct count the phone can give.
- **Put the measurements into `phone.health_observed` as well** — the fold reads every row of
  that type from two days back on each day-line and ledger request; thousands of heart rate
  rows a day would be decoded for sleep and steps that need a few hundred.
- **Send a heartbeat series as summary statistics (mean, RMSSD)** — it is the one sample Health
  keeps as beat times, and the statistic is what a later model can recompute from the times
  but not the reverse.
- **Poll with date-range queries instead of anchors** — a late-arriving Watch sample inside a
  range already read is missed, or the range is read again and every sample sent twice.

## Consequences

- Several thousand frames a day instead of tens: the phone's queue cap rises from 5000 to
  50000 frames, and one flush sends more than five batches.
- A day's log gains the Watch's full output (about 1 to 3 MB a day, more with a long workout); nothing
  reads the measurements yet.
- A sample Health deletes or edits later is not unsent: the log keeps what was recorded.
- `sleep_min` and `workout_min` are gone from the wire; a reader looking for them finds none.
- A heartbeat series longer than 4000 beats arrives as several events the reader joins by
  `part`.
