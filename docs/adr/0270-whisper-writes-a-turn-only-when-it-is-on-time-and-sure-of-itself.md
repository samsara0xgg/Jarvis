# ADR 0270 — Whisper writes a voice turn only when it is on time and sure of itself

**Status:** Proposed
**Date:** 2026-10-14
**Supersedes:** none

## Context

- ADR 0132 chose the `hybrid` final recognizer: for 1 s of speech or more, Whisper's text
  replaces SenseVoice's whenever it is non-empty. Nothing checks how sure Whisper was, and the
  commit waits for its pass with no limit. ADR 0150 gave its passes a word list and ADR 0137 a
  second hearing of short unclear lines; both keep that rule.
- Three turns of 2026-10-10..14 waited 10.6, 13.2 and 13.4 s for their words (the 10-14 21:18
  one Allen noticed). In `terminal.err.log` each had a silent warm-up pass ahead of it holding
  the one Whisper lock, ending 7.6, 12.0 and 12.6 s after SenseVoice had written the turn, with
  511 s, 20549 s and 8241 s since the last Whisper pass. A hot pass takes 0.43 s at the median
  and 1.6 s at the most (696 passes over the retained turns of 1 s or more, replayed 2026-10-14).
- The cause is the model being read back from swap. The live terminal's 3.1 GB of GPU buffers
  were swapped out (`vmmap`; resident size 111 MB), with the Mac at 12.0 of 13.0 GB of swap. In
  five controlled runs of the same model in a fresh process, left idle 64-120 s, the buffers were
  swapped out in 2 and the next pass took 1.0 s against 0.35 s hot; whether it happens depends on
  the pressure at that moment. One of those two had an MLX wired limit of 4 GiB set, which did not
  stop it.
- Whisper's words are not always better. Of the 671 retained turns where both engines wrote
  text, those whose worst segment averaged a log-probability under -1.5 numbered 12; against a
  third decode (Whisper large-v3, full decoder) SenseVoice was nearer in 6, neither was in 5,
  Whisper was in 1. At -1.0 (Whisper's own retry threshold) 31 turns fall under it and the split
  is 10 to 9. mlx returns `no_speech_prob` 0 for all 696 passes, and the only turn with both
  texts over a compression ratio of 2.4 had Whisper right.
- `utterance.received.confidence` is SenseVoice's fixed 0.9 or 0.1, or Whisper's mean of
  `exp(avg_logprob)` scaled by speech probability, whichever engine won. The scales cannot be compared, and nothing
  reads the field for a decision.

## Decision

In `hybrid`, let Whisper's words replace SenseVoice's only when its pass is done within 4 s of
the commit and its worst segment averaged at least -1.5; otherwise SenseVoice's words stand.
Record on `utterance.received` which engine's words stand and why, with each engine's own score
under its own name, and read the Whisper model's weights every 15 s of idleness for the 5
minutes after voice was last used.

- **Gate.** A poor pass is left out even when SenseVoice wrote nothing, so the turn is then empty.
  2026-10-14 22:14:46, over music, SenseVoice wrote nothing and Whisper (confidence 0.203, one
  segment) wrote a music credit line. Of the 5 retained turns where SenseVoice wrote nothing and
  Whisper did, all had healthy passes (worst average above -0.5). A credit line of the form
  "作曲 name" joins the transcripts the existing silence filter already drops.
- **Bound.** The 4 s runs from the commit and is 2.5 times the longest hot pass. A pass that has
  not finished cannot be interrupted, so it runs to its end; the next passes queue behind it.
- **`confidence`** keeps its meaning (the writing engine's own score) and is comparable only
  within one `asr_engine`; the two named scores are the ones to compare.

## Alternatives rejected

- **An MLX wired limit to keep the model resident** — tried 2026-10-14: in 1 of 2 runs the
  buffers were swapped out anyway within 64 s (next pass 1.04 s against 0.35 s).
- **A gate at -1.0, Whisper's own retry threshold** — 31 of 671 turns flip and SenseVoice is
  nearer in 10 against 9 for Whisper; it would rewrite as many good turns as bad ones.
- **A gate on `no_speech_prob` or the compression ratio** — the first is 0 in all 696 passes,
  the second flips 1 turn and Whisper was right in it.
- **Reading the weights only when Allen starts talking (today's `warm`)** — the pass queues
  behind that warm-up, which is what took 7.6-12.6 s.
- **One common confidence scale** — SenseVoice's value is a two-level heuristic (0.9 or 0.1) with
  no probability behind it, so there is nothing to map Whisper's onto.

## Consequences

- A turn that times out or is gated is written by SenseVoice, which mis-hears numbers and names
  (ADR 0132). Allen sees that as an occasional worse transcript where he used to see a long wait.
- Reading the weights is about 0.31 s of GPU every 15 s, 2% of one GPU, only while voice is in
  use: the 41 gaps between Whisper passes of 2026-10-10..14 were 60 s or less inside a
  conversation and over 600 s between them, so 5 minutes after the last speech or pass covers
  every gap inside one, and the thread ends after that until Allen next speaks. It does not warm
  the first turn after a long silence; the bound covers that turn. Its battery state is not read.
  Its benefit was not reproduced under pressure; the count of `whisper_timeout` turns is how to
  tell whether it holds.
- The gate rests on 12 turns judged by a decode of the same model family. It should be re-run
  with `tools/hybrid_asr_replay.py` when more turns are retained.
- A brain that predates the new fields drops them: a terminal that sends them to it loses
  nothing else.
