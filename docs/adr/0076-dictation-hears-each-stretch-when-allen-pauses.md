# ADR 0076 — Dictation Hears Each Stretch When Allen Pauses

**Status:** Accepted
**Date:** 2026-09-27
**Supersedes:** none

## Context

- ADR 0058's dictation heard the whole recording with SenseVoice only after
  the stop, then polished it. On the 60-clip set of 2026-09-26 (Allen's own
  Typeless recordings, 2 to 107 s, graded against what he sent), that decode
  took 0.10 / 0.32 / 0.95 s at the median for 2-10 / 10-40 / 40+ s clips,
  1.52 s at p90 on the long ones and 3.8 s for the 107 s clip, all of it
  after his second tap. The polish after it took 0.5 to 0.8 s.
- SenseVoice is an offline model: it hears a finished piece of audio, and a
  piece cut through a word loses that word. Allen pauses between phrases,
  and Silero already marks speech in 32 ms frames on the daemon's ingress.
- The recognizer's no-speech gate compared the mean level of the whole
  recording with 0.01. A dictation that holds a long pause averages his words
  under it: 2 of the 60 clips (20 and 30 s) came back blank. Typlus had the
  same fault and moved to its loudest window on 2026-09-12.
- On 2026-09-27 Allen asked for recognition while he talks, in Jarvis first,
  and for it to be well tuned.

## Decision

While Allen dictates, cut the recording at each half-second pause once the
open stretch is at least 5 s long and holds speech, hear that stretch at once,
one stretch at a time in order, and at the stop hear only what is left; a
stretch has no words when its loudest 0.2 s is under SenseVoice's floor, and
the joined words are judged empty or too short as a whole.

## Alternatives rejected

- **Whole recording after the stop (ADR 0058 as built)** — same accuracy
  (46 of 60 judged wrong both ways by gpt-5.6-sol, two blind passes), but
  0.95 s median and 1.52 s p90 left after the stop on 40+ s clips against
  0.05 s and 0.09 s.
- **Cut after 3 s instead of 5 s** — character error rate against what he
  sent was 0.147 / 0.328 / 0.432 against 0.149 / 0.322 / 0.431: no better on
  the long clips it is for, with more cuts.
- **Cut at 0.8 s pauses** — 0.154 / 0.326 / 0.436, worse in every length.
- **Keep the mean-level gate per stretch** — a stretch that holds a 35 s
  silence averaged 0.0069 and its sentence was dropped; long-clip error rate
  0.446 against 0.431 with the loudest window.

## Consequences

- A stretch is decoded without the words after it, so punctuation and
  number formatting at a cut can differ from a whole decode: replaying the
  60 clips through the session, 12 differ from the offline cut by one to
  three characters at a cut, with the same total error against what he sent.
- The loudest-window gate lets through a stretch whose only sound is one
  loud noise; the joined-words rule still drops a single character.
- Dictation runs its own Silero instance beside the voice session's, and
  SenseVoice's lock is now taken a stretch at a time during the recording
  instead of once after it.
- The polish connection is opened when the session starts and again at a
  pause when the last opening is over 3 s old, not at the stop, which would
  now come too late to save the handshake.
