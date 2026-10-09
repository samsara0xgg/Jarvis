# ADR 0193 — The echo canceller pairs her voice and the mic by host time

**Status:** Accepted
**Date:** 2026-10-09
**Supersedes:** none

## Context

- Over the laptop speakers WebRTC AEC3 (`jarvis/surface/voice_aec.py`) removes about 50 dB of her
  own voice from the microphone most of the time, but leaks it in bursts of 0.2-1.5 s, and those
  bursts are judged as Allen barging in.
- Until now the far end was paired with the mic by arrival: each 10 ms mic chunk was fed with
  whatever far end had reached the canceller so far. The far end comes from the helper's RENDERED
  frames (ADR 0129), read and tapped by one Python thread (`voice_native_out.py`) that stalls on the
  GIL for 50-100 ms or more, while the ingress worker keeps cleaning mic chunks. A far end that
  arrives late, relative to the mic chunks that hold its echo, makes AEC3 underrun and shift its
  delay alignment. Replaying recorded mic and far-end clips: far end fed early in bursts (the
  worker stalls) was fine, far end fed late leaked for 0.5-1.5 s each time.
- A mic discontinuity (PortAudio `input_overflow`) rebuilt the whole APM, whose reconvergence is a
  further leak of about 1 s.
- Every clock involved is the same mach one: the helper's `CLOCK_UPTIME_RAW` and the render
  callback's `mHostTime`, Python's `time.monotonic_ns()`, and PortAudio's ADC and DAC times.
  Verified on this Mac: `inputBufferAdcTime` sits about 33 ms before `monotonic_ns()` in the input
  callback.
- AEC3 finds a delay by itself but only up to a limit: in the synthetic room of
  `tests/integration/test_voice_aec.py` (60 ms room delay) a lead of 100 ms still cancels 39 dB,
  a lead of 150 ms only 9.8 dB.

## Decision

Pair the far end and the mic 1:1 by host time, independent of when Python threads run: each 10 ms
mic chunk is fed with the 10 ms of far end that sounds `LEAD` (60 ms) after the chunk starts, cut
from stamped blocks, and the mic waits, at most 300 ms, for that far end when it is late and the
player is alive.

- Each far-end block carries the host time its first sample leaves the speaker (the helper's
  render timestamp plus the device latency, the same value its REPORT frames use; the Python
  player's `outputBufferDacTime`). Each canonical mic frame carries the ADC time of its first
  sample.
- Missing or dropped far end is zeros. A stamp within 2 ms of where counting samples puts it is
  jitter or clock drift, not a gap, so blocks that follow each other stay sample-exact; a bigger
  jump, on either side, splices both streams at that instant.
- "Alive" means a block arrived in the last 500 ms; a player that stopped costs the mic no wait.
  After one wait times out, the mic does not wait again until the far end has caught up.
- A new APM is built only for a new stream epoch. A discontinuity splices; it does not restart.
- With no ADC time (replay, test backends) the first frame's arrival time stands in for it and the
  stream is followed by counting samples.
- The diagnostics dump's played channel is the far-end chunk fed with each mic chunk, and the
  "echo diagnostics written" log line counts waits, the longest, timeouts, gaps and restarts.

## Alternatives rejected

- **Feed on arrival, as before** — in the synthetic room with the far end 200 ms late in one
  burst mid-echo, the echo-only stretch is reduced 7.8 dB (worst 200 ms: 0.1 dB); with the mic
  losing 100 ms mid-echo, 15.0 dB (worst 200 ms: 1.5 dB). With the pairing by host time: 37.1 dB
  and 36.1 dB.
- **A fixed delay by sample count** — counting samples drifts against the real clocks and breaks
  at the first gap on either side, which is exactly where the leaks come from.
- **Apple VoiceProcessingIO** — the 2026-09-05 spike on this MacBook (ADR 0006) reduced echo by
  only 7.5 dB, and mixes cancellation with its own AGC.

## Consequences

- The mic path can wait up to 300 ms inside `clean()` while the reader thread stalls; the ingress
  worker's other duties (fault and route polling) wait with it. Longer stalls feed zeros and are
  counted as timeouts.
- `LEAD` is a constant tied to AEC3's delay range: raising it to 150 ms costs cancellation,
  lowering it below the helper's worst stamp error brings back echoes that precede their far end.
- A drift beyond 2 ms between a stamp and its sample count moves the far end by that much at once
  (50 ppm of clock drift reaches it in 40 s); the gap counter shows if the stamps jitter more than
  that.
