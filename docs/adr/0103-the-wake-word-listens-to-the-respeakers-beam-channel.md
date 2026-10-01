# ADR 0103 — The Wake Word Listens to the reSpeaker's Beam Channel

**Status:** Accepted
**Date:** 2026-09-30
**Supersedes:** none

## Context

- The reSpeaker XVF3800 sends two channels at 16 kHz. Channel 0 is echo
  cancelled against what the Mac plays through the Multi-Output Device,
  noise gated and level boosted; channel 1 is a beam without echo
  cancellation. The daemon opened one channel, so the wake word, VAD and
  ASR all heard channel 0.
- 2026-09-30, both channels recorded at once: Allen's three connected
  "Hey Jarvis" scored 0.996, 0.996 and 0.963 on channel 1 and 0.000, 0.449
  and 0.132 on channel 0. Four takes earlier that evening peaked at
  0.06-0.21 on channel 0. Two and a half minutes of room talk on channel 1
  stayed under 0.07.
- The same day a sentence played through the Multi-Output Device was silent
  on channel 0 and transcribed word for word from channel 1; Jarvis's own
  "Hey Jarvis" played that way scored 0.996 on channel 1 and 0 on channel 0.
- Typlus has recorded channel 1 since 2026-09-29: channel 0 is a hot,
  clipping mix.
- The wake loop already drops detections while Jarvis's output is active.
- Allen, 2026-09-30: no false wakes, so openWakeWord stays out (ADR 0042).

## Decision

The wake word reads `realtime.single_audio_ingress.wake_input_channel` when
the device has that channel, and every other listener (VAD, capture, ASR,
echo diagnostics) reads channel 0. The stream opens with as many channels as
that needs and the device has, so a mono microphone opens mono and the wake
word hears its one channel. The shipped default is null (channel 0 only);
Allen's settings set 1 for the reSpeaker.

## Alternatives rejected

- **Everything on channel 1** — Jarvis's voice is plain on channel 1, so in
  conversation mode she would barge in on her own answers.
- **Channel 1 through the daemon's software echo cancellation** — it only
  cancels the daemon's own player, not the other Mac audio the board
  cancels, and on channel 0 its output scored Allen's takes lower than the
  raw signal (0.06 against 0.21).
- **Reprogramming the board's channel routing** — it needs Seeed's host tool
  and changes the board for every app, Typlus included.
- **A lower wake threshold on channel 0** — most of Allen's missed takes
  scored under 0.2, and a phone video reached 0.950.

## Consequences

- The room's echo of Jarvis after she stops reaches the wake word
  unfiltered; how often it wakes her is unmeasured.
- False wakes on channel 1 are measured on two and a half minutes of room
  talk only; the daemon log's `wake score` lines are where more shows up.
- With the setting on, a device whose channel 1 carries nothing leaves the
  wake word deaf.
- A stream the ingress has to resample gives the wake word channel 0.
