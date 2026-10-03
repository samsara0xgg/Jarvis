# ADR 0133 — Speech Over Her Is Transcribed from the Beam Channel

**Status:** Proposed
**Date:** 2026-10-02
**Supersedes:** none

## Context

- ADR 0103 gives the reSpeaker XVF3800's channel 1 (a beam with no echo
  processing) to the wake word and leaves channel 0 (echo cancelled and
  noise gated by the board) to every other listener, final ASR included.
- While Jarvis speaks, the board's echo suppressor clips the first tens of
  milliseconds of Allen's speech on channel 0, so aspirated initials vanish.
  2026-10-02, her audio playing and 「退下吧」 spoken from another speaker:
  channel 0 transcribed 「会下吧/会下罢」 3 of 3 times, channel 1 「退下吧」
  3 of 3; with her silent both channels were correct. 「停」 over her was
  heard as 「顶」 the same way.
- Her voice is quiet on channel 1 (rms about 20-50 against speech in the
  thousands), and she ducks to gain 0.2 as soon as his speech is detected
  (ADR 0100), so the beam is clean enough to transcribe.
- Channel 0 is still what VAD, endpointing and barge-in detection are tuned
  on, and what the partial captions read.

## Decision

Final ASR, the hybrid recognizer's prepared pass included, hears channel 1
instead of channel 0 for an utterance that began while she was speaking
(the soft barge-in of ADR 0100), over exactly the frames the channel-0
utterance covers; everything else stays on channel 0.

Limits: it applies only when the device delivers the wake channel; one frame
of the utterance without it gives the whole utterance channel 0, logged once
at info. The utterance's retained recording is what final ASR heard.

## Alternatives rejected

- **Everything on channel 1 over her** — VAD and the barge-in confirmation
  read channel 0; ADR 0103 measured channel 1 as the one where her voice is
  plain, and no run has tuned the detectors on it.
- **Final ASR on channel 1 always** — with her silent both channels
  transcribed correctly, and channel 0 is the noise-gated one; nothing
  measured says the beam is better there.
- **Re-transcribing channel 1 after channel 0 came back wrong** — it adds a
  second Whisper run after the commit, and ADR 0132's prepared pass exists
  to take that wait out.

## Consequences

- ADR 0103's "every other listener reads channel 0" no longer holds for this
  one case; 0103's wake-word decision stands.
- A turn spoken over her is transcribed from audio the detectors never saw,
  so a sound the VAD ended on can have a different transcript than channel 0
  would give.
- The capture ring carries the wake channel beside channel 0 whenever
  `wake_input_channel` is set, doubling that lane's frame storage.
