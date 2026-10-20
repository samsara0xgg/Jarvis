# ADR 0312 — The phone keeps every fix while he moves, and the car's motion while he rides

**Status:** Accepted
**Date:** 2026-10-20
**Supersedes:** none

## Context

- On 2026-10-20 the map of his driving lesson (13:08 to 14:17 PT, about 21 km) showed one
  point every 30 s at best, and lost the first minutes after two of its three stops. He asked
  for a finer track while he moves fast (23:03Z), and set the rule 「可以节约电量 但是尽量不能
  丢失信息」 (23:09Z). He said 「动手」 at 23:18Z.
- While he moves, `LocationCollector` already runs GPS at about 10 m accuracy with no distance
  filter, so iOS hands over about one fix a second. The phone then sends at most one fix per
  30 s and 50 m (`phone.location_observed`) and drops the rest. The battery cost of the GPS is
  paid for every fix it drops.
- Every reader of where he is reads `phone.location_observed` row by row: here (ADR 0198),
  stays cut from fixes (ADR 0301, which assumes the sampled rate and that a gap at one spot is
  a stay), ride following (ADR 0224), plan facts and the day line. At one fix a second, a
  ride of an hour would be 3,600 rows for each of them.
- When he stops, updates end after 90 s of motion saying he is still, or 5 minutes without
  moving 150 m. Ending them also ends live motion and lets iOS suspend the app, so nothing
  can wake it fast: only a 200 m geofence, a significant change or a late visit departure,
  all judged from cell and Wi-Fi. On 10-20 the app woke 6 minutes and 403 m after he drove
  off from one stop, and 3 minutes and 515 m after another; the first fix was 52 m off.
- A running app with standard updates at three-kilometre accuracy keeps running in the
  background on cell and Wi-Fi positioning, with GPS off, and keeps getting live motion.
  Arc (LocoKit) records whole days this way. Damoov puts GPS at one fix a second at 5 to 10 %
  of the battery an hour; driving-telematics SDKs (DriveKit) run GPS only during a trip.
- Braking, turning and a knock show in the phone's device motion (acceleration, rotation,
  attitude), which iOS delivers only while the app runs. At 25 samples a second with 13
  values each, an hour is about 3 MB as gzip CSV. An event frame is at most 64 KiB.
- He wants a later step to make a drive usable as evidence after an accident (time stamps
  and checks, a copy on the Pi). That step needs the raw values, and whether iOS marks a fix
  as simulated by software, kept as they were sensed.

## Decision

While location updates run, the phone keeps every fix iOS gives as a dense track beside the
sampled fixes; while he is in a vehicle it also keeps the device motion as raw pieces and
keeps GPS through stops; and it stops in two steps, a light sleep that stays awake without
GPS before the deep sleep of today.

- The track is `phone.track_observed`: every fix of a run of updates, in any mode, at most one
  minute a frame, with the values iOS reported and whether iOS marked the fix simulated or
  from an accessory. `phone.location_observed` stays as it is and remains what every reader
  of where he is uses. The track is read only by readers that ask for it, over a window.
- He is in a vehicle when motion says automotive, when the fixes say at least 25 km/h for 10 s
  and motion does not say cycling, or when the audio route is CarPlay. A bus counts. It ends
  when motion says walking, running or cycling, or when updates sleep. In a vehicle, GPS runs
  at its best accuracy, and a stop keeps it on for up to 10 minutes.
- In a vehicle the phone records device motion at 25 Hz into pieces of at most 10 minutes,
  kept by the brain byte for byte in the raw archive of ADR 0238, beside the audio. Nothing
  on the phone judges braking or turning; a reader computes that from the raw pieces.
- When updates would have ended, they drop to three-kilometre accuracy with live motion for
  30 minutes, sending no sampled fix. Any sign of moving (motion, speed, or a coarse fix far
  beyond its own accuracy) brings GPS back at once. After 30 minutes, the deep sleep: visits,
  significant changes and a rest geofence of 100 m.
- Waking from the deep sleep reads the motion history at once, so the brain learns when the
  move really began even when GPS came late.

## Alternatives rejected

- **Send every fix as `phone.location_observed`** — an hour's ride becomes 3,600 rows that
  here, stays, rides and plan facts each read one by one, and ADR 0301 would cut stays from
  a rate it was not tuned for. One track frame a minute is 60 rows an hour.
- **Thin less (one fix per 5 s)** — still drops four fixes in five, while the GPS costs the
  same as for all of them.
- **Keep the track as an archive file, like the audio** — archive pieces go on Wi-Fi only
  and close every 10 minutes, so a drive would reach the brain hours later. A track frame is
  about 5 KB and goes with the other events on any network, acknowledged one by one.
- **Send device motion as events** — a 64 KiB frame holds about 40 s of it as JSON, so an
  hour is about 90 frames of text that no reader of the event log wants.
- **Keep GPS on through every stop** — 30 minutes of GPS at each stop of the day costs about
  as much as 30 minutes of driving, for fixes at one spot.
- **Judge hard braking on the phone and send only the judgements** — the thresholds could
  never be changed for drives already recorded, and the evidence step needs the raw signal.

## Consequences

- Each stop keeps the app running in the background for 30 minutes, and each ride adds device
  motion and GPS through stops. The cost per hour is not measured yet; the first real drive
  measures it.
- The event log grows by about 300 KB an hour of moving, and the archive by about 3 MB an hour
  in a vehicle. Bus rides are in the motion archive too: it does not know who drives.
- Pieces of device motion wait for Wi-Fi, so a drive's motion reaches the brain when he is
  home.
- ADR 0301 reads a gap at one spot as a stay. That still holds: no sampled fix is sent while
  updates sleep, light or deep.
