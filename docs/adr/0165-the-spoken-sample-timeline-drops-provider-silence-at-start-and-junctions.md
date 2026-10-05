# ADR 0165 — The spoken sample timeline drops provider silence at generation start and junctions

**Status:** Accepted
**Date:** 2026-10-04
**Supersedes:** none

## Context

- Every MiniMax segment (speech-2.8-turbo, 32 kHz PCM16, resampled to the 48 kHz
  canonical rate) carries digital silence at both ends, measured on 878 recorded
  segments: leading silence median 182 ms (p90 271), trailing median 261 ms (p90
  383). Segment edges sit at about -120 dBFS.
- Played back to back, each junction inserts about 440 ms of dead air, and the
  first audible word of every answer starts about 180 ms after playback starts.
  A shorter first spoken clause cannot pay off while each break costs that much.
- The silence is not a click source: the per-segment resampler leaves no step at
  a junction (maximum sample step 0.0000 over 99 real junctions, per-segment or
  shared resampler). Only its length is the problem.
- The player ring, the ledger (`PlaybackLedger`) and everything built on it
  count canonical samples written: word alignment rows, the heard prefix, the
  caption position, playback checkpoints and the barge-in "already said" line
  (ADR 0083). Provider word times count from the start of the provider's audio.
- Audio streams in chunks and speech must not wait: nothing above the threshold
  may be delayed or reordered to decide whether the silence after it ends.

## Decision

Cut the provider's silence out of the spoken sample timeline, and let every
sample position mean the audio as written.

Its limits:

- **Only quiet samples are cut.** A stateful per-generation trimmer sits after the
  resampler and before the player write. It removes or holds only samples whose
  5 ms RMS is below -50 dBFS, at most 400 ms at any one cut; everything above the
  threshold is passed on at once, in order, unchanged.
- **Where it cuts.** The leading silence of a segment keeps a short pre-roll (20
  ms) at the start of a generation. A junction keeps a pause set by the punctuation
  that ended the segment: 120 ms after a clause mark, 250 ms after a sentence end
  or no mark, split between the tail of one segment and the head of the next. A
  silence inside a segment is released whole when speech resumes.
- **Positions follow what was written.** Word boundaries move back by the lead
  dropped from their segment; a segment starts where the written audio of the
  previous one ends. Nothing downstream converts: the ledger already counts
  written samples.
- **One switch.** `realtime.streaming_output.trim_silence` off writes the provider
  audio unchanged. Thresholds and lengths are keys beside it. Only the streaming
  provider segments are trimmed; the `say` fallback and the cut-off cue are not
  segments of a trimmed generation.

## Alternatives rejected

- **Trim the finished segment before playback.** It waits for the provider's last
  chunk, which delays the first word by the whole segment's synthesis time; the
  streaming trimmer adds at most one window of lead detection.
- **Fix the junction with one shared resampler.** The step at the junction is
  already 0.0000 per segment; only the silence is audible, so the resampler is
  not the cause.
- **Fixed-length trim of every segment's ends.** The silence ranges from 0 to
  more than 400 ms per segment (p90 271 and 383 ms); a fixed cut either leaves
  silence or removes speech, and it cannot make a comma shorter than a full stop.
- **Start speech sooner with a shorter first clause and keep the silence.** Each
  extra break then costs about 440 ms of dead air (measured), which cancels the
  gain of an earlier first word.

## Consequences

Playback ends sooner by about 340 ms per answer on the recorded set, and the
provider's own pacing at sentence ends is replaced by ours. A tail is held in
memory until speech resumes or the segment ends, at most the pause plus the cut cap
(under 650 ms) of audio, so a long silence inside a segment reaches the ring late
by that much. Provider word times are mapped by one subtraction per segment, so a
provider that timed words from after its leading silence would be mapped early by
the same amount; the existing mapping already counts provider times from the start
of the provider's audio. Fixed thresholds treat
a quiet room tone above -50 dBFS as speech and leave it uncut.
