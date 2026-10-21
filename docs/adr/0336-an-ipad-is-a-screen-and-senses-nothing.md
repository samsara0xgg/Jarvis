# ADR 0336 — An iPad is a screen and senses nothing

**Status:** Superseded-by-0338
**Date:** 2026-10-21
**Supersedes:** none

## Context

- 2026-10-21 yilun asked for Jarvis on his iPad: the same iOS app, made universal, laid out for
  the iPad's width. He uses the iPad to look at his day and talk to her.
- The phone app reports what it senses to the brain: places and moves (ADR 0197), motion,
  Health, focus, power, headphones, calls, and the day's recording. It answers a silent push that
  asks where he is (`LocatePush`).
- ADR 0212 answers "here" from the device a turn came from. A paired iPad that reported its
  location would make "here" the iPad's: an iPad left at home would say he is at home while he
  is out, and a bus trip would start from the house.
- Health data on an iPad is what iCloud synced from the phone and watch. The phone already
  sends those samples, so an iPad sending them again would duplicate every one.
- An iPad's microphone records only what happens where the iPad lies, not his day.

## Decision

A device reports what it senses only when it is a phone; on an iPad (and any non-phone idiom)
the app starts no collector, records nothing, sends no frames and does not answer a request for
its location.

- The gate is one fact, the device's idiom, checked where sensing starts: the reporter's
  collectors and its queue, the day's recording, and the location push. The settings that switch
  sensing are not shown on an iPad.
- Pairing, talking, the day, the cards that wait on him, attachments and pushes work on an iPad
  as on the phone.

## Alternatives rejected

- **Let the iPad report and have the brain ignore it** — the brain cannot tell an iPad's fix from
  a phone's today (both are paired devices), and every frame would still be sent and stored.
- **Let him switch sensing on per iPad** — a switch left on reproduces the stale "here" of ADR
  0212's context, and Health duplication would follow whichever switch he forgot.
- **Report only Health from the iPad** — every sample would arrive twice, once from the phone
  and once synced, with no key the brain could use to drop the copy.

## Consequences

- An iPad cannot stand in for a lost or flat phone: while the phone is off, nothing reports where
  he is.
- A Mac or any later non-phone device inherits the same rule until a decision says otherwise.
