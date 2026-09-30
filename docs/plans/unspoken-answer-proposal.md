# ADR NNNN — An Answer Let Go Before It Played Is Recorded as Never Spoken

**Status:** Proposed
**Date:** 2026-09-30
**Supersedes:** none

## Context

- The next turn's state says where the last spoken answer stopped, read from
  the playback rows L5 writes for it (`surface.playback_started` and its
  checkpoint and terminal). No playback row reads as heard whole: that is
  also what a typed turn, or a boot without TTS, leaves.
- L5 lets an answer go without ever giving it a lease in three places: a stop
  that names an answer parked while Allen talks (ADR 0053) or queued behind
  another; a foreground grant that displaces the queued successors; and L3
  ending the run while the answer is still buffering. The answer is already on
  screen and in memory.db, and nothing durable says it was never heard; only
  the companion's `spoken … dropped` broadcast, which is not in the Event Log.
- The next turn then treats words Allen never heard as said, and the model
  refers back to them (handoff §3.1 item 5, open since `69f3fae`).
- A playback row needs a session and a generation. An answer that never
  reached a lease has neither, so the existing terminals cannot carry this.

## Decision

When L5 lets a registered answer go before any of it played, append one
`surface.speech_dropped` row naming the response, its turn and why; the
conversation projection marks that answer unspoken, and the next turn says it
was never spoken aloud and was only shown on screen.

Its limits:

- Only an answer that never started: once a lease exists, the playback
  terminal and its cursor say what was heard, as before.
- The look-back goes on past an unspoken answer, the way it goes past one that
  started without a sample, so the answer Allen last heard is still named.
- The row is best effort: letting the answer go never waits on it or fails
  because of it.
- A power transition that clears the queue writes none; it is not a turn Allen
  is still in.

## Alternatives rejected

- **Infer it in L3 from the missing playback rows on a voice turn.** A boot
  without a MiniMax key has no playback rows for any answer, so every voice
  turn would be told the last answer was never spoken while Allen read it.
- **Write `surface.playback_failed` for it.** Its schema requires a session
  and a playback generation that a never-activated answer does not have; a
  made-up generation would enter the generation-ownership checks
  `PlaybackHistory` runs on every playback row.
- **Leave it to the companion's `dropped` broadcast.** It is not durable: a
  daemon restart, or a turn folded from the Event Log alone, never sees it.

## Consequences

- One more L5 event type in the Event Log, written on every stop of a waiting
  answer and every displacement.
- Two model-facing lines can now stack: never spoken, then where the answer
  before it was cut.
- An answer dropped by an older build, before this row existed, still reads as
  heard whole.
