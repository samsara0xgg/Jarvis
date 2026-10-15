# ADR 0269 — Leaving voice mode keeps a sentence she has not started answering

**Status:** Accepted
**Date:** 2026-10-14
**Supersedes:** 0138

## Context

- ADR 0138 (Allen, 2026-10-03): when he leaves conversation mode from the surface
  she stops talking at once and every answer still being written is cancelled;
  2026-10-04 the exit says nothing back. `POST /inherent/controls` taking the mode
  from on to off calls the voice session's `dismiss()`, which cancels every open
  run of his words as `user_stop` whatever its progress, marks its turn stopped and
  stops what is audible.
- The companion's exit also names the turn it is still waiting on to
  `POST /inherent/cancel-response`, another `user_stop`, for a turn that has no
  answer to name yet.
- Live 2026-10-14 21:18 PT, Mac voice: leaving voice mode and entering it again
  cancelled a sentence he had just spoken, whose turn had not begun to answer. He
  asked the bus question again a few sentences later.
- What the exit has to stop is sound and writing already under way. A sentence whose
  answer has not started has no sound to stop; what the exit takes from him is his
  words and their answer.
- Speech after the exit would contradict ADR 0138: an older answer that spoke 12 s
  after a click exit is what that ADR was written against. Turns on a typed channel
  are already shown and not spoken (ADR 0181), and a run that is not answered at the
  speaker ends its face with `voice spoken` outcome `suppressed`.
- A restart closes every open run (ADR-0008 F14), so a mark on a turn has nothing to
  bind after one.

## Decision

Leaving conversation mode from the surface ends what is audible and every answer
already under way, but not a turn of his words whose answer has not started: that
turn goes on, and its answer is shown on the surface and not spoken.

Limits:

- An answer has started when the turn's non-commentary `surface.response_open`,
  `surface.response_chunk` or `surface.playback_started` is in the log. A wait line
  (a commentary run) is always cancelled and none is said after the exit.
- The turn is marked text only for 120 s in the response registry. The local speaker,
  a voice terminal's speech and the wait-line watcher skip a marked turn, and the
  surface is told `voice spoken` with outcome `suppressed`.
- The companion's exit cuts only the answer on screen. The stop on a poke while she
  speaks and the stop button are unchanged.
- Everything else in ADR 0138 holds: only the on-to-off edge through this route; the
  exit says nothing back; a run is cancelled only when its policy allows, it is not
  for a document alone and its turn started from his words on a channel the speaker is
  not silent for; a device's exit acts on that device's turns (ADR 0219); background
  turns are left alone; a text-only daemon only flips the switch.

## Alternatives rejected

- **Cancel it and let him say it again (ADR 0138 as written)** — 2026-10-14 21:18, a
  spoken sentence was lost and repeated.
- **Let the turn answer aloud** — she would talk with voice mode off, which is the
  12 s late answer ADR 0138 was written against.
- **Cancel the playing answer only and keep the companion's `stopTurn`** — the turn
  stop is a `user_stop` of the very turn, so the sentence is lost whatever the daemon
  spares.
- **Record the text-only mark in the Event Log** — it needs a new event type and a read
  in every speech consumer; a restart closes the run it would bind, so a durable mark
  has nothing to outlive.

## Consequences

- A sentence spoken just before the exit answers on the screen, with her face not
  speaking; if he enters voice mode again before it arrives it is still text only.
- A turn that has started but not yet opened its run is still not named by the exit
  (ADR 0138's gap): its answer is spoken.
- The mark lives in the brain's process: a voice terminal's exit reaches it through
  `cancel_runs`, and a terminal speaks only what the brain's speech rows allow.
- An answer that has opened but not yet played is cancelled at the exit, as before.
