# ADR 0197 — A phone reports what it senses in batches over HTTP

**Status:** Accepted
**Date:** 2026-10-09
**Supersedes:** none

## Context

- On 2026-10-09 Allen asked that the phone tell her all of the following (「全要」):
  - where he goes;
  - whether he is walking, riding or staying put;
  - Health steps, workouts and sleep;
  - Focus, alarms and the charger;
  - headphones and calls.
- Battery is a hard gate ("如果严重耗电不可以接受").
- iOS runs an app in the background only in short wakes:
  - a visit or a significant location change;
  - a Shortcuts automation running one of the app's intents;
  - a background refresh.

  Each wake lasts seconds. A socket the app opened is suspended with it.
- Terminals push their observers' events over `/terminal/ws` (ADR 0170). The exchange is
  `hello`, then `ready` with the baselines, then one `ack` per event.
- The same link carries tool calls, voice and asks, and whoever holds it can declare tools.
- The brain's event log is the owner's single state. Every event has a registered type and
  required fields, and an event from a terminal is tagged with that terminal's device name.
- The brain cannot reach the phone:
  - an iOS app does not listen;
  - the system decides whether and when a background push wakes it.

## Decision

A phone sends what it sensed as a batch of events in one HTTP request under its device token,
and the brain appends to its log, under the phone's device name, only events of a fixed set of
phone types.

Its limits:

- **Only the phone's own types are accepted.** They are:
  - a place visited (arrival, and departure when known);
  - a location fix;
  - a motion segment;
  - a Health total over a span;
  - a state change of Focus, alarm, power, headphones or a call.

  The route refuses every other type, including the observer types the terminal link accepts,
  and the link refuses the phone types.
- **No event is stored twice.**
  - Each event carries the phone's own id, and the brain appends an id once, so a batch resent
    after a lost answer writes nothing twice.
  - Each event gets its own answer. The phone drops what was stored or refused, keeps only what
    met an error, and sends it again at its next wake.
- **Only a paired device's token opens the route.** The local key does not.
- **Storing a signal decides nothing about how she uses it.** Where he is (ADR 0198) is the first
  reader. Anything else she does with these signals is a later decision.
- **Sharing is not a signal.** What the share sheet sends her is an input, and it is not in this
  set.

## Alternatives rejected

- **The terminal link `/terminal/ws`.** A wake would have to open a socket, send `hello`, wait
  for `ready`, then wait for one `ack` per event, all before iOS suspends it. One POST does the
  same in a single round trip, about 80 ms over the relay measured in ADR 0183. It also keeps a
  phone token from declaring tools.
- **The brain pulls from the phone.** The phone never listens. A background push that would
  wake it is delivered at the system's discretion, a few an hour at most, and never to an app
  the user force-quit.

## Consequences

- A stolen phone token can write false places, motions, Health totals and phone states into the
  log. It cannot write utterances, confirmations or tool results.
- The log now holds where he went, kept like every other event.
- A phone type added later must be known to the brain first. Until then, a phone that sends it
  gets that event refused.
