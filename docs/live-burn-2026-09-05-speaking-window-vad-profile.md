# Live burn 2026-09-05 — speaking-window VAD profile

Two arms of one scripted turn against a lane-B daemon (runtime root
`~/.jarvis-lane-b`, port 8016, its own overlay), `realtime.enabled: true`,
`single_audio_ingress.enabled: true`, `streaming_output.enabled: true`,
`JARVIS_REALTIME_TRACE_JSONL` per arm. Arm A ran
`output_active_vad_mode: "record"` (control), arm B `"tts"`.

Audio route: the DEFAULT route throughout — output `MacBook Pro Speakers`,
input `MacBook Pro Microphone`, read before and after each arm. No device was
switched, no `SwitchAudioSource` set, no loopback, so no restore trap. The
system output volume was muted at 0; it was raised to 50 for the arms and
restored to `output volume:0, output muted:true` afterwards. The port-8006
daemon was never signalled.

Stimulus: the wake word, the question and the follow-up were MiniMax-TTS clips
played through the default speakers, so the microphone heard them over the real
acoustic path — the same path that carries playback bleed. Identical clips in
both arms; nobody spoke into the mic.

## Result

| | arm A (`record`) | arm B (`tts`) |
|---|---|---|
| `vad_speech_started` rows while output was active | **0** | **0** |
| rows carrying `vad_mode: "tts"` | 0 | **0** |
| spoken-answer duration | 15.378 s (`playback_started` 23:26:44.332 → `playback_completed` 23:26:59.710) | 45.002 s (`playback_started` 23:27:38.161 → `playback_failed` 23:28:23.163, `partial_tts_provider_failure`) |
| utterances captured | 1 | 2 |

Every classified frame in both arms reported `vad_mode: "record"`. Arm B, with
the strict profile configured, never selected it.

Arm A, `vad_speech_started` + `audio_input_capture_started` for its utterance:

    {"attributes": {"required_hits": 3, "vad_mode": "record"}, "monotonic_ns": 2012938471242375, "name": "vad_speech_started", ...}
    {"attributes": {"input_sample_cursor": 206848, "measurement_boundary": "software_vad_speech_onset", "session_id": "S4ad8b53f86d07415", "stream_epoch": 1}, "monotonic_ns": 2012938471272333, "name": "audio_input_capture_started", ...}

Arm B, the end-of-arm utterance spoken as the answer ended:

    {"attributes": {"required_hits": 3, "vad_mode": "record"}, "monotonic_ns": 2013044629236041, "name": "vad_speech_started", ...}
    {"attributes": {"input_sample_cursor": 1064448, "measurement_boundary": "software_vad_speech_onset", "session_id": "S8b71f5f6c6bec524", "stream_epoch": 1}, "monotonic_ns": 2013044629273000, "name": "audio_input_capture_started", ...}
    {"attributes": {"consecutive_silence_audio_ms": 768.0, "consecutive_silence_frames": 24, "vad_mode": "record"}, "monotonic_ns": 2013046231366791, "name": "vad_endpoint_candidate", ...}

`utterance.received` transcripts: arm A `请直接用语音详细回答。` (id 152); arm B
`请直接用语音详细回答。` (id 178) and, for the end-of-arm utterance,
`现在几点了？` (id 230).

## Why arm B never selected the strict profile

The shipped Wave-3 path classifies no frame while output is active:

- `UtteranceAssembler.feed` returns after `observe_idle` while the assembler is
  IDLE, so the VAD is fed only between wake-arm and endpoint commit.
- A wake hit while output is active is suppressed (ADR-0006 D8,
  `barge_in.enabled: false`), so the assembler cannot arm during output.
- The assembler goes IDLE at commit, before the answer's first sample plays.

The only window in which `output_active()` can be true for a classified frame
is the sub-second overlap where the next utterance's capture opens while the
player is still draining. Across four live turns that window was never hit: the
follow-up wake fired 0.4–1.0 s after the output terminal event, by which time
`output_active()` was already false.

Because the mode moves with the thresholds, `vad_mode` is a direct read-out of
`output_active()` at classification time. All-`record` in arm B is therefore not
a null measurement of the thresholds but a positive measurement of the gate:
zero frames were classified while output was active.

## Verdict — default is `"record"` because the A/B was null

The default ships as `"record"` **because this burn could not measure `"tts"` at
all**, not because `"tts"` was rejected on evidence. Those are different claims
and only the first is true: the strict profile made nothing better and nothing
worse because it never engaged. The stop condition did not fire — arm B missed
no onset arm A caught, truncated no transcript, and lost no utterance; it
captured one more utterance than arm A. No threshold, counter, or answer script
was adjusted.

`"record"` keeps today's classification behaviour exactly. Had the default
shipped as `"tts"`, the −22 dB gate would have started applying silently the
first time natural barge-in could arm capture during output — an unmeasured
behaviour change that would have surfaced inside some future barge-in card and
been attributed to it rather than to this one. The mechanism and the knob stay
in place so that adoption is an explicit, measured decision by whoever lands
that card: re-run this burn then, because the measurement is only meaningful
once a frame can be classified while output is active.

## Owner follow-ups (not blockers)

- No human voice was ever in the loop. The stimulus was TTS clips over the real
  acoustic path because the running session cannot speak. A run with a person
  talking over the answer's tail is still worth having.
- The exception path — a raising `output_active` keeps the current profile
  rather than failing strict — has no test. The card capped this file at
  exactly two tests and forbade per-state tests, so the gap is the card's, not
  the implementation's; a later card can close it deliberately.
