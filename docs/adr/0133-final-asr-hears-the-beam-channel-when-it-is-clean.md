# ADR 0133 — Final ASR Hears the Beam Channel When It Is Clean

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
- It is not only her voice: with music playing on the Mac, 「退下」 was heard
  as 被下/背下/退下 on channel 0 (2 of 3 wrong) and 退下 3 of 3 on channel 1.
  Channel 0's echo cancellation clips the start of speech whenever the Mac
  plays anything. With silence both channels were right.
- Channel 1 is raw, so it also carries other people's speech. Synthetic TV
  speech mixed into channel-1 clips, with snr the median frame rms of the
  speech frames over that of the others: at snr 5.4-9.4 SenseVoice still
  transcribed only Allen; at 2.3-3.9 it transcribed the TV (「节目到这里就」).
  Music alone gave snr 13-18, silence 50-100.
- Her voice is quiet on channel 1 (rms about 20-50 against speech in the
  thousands), and she ducks to gain 0.2 as soon as his speech is detected
  (ADR 0100), so over her the snr is high.
- Channel 0 is still what VAD, endpointing and barge-in detection are tuned
  on, and what the partial captions read.

## Decision

Final ASR, the hybrid recognizer's prepared pass included, hears channel 1
instead of channel 0 for every utterance whose beam is clean enough, over
exactly the frames the channel-0 utterance covers. Otherwise it hears
channel 0 as before.

Clean enough: beam snr of at least 5.0. Speech is the median per-frame rms
of the beam frames the channel-0 VAD marked as speech; background is the
median of the others (pre-roll, pauses, tail). No other frames, or a silent
background, counts as clean; no speech frames gives channel 0.

Limits: it applies only when the device delivers the wake channel; one frame
of the utterance without it gives the whole utterance channel 0, logged once
at info. Each committed utterance logs which channel final ASR heard and the
snr. The utterance's retained recording is what final ASR heard.

## Alternatives rejected

- **Everything on channel 1** — VAD and the barge-in confirmation read
  channel 0; ADR 0103 measured channel 1 as the one where her voice is
  plain, and no run has tuned the detectors on it.
- **Channel 1 over her only** — music on the Mac clips channel 0 the same
  way, with her silent.
- **Channel 1 always, with no snr gate** — TV speech at snr under 4 replaced
  his words.
- **Re-transcribing channel 1 after channel 0 came back wrong** — it adds a
  second Whisper run after the commit, and ADR 0132's prepared pass exists
  to take that wait out.

## Consequences

- ADR 0103's "every other listener reads channel 0" no longer holds for final
  ASR; 0103's wake-word decision stands.
- A turn is transcribed from audio the detectors never saw, so a sound the
  VAD ended on can have a different transcript than channel 0 would give.
- The threshold of 5.0 sits between two small measured groups (3.9 and 5.4),
  not a tuned value; in a noisy room final ASR falls back to channel 0.
- The capture ring carries the wake channel beside channel 0 whenever
  `wake_input_channel` is set, doubling that lane's frame storage.
