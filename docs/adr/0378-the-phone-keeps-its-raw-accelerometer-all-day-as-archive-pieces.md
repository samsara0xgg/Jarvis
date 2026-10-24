# ADR 0378 — The phone keeps its raw accelerometer all day, as archive pieces

**Status:** Accepted
**Date:** 2026-10-23
**Supersedes:** none

## Context

- On 2026-10-23 yilun chose 「做」 for knowing whether his phone is on him. Motion activity
  (`phone.motion_observed`) and steps show the phone carried while he walks; `stationary`
  covers the phone in his pocket while he sits and the phone on a table alike, and nothing the
  brain holds tells them apart.
- His rule for data: every source of his devices is kept raw on the brain, and nothing is
  judged at ingestion; later layers read it. Ride motion already follows it (ADR 0312).
- `CMSensorRecorder` has the motion coprocessor record the accelerometer at 50 Hz while the
  app is suspended or killed, for up to 12 hours per request; the app reads the samples back
  for 3 days. It needs only the Motion & Fitness permission the app already asks for, and
  works on A10 chips and later. Apple DTS calls it the default API for motion analysis that
  need not run in real time (developer forums thread 841616).
- Live accelerometer updates (`CMMotionManager`) arrive only while the app runs; the app is
  suspended for most of a day.
- The app wakes in the background many times a day (location, silent pushes, report
  flushes), each time for seconds, and reading a recorder's samples back costs CPU per sample.
- At 50 Hz with three values, a day is 4.32 million samples. As gzip text of 10-minute pieces
  a simulation gives 200 to 310 KB a piece, about 30 to 45 MB a day. An event frame is at most
  64 KiB.
- The raw archive (ADR 0238) keeps audio and ride motion pieces byte for byte by kind, device
  and UTC start; the phone sends them on Wi-Fi only, and the brain refuses a piece that would
  take its disk under 5 GB free.

## Decision

The phone keeps the recorder running at all times, reads back what it recorded at its wakes,
and sends it in 10-minute pieces of raw acceleration (gravity included, in g, in the phone's
own axes, at the recorder's own sample times) to the raw archive as their own kind, `accel`;
nothing on the phone or at ingestion judges whether it was carried.

- It asks the recorder for 12 more hours at every wake, so recording lapses only when the app
  is not woken for 12 hours.
- Pieces are cut on fixed 10-minute UTC boundaries, and a piece is read only once its window
  has ended long enough ago for the recorder to have delivered it, so reading the same window
  again after a kill makes the same piece.
- A piece is the samples the recorder gave, gaps and rate as they came. A window with no
  samples sends nothing.
- An iPad records nothing (ADR 0336).

## Alternatives rejected

- **Send the samples as events** — a 64 KiB frame holds about 40 seconds at 50 Hz as JSON, so
  a day would be over 2,000 frames of text no reader of the event log wants.
- **File them with the ride motion (`motion`)** — a reader of a ride's motion would open every
  piece of the day to find the ride's, and the columns differ: no rotation, attitude or
  heading here, gravity not taken out.
- **Judge carried or put down on the phone and send only that** — the thresholds could never
  change for days already recorded, and a later reader could not try another way.
- **Keep one value a second (mean and spread)** — breathing and the shape of a step, which
  tell a pocket from a table, are faster than a second.

## Consequences

- The archive grows by about 30 to 45 MB a day, some 15 GB a year; the first real day
  measures it.
- A day's backlog may need several wakes to read; samples the app does not read within 3
  days are gone.
- If the app is not woken for 12 hours the recorder stops until the next wake, and the gap
  shows as missing pieces.
- Pieces wait on the phone until Wi-Fi, like the other archive pieces.
- The cost to the phone's battery is not measured yet.
