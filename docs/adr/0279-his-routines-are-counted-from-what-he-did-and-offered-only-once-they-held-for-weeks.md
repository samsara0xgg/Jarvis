# ADR 0279 — His routines are counted from what he did, and offered only once they held for weeks

**Status:** Accepted
**Date:** 2026-10-16
**Supersedes:** none

## Context

- Planning his day (ADR 0275) needs defaults for what nobody wrote down: when he usually leaves
  home, which bus he usually takes, where he usually is on a weekday, when he usually sleeps.
  Things he has to go to are commitments (ADR 0274) and come first; a routine only fills the gaps.
- The data that shows these is already logged: the phone's visits (`phone.visit_observed`, ADR
  0197), the rides he was on (ADR 0224), the phone's sleep samples (`phone.health_observed`), and
  his kept places (ADR 0234).
- On 2026-10-16 the brain held phone data from 2026-10-09 only, visits from one day (10-10) and
  one ride. A pattern seen once or twice is not his routine; a planner that trusts one is wrong
  in a way he notices.
- Class days are written in his calendar and in commitments. Learning them from visits would
  duplicate a source that is exact.
- Places are kept by ADR 0234's table; unkept spots are clustered fresh on every read and their
  ids are only valid for that read, so nothing durable may be keyed by them.

## Decision

- `jarvis/state/routines.py` counts routines on read; there is no table and nothing is written.
  It reads the event log and the kept places, over the last 12 weeks before the day asked about.
- Four kinds are counted:
  - `leave_home`: the first time each day he leaves his kept home;
  - `other`: his first arrival each day at a kept place that is not home (the place's name);
  - `ride`: each ride, by line, boarding stop and alighting stop;
  - `sleep`: each night's sleep, from bedtime to waking.
  `class` and `meal` stay in the shape for the planner and are not counted (class days come from
  the calendar and commitments; nothing logs meals).
- A routine is one kind, one key (place, ride, home, sleep) and one weekday. It is offered only
  when it happened in at least 3 distinct weeks and in at least 60% of the weeks the source
  reported anything on that weekday. Until then the reader returns nothing, and it fills in by
  itself as visits and rides build up. `seen` and `of` go with every routine, so the planner can
  weigh it.
- Its time is the median of the times seen, and `spread_min` the median distance from it; sleep is
  counted around midnight, not across it.
- Unkept spots give no routine. A spot he goes to often is asked about and named through ADR 0234;
  once kept, its visits count.
- `routine_id` is a digest of kind, key and weekday, so it stays the same while the pattern holds.
  `sources` are the event uids of the visits and sleep samples and the ride ids it rests on.
- A routine is a default and never a commitment. A commitment that day wins. A fixed arrangement he
  states ("class every Tuesday") is a weekly commitment through `keep`, not a routine.
- Read surface: `for_day(conn, day, *, zone, places)` returns the routines of that day's weekday,
  for the planner inside the brain process. No tool, prompt or model-visible text changes here.

## Alternatives rejected

- **A routines table refreshed each night.** It would hold a second copy of the visits ADR 0234
  keeps out of storage, and go stale the moment he renames or forgets a place; counting on read
  over twelve weeks of rows is cheap.
- **Offer a pattern after one or two weeks.** On the data the brain holds today that is one day of
  visits and one ride; the planner would present a guess as his habit.
- **Learn class days from his visits to school.** His calendar and the commitments log hold them
  exactly; a learned copy could only disagree.
- **Key routines by unkept spots.** Their ids change with every clustering, so a routine's id would
  not stay the same from one read to the next.

## Consequences

- Today it returns nothing; it starts to offer routines about three weeks after the phone's
  visits and rides flow steadily.
- Visits stopped after 2026-10-10 while locations kept arriving. Until visit capture is fixed
  (another thread), only rides and sleep can build up.
- A routine that ends (a term is over) fades as its share of observed weeks drops under 60%.
- Labels (`what`) are the kept place's name, the line and stops, or empty; wording them for him
  is the planner's or the model's job.
