# ADR 0323 — His footprint is folded when asked, as the outings between long stays, each with its route

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** none

## Context

- He asked for a 我的足迹 page on 2026-10-20 (「或者你干脆现在做了 一个我的足迹的ui」). On
  10-21 he approved its second design, a map of the day's outings above the timeline's own bar
  (03:47Z 「这一版设计得非常好看，我很喜欢」), and said to start building it. Fifteen minutes
  after he got somewhere, the design shows him arrived there, not still out.
- The day line (ADR 0199) folds stays (ADR 0301) and moves from the phone's rows on every read.
  It has no route, distance or speed. `GET /inherent/track` (ADR 0312) gives the raw fixes of
  at most 12 hours: at one fix a second, an hour of driving is 3,600 rows of 11 values, about
  300 KB of JSON.
- His two driving lessons: 10-18, 11:58 to 12:36, about 9 km with one 16-minute stop; 10-20,
  13:08 to 14:17, about 21.7 km with stops at 13:21, 13:33 and 13:54, no fix at all from 13:42
  to 13:47 after a stop, and about 81 km/h at most. Both began and ended at home, and he lived
  each as one lesson. Both days predate the dense track: they hold only the sampled fixes, one
  per 30 s and 50 m at best.
- A fix right after the phone wakes can be far off (the first after one 10-20 stop was 52 m
  off), and iOS marks the fixes software simulated (ADR 0312). In a vehicle the phone keeps GPS
  on through a stop of up to 10 minutes, a fix a second, each a few metres from the last
  while the car stands still.
- iOS gives a speed with each GPS fix. Between sampled fixes 30 s apart, only distance over
  time is known.
- He wants his heart rate while driving set against the same day at home (10-20: 88 in the
  lesson, 68 at home). The Watch's samples arrive as `phone.measure_observed` heart_rate.
- Every fixed word Jarvis shows lives in a zh/en table and follows the language its settings
  name, the system's by default (yilun 10-19). The phone already has the day line's calendar
  items.
- The phone queues up to 50,000 frames while it cannot reach the brain (ReportQueue), so the
  rows of an outing can arrive hours after it ended.

## Decision

Fold his footprint of a day from the event log on every read, as the day line's long stays and
the outings between them, each outing with its route, stops, gaps, distance, speeds and heart
rate, and serve it read-only at `GET /inherent/footprint`; store nothing and send no words.

- A long stay is a stay of the day line, with its id, that lasted at least 20 minutes, lies in
  a place he keeps (ADR 0234), or is where he is now. An outing is the time between two long
  stays that holds a move or ends at least 300 m from where it began; its shorter stays are its
  stops. An outing that overlaps the day is given whole; with no long stay after it yet, it is
  still going.
- The route is the dense track (ADR 0312) of the device with the most fixes in the outing, with
  that device's sampled fixes added where its track has none within 30 s. A fix worse than
  50 m, or simulated, is left out. Two points more than 2 minutes and 300 m apart are a gap:
  no line is drawn across it.
- The speed at a point is the mean over the 10 s around it of iOS's speeds, else of distance
  over time; the outing's top speed is the highest of those means.
- The route is simplified so that no fix left out lies more than 5 m from where the kept points
  put him at its time, and the distance runs along the kept points, straight across a gap.
- Heart rate is the Watch's samples inside the outing, set against the mean of that day's
  samples at his kept home.
- The phone words the page from its own table: a calendar item of the day line over an outing
  names it, else its destination does.

## Alternatives rejected

- **Draw from `/inherent/track` on the phone** — an hour of driving is about 300 KB of rows,
  and the phone would fold stays and moves a second time by rules the brain owns and the
  ledger counts by; the two folds of one day part the first time either rule changes.
- **Split outings at every stay of 5 minutes or more** — the 10-20 lesson becomes four outings
  and the 10-18 one two, though he lived each as one; at 20 minutes both stay whole, and a meal
  or a class still ends an outing.
- **Store each outing as an event when it ends** — rows that arrive hours late, from the
  phone's queue, would be missing from it until it was rewritten, and it would disagree with
  the day line, which folds the same rows on every read.
- **Send every fix unsimplified** — six hours of walking with updates running is 21,600
  points; on the outing page (a few kilometres across 440 points, several metres a point),
  5 m is less than a point.
- **Measure the distance fix by fix** — ten minutes parked with GPS on are 600 steps of a few
  metres each, a kilometre or more that no road holds; in the tests a straight 3 km run with
  fixes 2 m off the line measured 3,088 m.
- **Word titles and places on the brain** — each call would need the phone's language, and
  fixed words would live outside the phone's table.

## Consequences

- Each open of the page folds the day again: the day line's read of the phone's rows plus the
  track over each outing, up to 43,200 fixes for 12 hours of moving, on a worker thread.
- Days before the dense track show one point per 30 s at best and a gap after most stops.
- The thresholds apply to past days at once, since nothing is stored, and a change to how ADR
  0301 cuts stays moves the outings with it.
- A halt shorter than the day line's shortest stay (5 minutes), such as a red light, is no stop;
  it shows only as low speed on the route.
- While he is at a stop, the outing ends there; when he drives on, the stop is a stop again
  and the two halves are one outing.
