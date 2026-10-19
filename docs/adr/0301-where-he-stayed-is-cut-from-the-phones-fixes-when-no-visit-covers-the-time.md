# ADR 0301 — Where he stayed is cut from the phone's fixes when no visit covers the time

**Status:** Accepted
**Date:** 2026-10-19
**Supersedes:** none

## Context

- Every reader of where he stayed takes `phone_day.stays`: the day line (ADR 0199), the
  places tally (ADR 0234), routines (ADR 0279) and the ledger (ADR 0201). It reads only
  `phone.visit_observed`, iOS's own judgement of a visit.
- On the brain, from 2026-10-10 to 2026-10-18, iOS sent visits on 3 days (10-10, 10-17,
  10-18). Location fixes arrived on every day he went out: 48 to 178 a day, and 79 on 10-14,
  which has no visit at all. On those days the timeline shows walks and rides with nothing
  between them, so "where was I yesterday" misses whole afternoons.
- The phone's sending rules decide what a gap between fixes means (`LocationCollector`):
  - while he moves, it sends at most one fix per 30 s, and only fixes at least 50 m from the
    last one sent;
  - it stops after 5 minutes without moving 150 m, or 90 s after motion reports him still;
  - a 200 m geofence around the resting point starts it again when he leaves.

  So a long gap between two fixes at the same spot means he stayed there. It does not mean
  data is missing.
- Motion arrives as separate segments with no coordinates (walking, automotive, stationary).
  A fix can be worse than 100 m; ride following already ignores those (ADR 0224).
- A late phone batch can arrive hours after the fact (seen on 10-15), so stays must be
  recomputable when old rows land, and caches keyed by `phone_day.stamp` must notice
  location rows.

## Decision

Where no visit covers a stretch of time, cut his stays from the phone's location fixes on
read, and give every reader of stays the one merged list.

- A cut stay is a run of fixes with accuracy of 100 m or better that all lie within 150 m of
  the run's running centre, the same distance the phone uses to decide he has not moved. It
  must last at least 5 minutes. Its length runs from the first fix of the run to the moment
  he left: the start of the first move (walking, running, cycling or automotive, medium or
  high confidence, merged and kept as the day line counts moves) that carried him away. When
  a run is followed by another, that is the last move starting after the run's last fix and
  before the next run's first fix, else the next run's first fix: a four-minute walk about
  the flat at 23:00 does not end a night whose next fix, already outside, comes at 09:00. A
  run with no run after it stays open, with `end_ms` null, as an open visit does, whatever
  moves follow its last fix, and is measured to now.
- Visits win. A cut stay is clipped to the time no visit covers. A move that crosses a
  piece's start or end keeps its minutes, since the first fix of a run often comes while he
  is still walking in. What is left is
  dropped when it is under 5 minutes or lies wholly inside one move: a slow walk sends
  fixes close enough together to look like runs, and no one rests inside a walk.
- A cut stay has the same shape as a visit stay and is told apart only by its origin: its
  id is the `event_uid` of its first fix, and it says it was cut from fixes. Its position
  and accuracy are the means of its fixes, and its place text is the commonest one among
  them. Kept places, the tally, routines and the ledger treat it as a stay.
- Nothing is stored; the stays are folded at each read, as ADR 0199 folds the day. The
  reader looks back to the last fix before the window, so a stay that began days earlier,
  such as a weekend at home, still covers the window.
- `phone_day.stamp` counts location rows, so a late batch of fixes changes the cache key.

## Alternatives rejected

- **Wait for iOS visits** — visits came on 3 of 9 days, and nothing on the brain can make
  iOS send them. The fixes for the other 6 days are already on the brain.
- **Cluster fixes by time alone (a gap of N minutes ends a stay)** — the phone stops
  sending while he is still, so the longest gaps are inside stays. A night at home is one
  gap of hours between the last fix of the evening and the first of the morning, at the
  same spot.
- **Store the cut stays in a table** — this is the second copy of state that ADR 0199
  rejected. A late batch would have to rewrite past rows, while a fold costs one read of a
  few hundred fixes.
- **Let a model find the stays** — this is arithmetic on coordinates and times. A model's
  answer cannot be checked minute by minute, and it would cost a call on every read.

## Consequences

- Stays now exist on days with fixes but no visits. The places tally, routines and the
  ledger's counts change for those days. A spot that was never counted can reach
  `ask_once`'s three days and be offered as a place.
- A short wait, such as 6 minutes at a bus stop, is a stay. The day line shows it between
  two moves.
- Cut stays have no `place` text when no fix inside them carried one; naming them is left
  to kept places and the doing lane (ADR 0302).
- Repository fixtures for cut stays must use made-up coordinates (ADR 0234).
