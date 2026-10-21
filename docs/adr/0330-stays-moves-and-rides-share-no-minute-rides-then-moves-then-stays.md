# ADR 0330 — Stays, moves and rides share no minute: rides, then moves, then stays

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** none

## Context

- The place line of a day (ADR 0199) is stays, moves and rides. A stay is a visit iOS
  reported or a run of fixes (ADR 0301); a move is motion's or a path of fixes (ADR 0316); a
  ride is a bus ride followed on the bus's own position (ADR 0224).
- A cut stay already gives a move that crosses its start or end the minutes they share, and is
  dropped when it lies wholly inside one move. A visit is kept as iOS dated it, and a move that
  only partly overlaps a ride is kept as it is.
- Over 2026-10-07 to 2026-10-20 the brain's day lines had 31 pairs of overlapping items, all
  between a stay and a move: 27 moves across a visit's start or end (median 135 s, longest
  505 s), one visit of 5 minutes wholly inside a 16-minute walk, and 3 rides across a stay's
  edge (83 to 154 s). The Mac drew them on top of each other.
- On 10-20 at 17:54:15 iOS dated an arrival while the 1 Hz track still had the car at 14 m/s;
  it stopped at about 17:54:30 and motion ended automotive at 17:54:54. At 19:04:35 motion said
  automotive while the visit lasted until 19:05:43. iOS dates a visit's arrival and departure
  after the fact, from cell and Wi-Fi changes; motion reports a move as the phone feels it, and
  a ride is timed by the bus feed.
- Motion says automotive in a car that has stopped as well as one that moves (ADR 0312), so a
  visit inside a drive can be a real stop; it cannot say walking for minutes while he stands.
  iOS also splits one visit in two back to back, a second apart, without his leaving.
- He asked for it to be done to the end (「可以要做就做到完美」, 07:30Z on 10-21).

## Decision

Give contested time to the item that measured it best: a ride takes its time from every stay
and move, a move takes the minutes it shares with a stay whose edge it crosses, and a stay
keeps the rest.

- A visit gives way to motion's moves as a cut stay does: a move that crosses its start or end
  keeps the minutes they share, and a visit wholly inside a walk, run or ride on a bike is
  dropped. A visit inside a drive is a stop: it stays, and the drive is cut in two around it.
  This happens in `phone_day`, so the day line, the ledger, places, routines and the plan facts
  share it.
- Stays within 150 m of each other, one starting at most a minute after the other ends, are one
  stretch at one place: a move wholly inside them takes nothing from them and is dropped, as a
  move inside one stay is.
- Two moves with a stay between them are never merged into one, and a move gives up whatever
  time a stay still holds, so a stay and a move never share a minute.
- On the day line, an automotive or unnamed move a ride covers for half its span or more is
  that ride and is dropped, as before. Every other stay and move loses the time a ride covers:
  each piece left keeps its fields, the first its id and a later one `id@start`.

## Alternatives rejected

- **Let the stay keep the overlap** — at 17:54 on 10-20 either edge is within 30 s of the stop
  the track shows, but at 19:05 iOS dated the departure 68 s after motion felt the car move,
  and on 10-17 it dated two visits back to back (14:25:27, 14:25:28) across 14 minutes motion
  felt as walking: a stay keeping the overlap would hide that walk.
- **Decide each overlap by whether the fixes lie inside the visit's circle** — the circle is
  150 m plus accuracy, which a walk in crosses in about two minutes, so the walk in would be
  cut short; and before 10-20 the phone sent at most one fix per 50 m.
- **Clip the moves to the stays instead** — a walk wholly inside a visit is already dropped; a
  walk that leaves the visit's span is a walk, and cutting it at iOS's departure would move its
  start by up to 8 minutes on 10-17.

## Consequences

- Visits are shorter by the minutes moves take, and one wholly inside a walk is gone: the
  minutes at places, the places tally and routines count less on days with such walks.
- A stop at a red light that iOS took for a visit would cut a drive in two; a cut stay inside a
  drive is still dropped (ADR 0301).
- Walking about inside a large shop that motion calls walking now shows as a walk across the
  edges of the shop's visit, not as part of the stay.
- Rides cut stays and moves only on the day line; the ledger still counts a stay's minutes a
  ride overlaps (at most 154 s in those two weeks).
- A stay split by a ride has a second id, `id@start`, that did not exist before.
