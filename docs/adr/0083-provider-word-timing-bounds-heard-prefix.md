# ADR 0083 — Provider Word Timing Bounds the Heard Prefix

**Status:** Accepted
**Date:** 2026-09-29
**Supersedes:** none

## Context

A counting answer can contain fifty numbers in one synthesis segment. A
whole-segment cursor cannot distinguish interruption after fifteen from
interruption before any speech. Dropping the gain later in that segment
also discards words already played at normal gain. Allen asks the next
answer to know where the spoken count stopped.

MiniMax streaming subtitles supply source-text offsets and word-end times.
Live observations show cumulative updates within blocks, repeated source
spans for numeric syllables, and revisions to the last unfinished token.
Provider synthesis progress is not evidence of speaker playback.

## Decision

Admit a partial-segment heard prefix only at a validated, settled provider
word boundary behind the player's audible sample horizon and before the
first uncertain-gain interval, with the mapping persisted before the
cursor. Keep the existing full-segment path when timing is unavailable.

Distinguish an empty confirmed prefix with submitted audio from no audio,
and playback completion with uncertain audibility from interruption. A
later answer cancelled before its first sample must not hide the last
audible answer from the next turn.

## Alternatives rejected

- **Estimate text position as a fraction of duration.** Numerals, pauses,
  punctuation and speech rate do not have uniform sample lengths.
- **Split every number into a synthesis request.** Fifty separate requests
  add latency and do not solve interruption in ordinary long sentences.
- **Accept the newest subtitle tail immediately.** Live updates revise
  that token's end and can repeat its source span for another syllable.

## Consequences

Word evidence needs an additional durable mapping and remains dependent
on the provider's alignment quality. Missing or inconsistent timing keeps
the position conservative rather than reconstructing it from the written
answer. Quiet speech after ducking is not promoted to confirmed hearing.
