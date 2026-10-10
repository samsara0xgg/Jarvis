# ADR 0221 — A named bus is asked about directly and judged by spare minutes

**Status:** Accepted
**Date:** 2026-10-10
**Supersedes:** none

## Context

- 2026-10-10, Allen, walking to a stop, asked five times whether he could still catch the 39 that
  left at :46. Google drops a bus once its leave time (departure minus Google's own walking
  estimate) has passed, so a bus he could reach by walking fast vanished from "leave now" answers,
  and the answer flipped as the minute ticked. The model probed with shifting `depart_at` values,
  named a target arrival he never gave, and contradicted an earlier result after one failed call.
- `transit` had no way to be asked about one route, so "can I make the 39" was never a question
  it could answer directly.
- Speech recognition heard the route number as "39 degrees" and as nonsense words; "UVic" and
  "CARSA" alone returned no route although the saved word `school` and "CARSA, University of
  Victoria" did.
- BC Transit's realtime feed (ADR 0200) knows when the bus really leaves; the tool layer may not
  import the runtime that holds it (the layer contract), so the runtime hands it in as it does `here`.

## Decision

`transit` takes an optional `route`; asked with it, the tool starts Google ten minutes before now,
puts that route's first bus first in the options, and returns `named_route` with its stop,
departure, walk, `spare_min` (minutes to the bus minus the walk to its stop) and a verdict, and
the following departure.

- Every option carries `spare_min`. The verdict is `easy` at 2 or more, `missed` at -2 or less,
  `tight` between (he must walk fast), and `none` when Google has no such bus still ahead; then
  the usual "now" answer is fetched once more and returned.
- The named bus is never cut by the far-fetched filter. Buses the earlier start returns that he
  could not reach at a normal pace are not offered.
- The runtime injects BC Transit's live departure; when it answers within four seconds it replaces
  Google's departure in `named_route` (`live: true`), otherwise Google's time stands.
- "UVic" and "University of Victoria" are the saved `school`. A text destination that comes back
  not found is retried once with the home's city appended (Victoria, BC if none).
- The description tells her to pass `route`, answer about that bus first, use the number when
  speech recognition garbles it, never probe with shifting `depart_at`, never invent a target time,
  claim catchable or not only when this turn's result shows the bus, and on a failed call say only
  that the lookup failed.

## Alternatives rejected

- **Probe with `depart_at` values until the bus shows** — the five live calls changed the answer
  with the minute (15:40 to 15:42 returned the 39, 15:43 did not) and cost five Google requests.
- **Shift every "now" request back ten minutes** — it returns buses he cannot reach at a normal
  pace for questions that name no bus; the shift is paid only when a route is named.
- **Prompt guidance only, no `route` argument** — in five asks she inferred "cannot catch" from the
  39 being absent; the verdict is computed from the result instead.

## Consequences

- A named-route ask whose bus Google lacks makes two TRANSIT requests instead of one.
- A cold BC Transit cache makes the first live lookup slower than four seconds; that call keeps
  Google's time and the download finishes in the background.
- `named_route.departs` can differ from the option's `departs` and `leave_at` when the live time
  applies; the card and a pin still copy the option, whose live refresh is ADR 0200's.
- The retry for a missing city adds a request for each text destination Google cannot find.
