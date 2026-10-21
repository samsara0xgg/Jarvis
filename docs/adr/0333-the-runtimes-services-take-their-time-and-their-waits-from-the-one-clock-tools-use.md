# ADR 0333 — The runtime's services take their time and their waits from the one clock tools use

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** none

## Context

ADR 0322 gives tools one clock from the runtime. The services in `jarvis/runtime/` and
`jarvis/state/` that tools do not touch still read the wall clock and wait on their own: 183
direct reads and sleeps in 60 files (grep of `time.time()`, `time.monotonic()`, `time.sleep(`,
`datetime.now(` on main e07dffe2).

Measured on that commit:

- `test_lifecycle_commentary` takes 61 s for 127 tests, nearly all of it real waits. The commentary
  hold waits on `Event.wait` against `time.time()`, so each test patches the shipped 8 s and 25 s
  holds down to a fraction of a second and sleeps through them, and the shipped numbers are never
  run.
- `test_surrogate_route_parallel` asserts wall-clock bounds (`elapsed < 0.9`, `400 <= latency_ms <=
  500`, `1.0 <= elapsed < 1.6`) against an endpoint that really delays, and flakes under load.
- Tests that depend on the time of day failed on 2026-10-21.
- `time_machine` freezes `time.time()` and `datetime.now()`, but a thread already blocked in
  `Event.wait(8)` stays blocked for 8 real seconds, so a freeze cannot make a hold pass.

ADR 0325 asks for "an injected clock and zone" in production code and counts 11 fake clocks in
the tests.

## Decision

The runtime's services take the time and their waits from the same one clock that ADR 0322
gives tools: `runtime.clock`.

Limits:

- **One object.** The runtime builds it once at boot, and a service is handed it, never builds its
  own. The tool context carries the same object.
- **Everything time goes through it:** the wall time, the monotonic time, every sleep and every
  wait on a thread event. A service that has the clock reads no `time` function or
  `datetime.now()` itself.
- **What stays real:** the event loop's own `asyncio.sleep` (a poll's cadence), socket and HTTP
  timeouts, SQLite busy deadlines, hold instrumentation, and audio timing in the voice surface,
  which are real time by nature. So is a stamp that must equal one another layer writes with
  `time.time()`: the Event Log's rows, the Jev dataset's rows, and a turn ending that the surface
  reports with its own `time.time()`; each moves when that layer takes the clock.
- **Tests** hand the service the fake clock and move it with `advance`; a test that checks how
  long something took checks it on that clock.

## Alternatives rejected

- **Keep freezing time with `time_machine`.** It cannot end a wait already blocked: the 61 s above
  are waits a freeze does not shorten.
- **Patch each service's timing constants down in its tests.** `test_lifecycle_commentary` does it
  at 11 sites and runs 0.3 s where the owner chose 8 s, so a change to the shipped number passes
  every test unrun.
- **Give each service its own `now` and `sleep` parameters.** `night_run` does, and the tests
  hold 11 fake clocks (ADR 0325): four parameters per service, no one zone, and no single fake to
  move all of them at once.

## Consequences

- A service built outside bootstrap takes the clock as a parameter, and every constructor that
  holds a service's timing now has one more argument.
- The Event Log still stamps new rows from `time.time()` itself, so a test that moves the fake
  clock gives the rows it cares about their time explicitly.
- A wait on the clock is a wait on a thread event, so an async service that holds on the clock
  does so from a worker thread, as the commentary does.
- The services not yet moved keep reading the wall clock, and a freeze of `time.time()` and
  `datetime.now()` remains the only way to hold them still until they are.
