# ADR 0129 — Her voice renders in a native helper process

**Status:** Proposed
**Date:** 2026-10-02
**Supersedes:** none

## Context

- `AudioStreamPlayer._callback` (`jarvis/surface/voice_tts.py`) is Python code
  that CoreAudio calls every IO cycle from inside the daemon, so it needs the
  GIL every cycle. Whenever another daemon thread holds the GIL for longer than
  one cycle, the callback misses its deadline, coreaudiod skips the cycle and
  Allen hears a pop or a stutter.
- 2026-10-02: `log show` printed `python3.12 (the daemon's pid):
  HALC_ProxyIOContext::IOWorkLoop: skipping cycle due to overload` about 85 ms
  before Allen heard a stutter. Raising the block to 2048 frames and the host
  latency to 0.12 s (the 120 ms host buffer commit) reduced the overloads and did
  not remove them.
- PortAudio cannot see the miss: the player's `underflow_count` and
  `starvation_gaps` were 0 through every overload below.
- The player is two halves. The ledger, leases, heard-prefix accounting and
  the media actor decide what was heard and must stay where the event log is.
  The realtime half is a sample ring, a gain ramp, declicks and one callback.

## Decision

Render her voice in `native/voice_out/main.swift`, a helper process the daemon
spawns and talks to over stdin and stdout, behind
`realtime.streaming_output.native_player` (default `false`); the Python
`NativeAudioStreamPlayer` inherits everything else from `AudioStreamPlayer`.

- **The callback is a line-for-line port**, not a redesign: hold, generation
  discard boundary, gain ramp with audibility classes, declick, tail ramp
  (ADR 0006 D12), head ramp after starvation, starvation counting and
  `first_for_generation` behave as in `_callback`. The render thread uses
  atomics and preallocated rings only; a reader thread fills the sample ring
  and a writer thread drains the report ring.
- **Python's accounting does not change.** The helper sends one REPORT per
  callback that played a generation's samples, with the callback time and the
  presentation delay (`mHostTime - now` plus the device's latency, safety
  offset and stream latency, plus the block's own duration, because the ledger
  compares the end cursor). `poll_presentation` consumes them unchanged.
- **Interrupts stay exact.** `ACTIVE(-1)` then `DISCARD(seq)`; the helper acks
  the first callback after applying the boundary, so every report for
  discarded samples precedes the ack and `settle_interrupted_generation`
  freezes only after it.
- **Failure leaves her voice on.** The helper exits when stdin closes. A build
  or start failure, or echo cancellation being on (v1 has no playback tap),
  keeps the Python player. A helper that dies later ends the lease and
  `is_running` goes false; `restart()` and the device-refresh path
  (`stop`, `set_device`, `start`) bring it back.

## Alternatives rejected

- **A bigger Python buffer.** At 2048 frames and 0.12 s the Python player lost
  20 cycles to 22 GIL holds of 169 ms (`tools/voice_out_stall_check.py
  --stall c`, 20 s into BlackHole 16ch, 2026-10-02) and the live daemon was
  still overloading that day. Surviving a hold of that length needs a host
  buffer past 170 ms, which is added to every barge-in stop. Refuted if the
  same run shows 0 skipped cycles at a buffer under 100 ms.
- **A playback-only Python process.** It runs the same callback, so any pause
  of its own GIL (the pipe-reader thread, garbage collection) skips the same
  cycles; moving the process removes the other threads, not the dependency.
  Not measured: refuted if the stall check, run in such a process with its
  collector active for an hour, shows 0 skipped cycles.
- **Shared memory instead of pipes.** The stream is 48 kHz mono float32,
  192 KB/s, one 32768-sample frame at most per write; a pipe carries that
  without a second synchronization scheme between the processes. Refuted if the
  daemon's pipe writes ever block the media actor longer than a cycle.

## Consequences

- The helper is built with `swiftc -O` on the first start (a missing or older
  binary), so the machine needs the Swift toolchain; the binary lives in
  `native/voice_out/.build/` and is not committed.
- With echo cancellation on, the Python player stays: the helper cannot feed the
  canceller's far end. A native far-end tap is a separate decision.
- `underflow_count` now means CoreAudio's own processor-overload notifications
  for the device, which is what the Python count could never see.
- A helper that dies mid-answer drops that answer's remaining audio; the heard
  prefix up to the last report stays correct.
- The helper opens its device by name when it starts, so `refresh_devices`
  re-resolves it through `stop`, `set_device`, `start`; PortAudio's
  reinitialization no longer touches her output.
