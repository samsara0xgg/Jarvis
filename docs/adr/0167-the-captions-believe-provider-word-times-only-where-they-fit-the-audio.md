# ADR 0167 — The captions believe a provider's word times only where they fit the audio

**Status:** Accepted
**Date:** 2026-10-05
**Supersedes:** none

## Context

- Allen, 2026-10-05: the lit words run ahead of her voice. ADR 0112 lights them
  from the output ledger, which maps each spoken segment onto the sample clock
  with the word ends MiniMax returns, and carries them on at a pace estimate
  between reports.
- Measured on 449 kept segments (kept TTS audio against the stored
  `surface.playback_alignment` rows): MiniMax stamps a word with the time it
  starts, not ends. The first "end" of a segment is the voiced onset, and across
  syllable-consistent Mandarin segments the stamp matches the word's start (median
  +45 ms) and misses its end by a median -140 ms, on every word.
- 111 of 406 segments (27%) with four or more words are squeezed: the stamps run
  a frame (2048 samples at 48 kHz) apart and say the whole segment in under half
  its voiced duration (one segment: 17 characters stamped within 1.15 s, voice
  3.3 s). The ledger then reports the segment played seconds early, and the
  surface never steps lit words back. Frame spacing does not tell them from
  good segments (nearly every stamp sits on that grid); only the span against the
  segment's audio does.
- The first report was sent when the segment opened, about 0.4 s before any
  sound, and the surface starts its pace estimate from a report.

## Decision

The ledger keeps word ends only from provider times that fit their segment's
audio, read as word starts, and reports no place before her first sound.

Its limits:

- A segment's word times are judged when its audio is complete: with four or
  more words, if the last starts before 60% of the written segment, the segment
  keeps no word ends and the surface paces across it as it does without them.
- Otherwise word i ends where word i+1 starts, and the last word ends with the
  segment.
- Until the audible horizon of the generation moves, the report says played 0
  and ahead 0, which the surface already reads as nothing lit and nothing paced.
- Word ends are therefore recorded at segment end, not per provider chunk; a
  barge-in before that keeps the whole-segment heard prefix of ADR 0083.

## Alternatives rejected

- **Reject on word-end spacing alone** — 281 of 295 first segments sit on the
  same 2048-sample grid, squeezed or not; the rule would reject almost none.
- **Judge each chunk as it arrives** — a chunk holds a few words and no audio
  length, and the squeeze shows only against the finished segment: at its end the
  60% rule rejects 111 of 406 segments, 110 of them squeezed by an independent
  span-against-voice measure, and misses none.
- **Raise the surface pace constant** — it cannot absorb the first-sound wait
  and does nothing for jumps the ledger itself reports.

## Consequences

- A rejected segment has no per-word steps: its words advance at the pace
  estimate, capped at the segment end, as ADR 0112 does for a segment without word ends.
- Word ends reach the ledger after the segment's audio has all arrived, about
  half a second into a streamed segment; a barge-in in that first half second
  keeps no word-level heard prefix for that segment.
- The last word of a segment lights with the segment's closing silence (up to
  the junction pause of ADR 0165) rather than with its voiced end.
