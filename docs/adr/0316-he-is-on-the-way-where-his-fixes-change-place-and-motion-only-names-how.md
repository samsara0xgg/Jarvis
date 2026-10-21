# ADR 0316 — He is on the way where his fixes change place, and motion only names how

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** none

## Context

- A move on the day line (ADR 0199) comes only from `phone.motion_observed`: segments of
  walking, running, cycling or automotive of medium or high confidence, merged and kept when
  they last 3 minutes. The plan facts and the doing lane read the same moves; the doing lane
  turns them into the place line's walk, run, cycle and drive, and the Mac sums them as "On
  the way".
- On 2026-10-20 he drove a lesson from 13:08 to 14:17 PT, about 21 km with two parked stops
  (13:34 to 13:47 and 13:54 to 14:04). iOS motion said automotive only around 13:11, 13:15
  and 14:15 and unknown the rest of the time, so the day line had one move, 13:09 to 13:15.
  The Mac showed "On the way 0:06" and nothing at all for 13:47 to 13:54 and 14:04 to 14:17.
  He asked why the lesson was not in the line (00:23Z on 10-21) and said 「动手」 at 03:46Z.
- Over the same hour the phone sent `phone.location_observed` about every 30 s and 50 m, with
  10 m accuracy: the fixes moved the whole time. Stays are already cut from those fixes where
  no visit covers the time (ADR 0301); between two stays, the fixes are where he went.
- iOS motion is a classifier of the phone's own sensors. In a car it often says unknown or
  automotive at low confidence; on foot it is reliable. It has no coordinates. A fix has no
  activity, but two fixes give a speed: about 25 km/h is more than he walks, runs or usually
  cycles, and the phone already takes it as the sign of a vehicle (ADR 0312).
- A fix can be off by more than its accuracy: one fix 200 m from home, reported at 65 m,
  between two stays at home must not become a trip.

## Decision

Take a move from his location as well as from motion: where the good fixes leave the stays
and change place, he is on the way, and motion, or else speed, names how.

- A path is the time between two stays that good fixes (100 m accuracy or better, as for
  stays) fall in, with no stay starting among them and no two of them more than 5 minutes
  apart. It runs from the end of the stay before it to the start of the stay after it when
  each is within 5 minutes of its nearest fix; else it starts or ends at that fix. A path
  with no stay after it, whose last fix is at most 5 minutes old, is still going.
- A path is kept only when one of its points, the centres of those two stays included, lies
  farther from its first or last point than 150 m beyond both their accuracies.
- The motion moves keep their place. What of a path they do not cover becomes moves of its
  own, named, in this order: automotive when two of its points at least 10 s apart are at
  least 25 km/h apart beyond their accuracies, unless motion said cycling on that path; else
  the activity motion reported for most of that time (medium or high, any length); else the
  one activity motion reported on that path, when it reported only one; else no activity.
- Then moves of one activity, or of none, that are at most 3 minutes apart merge, and a move
  under 3 minutes is dropped, as before. The cut stays still end where motion moves say, so
  no stay changes.
- A move with no activity is drawn as moving; the doing lane counts it as a drive, and a bus
  ride covering half of it replaces it as it replaces an automotive move (ADR 0224).

## Alternatives rejected

- **Count motion segments of low confidence too** — on 10-20 motion said unknown, not
  automotive at low confidence, for most of the lesson; no confidence level would have
  found those minutes.
- **Take moves from location only and drop the motion moves** — after a deep sleep the phone
  reads motion history (ADR 0312), which says when he started walking minutes before the
  first fix; and motion is what tells a walk from a ride at walking speed.
- **Read the dense track (ADR 0312) for the paths** — the sampled fixes put a path's ends
  within about 30 s, finer than the day line draws, while the track would add about 2 MB of
  JSON to every fold of two days for every reader of the phone's rows.
- **Name a slow path walking or cycling by its speed** — between 7 and 25 km/h a car in
  traffic, a bike and a run look the same in fixes 30 s apart; a wrong name is worse than
  "moving".

## Consequences

- On days with fixes but little motion, "On the way" grows and the place line has fewer
  holes. Plan facts see these moves too, so a drive with no automotive segment can now end
  a wait for him to leave (`plan_watch`).
- A move can have no activity. The Mac and the phone already draw one as moving; the doing
  lane turns it into a drive, which is wrong for a slow walk motion missed.
- A noisy pair of fixes more than 150 m beyond their accuracies from a stay at the same spot
  is still a trip of a few minutes, and a gap of 5 minutes without a fix inside a path ends
  it.
- A move found from fixes takes the id of the path's first fix and its start, `uid@start`, so
  a fix or a stay that arrives late can change the id of the move around it.
