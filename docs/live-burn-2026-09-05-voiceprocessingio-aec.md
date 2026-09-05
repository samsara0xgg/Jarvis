# Live burn — 2026-09-05 (ADR-0006 D9 VoiceProcessingIO AEC spike)

The measurement ADR-0006 D9 blocks on, taken once on this MacBook. One real
acoustic run per configuration: built-in speakers → air → built-in microphone,
with `inputNode.setVoiceProcessingEnabled(true)` on one capture and left off on
the other. Software spike only — nothing under `jarvis/` changed, no backend
was wired, no hardware was bought.

## Invocation

```
# far-end material: no *.wav exists in the repository, so one was generated
# through the shipped MiniMax TTS path (MiniMaxWSClient, 32 kHz in → 16 kHz out)
# into ~/.jarvis-lane-b-test/aec-spike/far-end.wav — 15.91 s, 16000 Hz 1ch 16-bit.

osascript -e 'output volume of (get volume settings)'   # 56  (captured)
osascript -e 'set volume output volume 25'              # household-noise bound

swift scripts/spike_voiceprocessingio_aec.swift \
  --far-end ~/.jarvis-lane-b-test/aec-spike/far-end.wav \
  --out     ~/.jarvis-lane-b-test/aec-spike/capture-aec-off.wav \
  --aec off --lead 2.0 --tail 2.0

swift scripts/spike_voiceprocessingio_aec.swift \
  --far-end ~/.jarvis-lane-b-test/aec-spike/far-end.wav \
  --out     ~/.jarvis-lane-b-test/aec-spike/capture-aec-on.wav \
  --aec on --lead 2.0 --tail 2.0

PYTHONPATH=. .venv/bin/python scripts/spike_voiceprocessingio_aec.py \
  --aec-off ~/.jarvis-lane-b-test/aec-spike/capture-aec-off.wav \
  --aec-on  ~/.jarvis-lane-b-test/aec-spike/capture-aec-on.wav

osascript -e 'set volume output volume 56'              # restored, in a trap EXIT
```

Both `swift` invocations exited 0, played 15.97 s of wall audio each (under the
20 s bound) and wrote a 19.89 s capture at 16000 Hz 1ch 16-bit — the WAV shape
`FileReplayBackend` already enforces, verified by constructing one over the
capture. Route, held by a `trap EXIT` guard and re-read afterwards: output
`MacBook Pro Speakers`, input `MacBook Pro Microphone`, before, during and
after. `BlackHole 16ch` is a separate device in the same listing and was
neither leg; a digital loopback carries no acoustic path and cannot produce the
echo D9 asks about. Volume 56 → 25 → 56.

The far-end plays through the engine's **own** output node, not a second
process, so voice processing has a reference signal to subtract.

## Numbers

Windows are the sample offsets the capture host measured, not guesses: silence
lead-in `[0, 29710)` / `[0, 31076)`, playback `[29710, 285910)` / `[31076,
285910)` at 16 kHz.

| Metric | AEC off | AEC on | Δ (on − off) |
|---|---|---|---|
| Playback-window RMS | −37.68 dBFS | −45.20 dBFS | **−7.52 dB** |
| Silence-window RMS | −51.48 dBFS | −76.69 dBFS | −25.21 dB |
| Residual echo (playback − silence) | **+13.81 dB** | **+31.49 dB** | **+17.69 dB** |
| False candidates, `record` (prob ≥ 0.4, dB ≥ −45) | 6 over 16.01 s = **22.48/min** | 2 over 15.93 s = **7.53/min** | −14.95/min |
| False candidates, `tts` (prob ≥ 0.5, dB ≥ −22) | 0 over 16.01 s = **0.00/min** | 0 over 15.93 s = **0.00/min** | 0 |

**The residual figure inverted, and that is the finding, not a bug.** D9's
residual metric is playback RMS minus silence RMS. Voice processing lowered the
echo by 7.5 dB but lowered the idle noise floor by 25 dB, so the ratio got
*worse* while the absolute echo got better. A ratio against the canceller's own
suppressed floor is not an echo-return-loss measurement. A future
`VoiceProcessingIOBackend` card should score absolute far-end level in the mic
against a fixed reference, not against the silence window.

Against D9's ≤ 0.5/min false-candidate target (`docs/adr/0006-full-duplex-voice-session.md`
§ D9 thresholds): the `record` profile misses it by 15× even with the canceller
on. The `tts` profile — the one that actually runs while Jarvis is speaking —
was already at 0/min *without* the canceller, because its −22 dB energy gate
sits 15 dB above the loudest echo this run produced at 25% output volume.

## Format facts

| Fact | AEC off | AEC on |
|---|---|---|
| `inputNode` format before enabling | 48000 Hz, 1 ch, `pcmFormatFloat32` | 48000 Hz, 1 ch, `pcmFormatFloat32` |
| `inputNode` format in this state | 48000 Hz, 1 ch, `pcmFormatFloat32` | 48000 Hz, **9 ch**, `pcmFormatFloat32` |
| Changed by voice processing | no | **yes** |
| Tap buffer format | 48000 Hz, 1 ch | 48000 Hz, 9 ch, deinterleaved |
| 16 kHz mono int16 still obtainable | **yes** | **yes** |
| `isVoiceProcessingAGCEnabled` | false | **true** (its default) |
| `isVoiceProcessingBypassed` | false | false |
| Engine IO `deviceID` | 246 | 135 |
| `AVCaptureDevice.default(for: .audio)` | MacBook Pro Microphone / `BuiltInMicrophoneDevice` | same |

Two platform facts a backend card will need:

1. **Enabling voice processing after the main mixer is implicitly created
   fails.** `engine.start()` returns
   `-10875 … failed call=err = PerformCommand(*outputNode, kAUInitialize, NULL, 0)`
   when `setVoiceProcessingEnabled(true)` runs before the first
   `engine.mainMixerNode` access and a player is then connected through it.
   Touching `mainMixerNode` first makes the same graph start cleanly. This was
   isolated by starting the engine four ways with no audio played.
2. **The processed input bus is 9 identical channels.** All nine carry the same
   signal (per-channel dBFS identical to 0.1 dB over 2 s of silence and 4 s of
   playback), so the capture takes channel 0 through an explicit
   `AVAudioConverter.channelMap` rather than trusting a default 9→1 downmix.

## Not measured

- **Near-end interrupt recall** and **double-talk near-end recall**
  (`docs/adr/0006-full-duplex-voice-session.md` § D9) need a human speaking over
  playback; no unattended run can produce them.
- **False hard cancel ≤ 0.1/hour over ≥ 10 aggregate hours** — not producible in
  one sitting by construction.
- Only one volume/distance setting was covered (25% output, laptop at desk
  distance). D9 asks for three per profile.

## What a future card still owes

The measurement alone cannot flip any route, and no code in `jarvis/` was
touched to pretend otherwise. Even with an accepted validation record, an
observed SPEAKER route can never reach the `allowed_barge_mode="natural"` return
today: `resolve_device_profile` gates promotion on
`key.route_kind in {RouteKind.UNKNOWN, RouteKind.HEADPHONES}`
(`jarvis/surface/voice_backend.py:203-214`). `DeviceProfileKey.aec_mode` is
likewise a default-only field fixed to `"none"` with no writer anywhere in
`jarvis/` (`jarvis/surface/voice_backend.py:138`). Both are the resolver work a
`VoiceProcessingIOBackend` card would have to do, on top of the backend itself.

## Recommendation

**Inconclusive, needs the near-end trial.** macOS `VoiceProcessingIO` is
usable on this machine — the engine starts, the capture stays 16 kHz mono int16,
and the canceller cuts absolute far-end echo by 7.5 dB and `record`-profile
false candidates by two thirds. That is not enough to unblock D9: the
false-candidate rate is still 15× over target on the `record` profile, and the
two remaining D9 gates (near-end interrupt recall, double-talk recall) need a
human talking over playback and were not attempted. Nothing here argues for
buying hardware, and nothing here argues for abandoning the software path; the
next thing worth spending is Allen's voice, not a purchase.
