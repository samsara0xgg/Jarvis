# ADR 0256 — The Mac records his day from the microphone it already holds

**Status:** Accepted
**Date:** 2026-10-14
**Supersedes:** none

## Context

- ADR 0238 keeps his recorded day on the brain, piece by piece, from a paired phone.
- On 2026-10-14 Allen turned the phone's recording off: while it ran, people on a Discord voice
  call on that phone could barely hear him. iOS gives the microphone to one recording session
  at a time, so a phone that records all day competes with every call app on it.
- At home he sits at the Mac, and the reSpeaker is plugged into it. The Mac terminal already
  holds the one audio ingress (ADR 0172) for wake, VAD and recognition, and keeps it open while
  it listens. macOS lets other apps open the same microphone at the same time.
- He chose the Mac at 01:08Z: 「mac吧 mac就行 我现在respeaker一般链mac」.
- The Mac's disk is nearly full. macOS ships `afconvert`, which encodes Opus in CAF and AAC in
  MPEG-4, both containers the archive takes.
- The ingress's canonical lane is 16 kHz mono (channel 0), read by every listener. Echo
  cancellation is applied later, only on the capture path.

## Decision

A listening terminal whose own config turns `lifelog_capture.enabled` on records its canonical
microphone lane into the brain's archive, in pieces cut at each ten-minute mark of the wall clock.

- It reads a diagnostic lane of the ingress it already holds, so it never opens the microphone
  a second time and never takes it from another app.
- A piece also ends at a gap in the stream and while the microphone is muted; nothing is kept
  while muted.
- A piece waits in the terminal's outbox until the brain's SHA-256 of it matches, so the Mac
  holds only what has not reached the brain. A piece the brain refuses for good is held, not
  deleted.
- The archive files it under the terminal's paired name, beside the phone's pieces.

## Alternatives rejected

- **Keep recording on the phone and fix the clash** — iOS lets one recording session hold the
  microphone; every call app on the phone would keep pausing it or being degraded by it, as
  the Discord call on 2026-10-13 was.
- **A separate recorder process on the Mac** — it would open the microphone a second time beside
  the terminal's ingress, and the two would disagree about device changes the ingress already
  handles (reopen, route, epoch).
- **Record the native device stream (48 kHz, every channel)** — only the ingress callback sees
  it, and a second consumer there runs on the audio thread. The canonical lane is what the
  recognizer already transcribes well.
- **Store on the Mac and copy later** — the Mac's disk is nearly full, and a second copy of the
  archive drifts from the first.

## Consequences

- The Mac records only while the terminal listens: the lid closed, the terminal stopped or
  listening off means no recording, and nothing marks that gap except the missing pieces.
- 16 kHz mono loses what the phone's stereo would keep (direction, music above 8 kHz).
- When he is at the Mac with the phone also recording, the same hour is kept twice, once per
  device; telling them apart is left to whatever reads the archive.
- Discord's own voice on the Mac and her own voice from its speakers are recorded as heard.
