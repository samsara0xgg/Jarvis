# ADR 0245 — His bus is one tool, and the server remembers the last list and the ride

**Status:** Accepted
**Date:** 2026-10-11
**Supersedes:** ADR 0189 and ADR 0221 (the `transit` tool they describe), and the clauses of ADR 0203, 0204 and 0224 that make `pin_departure`, `watch_for_boarding`, `start_ride`, `set_ride_stop` and `end_ride` model tools. Everything else in them stands.

## Context

- The model had six bus tools: `transit` (ADR 0189, 0221), `pin_departure` (ADR 0200, 0203, 0204)
  and the four ride tools (ADR 0224). Their entries are 7,490 characters of the 53,078 the
  56-tool menu spends on tools; `transit`'s description alone is 2,911 and ends by pointing at
  `pin_departure`.
- `pin_departure` takes six fields copied out of an earlier `transit` result in the conversation
  (leave time, route, stop, bus time, arrival, destination). The server already holds the
  options that result listed (ADR 0205) and the ride he is on (ADR 0224); only the model carries
  either from one call to the next.
- The replay exam of 2026-10-11 (three models) on the bus:
  - "how long until I am home" on the bus: all three called `transit` for the next departures,
    not the ride he was on;
  - "I have eaten, taking the bus home now": no ride started;
  - "can you remind me at the next stop": the models said they cannot track the bus, though the
    ride tools were on the menu;
  - "which stop do I get off at" and "get off at that stop": wrong tools and recall cursors
    that no result had given;
  - "I am on it" after being told which bus: `start_ride`, right.
- A repeated identical call in a turn is refused (ADR 0239), so every intent needs arguments of
  its own.

## Decision

The model's bus is one built-in tool, `bus`, on the menu from the first request. With no
arguments it answers about the ride he is on (route, next stop, his stop, stops and minutes
left), else the next buses home. `to` (with `from`, `at`, `route`) lists up to three numbered
options as `transit` did, and the server keeps that list; `pin` puts options of the last list on
the notch by number; `ride` says he boarded (`start`, by `option` or `route`), is heading out
(`watch`) or got off (`end`); `get_off` sets his stop. Arguments of different intents may not
be mixed, a number the list lacks is an error that says what the list has, and every answer
opens with a sentence he can hear, in local time with "in N min", and carries no ids. The six
tools leave the menu; pins, cards, pushes, live tracking and the warning before his stop are
unchanged.

## Alternatives rejected

- **Keep the six tools and rewrite their descriptions.** In the exam the ride tools were on the
  menu with descriptions that said when to use them, and the three models still called `transit`
  for the ride question: they choose by the topic, and the ride question is a bus question. One
  entry with a no-argument ride status removes the choice; six entries cost 7,490 characters,
  one costs 2,363.
- **An `action` argument on every call.** A second word to get right before the first argument,
  for the commonest call (no arguments, and the ride is the answer); the arguments already say
  the intent, and only `ride` needs a word.
- **A read-only `bus` plus a separate tool for pins and rides.** The exam's wrong choices were
  between exactly those two (a times lookup instead of the ride), so the split keeps the
  failure and the second entry.
- **`ride=watch` folded into a times lookup.** A read that also writes: every repeated lookup
  would start the 45-minute watch again.
- **Pin by copying the option's fields, as before.** The fields are in the server already; the
  copy is what a stale conversation gets wrong, and it made `pin_departure` the one tool whose
  arguments cannot be given without a `transit` result in view.

## Consequences

- `bus` is not read-only (it writes pins and rides). The turn guards (ADR 0239) count a
  successful call, a times lookup included, as a write: an identical repeat after any `bus` call
  runs again, and an answer that says something was set is not sent back after a bare lookup. A
  live delegation limited to read-only tools (ADR 0016) no longer sees the bus times.
- The option numbers belong to the last plan in the daemon's process. A newer plan renumbers
  them; after a restart `pin` and `ride=start` with `option` say to ask for the times again. The
  chat card's rows, and their pin by offer (ADR 0205), are unchanged.
- A pin of the buses already pinned (the same route and stop, within five minutes of the live
  time) leaves the pin and its ring as they are, and says so.
- The description cannot carry `transit`'s three paragraphs: what to say of a named bus, that
  `from` is only for a start he names, and that a pin is asked for, never offered, moved into the
  argument descriptions, which every request still carries.
- Turns already in the log and logged requests still name the six old tools: the step labels keep
  their names, and the exam reads them through its alias file.
