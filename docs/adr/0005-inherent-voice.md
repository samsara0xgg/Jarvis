# ADR-0005 — Inherent Voice Surface (wake · PTT · TTS)

**Status:** Accepted
**Date:** 2026-05-26
**Supersedes:** the `POST /inherent/asr-submit` 501 stub in `jarvis/surface/inherent_server.py` (placeholder reserved by ADR-0003 Step 1).
**Depends on:** ADR-0003 (Inherent text surface — FastAPI app, broadcaster, watchers, process lock).
**Defers to future ADRs:** ADR-0004 (image input), ADR-0006 (barge-in / interrupt during TTS), ADR-0007 (`surface.failed` durable emit per spec §3.6.11 — promoted from ADR-0003 F5 carry-over).

---

## 1. Context

ADR-0003 shipped the new-Jarvis Inherent surface as a FastAPI daemon on `127.0.0.1:8006`, with `POST /inherent/submit` wired to `emit_surface_user_intent`, three-envelope `open/append/done` WebSocket broadcast for the three `surface.response_*` event types, and a single-cursor watcher pattern that eliminates ordering races. The two endpoints `/inherent/image-submit` and `/inherent/asr-submit` were left as deliberate 501 stubs naming ADR-0004 and ADR-0005 respectively.

The legacy Jarvis (`/Users/alllllenshi/Projects/jarvis-legacy/`) implements a fully working Inherent voice chain across `core/wake_word.py`, `core/inherent_wake_listener.py`, `core/vad_silero.py`, `core/audio_recorder.py`, `core/speech_recognizer.py`, `core/asr_normalizer.py`, `core/tts.py`, `core/tts_minimax_ws.py`, `core/audio_stream_player.py`, `core/media_ducking.py`, plus Swift companions `desktop/inherent-swift/InherentCard/NativeVoiceRecorder.swift`, `OutputSpeechPlayer.swift`, and `SystemAudioDucker.swift`. Behavior is proven; what is missing is conformance to the 6-layer architecture, the canonical event taxonomy, and three specific spec deviations identified during legacy inspection.

This ADR specifies the migration of that voice chain into `jarvis/surface/` under strict spec compliance (`docs/spec.html` §3.4.13, §3.6.1–§3.6.6, §3.6.10–§3.6.11, §5.4) while porting the proven implementation modules aggressively.

## 2. Scope

In scope (single ADR, ship-as-one):

1. **"Hey Jarvis" wake-word listener** — always-on background thread; on detect, opens a voice turn (record → ASR → normalize → emit `utterance.received`).
2. **Long-press Return push-to-talk** — Swift-owned NSEvent monitor in the Inherent app posts captured WAV to `POST /inherent/asr-submit`; the endpoint executes ASR → normalize → emit `utterance.received`.
3. **TTS voice output** — assistant-response streaming TTS triggered by `surface.response_chunk` / `surface.response_emitted` events, playback respects `ResponsePlan.required_gate_mode` per spec §3.6.6.

Out of scope:

- **Barge-in / interrupt-during-TTS** — deferred to ADR-0006. Legacy `interrupt_monitor.py` is not ported. While TTS is playing, the wake listener pauses (does not open its mic stream).
- **Image input** — ADR-0004.
- **Streaming ASR / interim transcripts** — neither legacy implementation has it; out of scope here too.
- **Cloud calculator (non-Inherent) voice surfaces** — Mac-only voice session per spec §3.7.2.
- **`surface.failed` durable event emit** — ADR-0007 (carries over from ADR-0003 F5).

## 3. Spec Compliance Map

| Requirement | Spec section | Where this ADR satisfies it |
|---|---|---|
| Input adapter `raw → buffer → canonical event` | §3.6.1 | `voice_audio.py` (PortAudio raw → VAD buffer) → `voice_asr.py` (recognize) → `voice_pipeline.py` (normalize) → `emit_event("utterance.received", ...)`. |
| `audio frames → VAD/ASR local buffer → utterance.received(transcript, confidence)` | §3.6.1 (row 1) | `utterance.received` registry extended with `confidence`, `audio_artifact_ref`, `language_detected`, `emotion` in `optional_payload`. **Confidence semantics:** provider-specific — SenseVoice returns a binary 0.1/0.9 heuristic (legacy `speech_recognizer.py:170-175`); Whisper returns a log-prob mean. Downstream consumers must read confidence as provider-tagged, not normalized. This ADR records the value the provider returns; normalization is out of scope (a future ADR can promote a projection-level threshold). |
| Adapter-internal canonicalization (corrections inside adapter, pre-emit) | §3.6.2 | `voice_asr.py.normalize(text)` runs BEFORE `emit_event(...)`. Three-layer cascade (manual context-guarded fixes, structured aliases, opt-in fuzzy) ported verbatim from `core/asr_normalizer.py`. **Fixes legacy deviation:** legacy normalized in `JarvisApp._process_turn` AFTER the surface emitted raw text. |
| Entity aliases NOT in voice layer | §3.6.2 | The legacy `core/asr_normalizer.py` already keeps entity aliases out — only phonetic / homophone corrections live here. Ported as-is; no entity alias logic added. |
| Raw ASR output optionally as `artifact_ref` | §3.6.2 | New `voice_artifact_store.py` (writes the raw WAV under `data/voice_artifacts/{turn_id}.wav` when `JARVIS_VOICE_RETAIN_RAW=1`); `audio_artifact_ref` field on `utterance.received` carries the path. Default disabled. |
| Voice = input adapter AND output surface, neither side oversteps | §3.6.5 | `voice_pipeline.py` (input) emits `utterance.received`; intent decision happens in L3. `voice_tts.py` (output) only plays text already vetted by `surface.response_*` events (i.e. text that passed Pre-emit Gate per ADR-0003 Step 2). Voice surface never decides "should I interrupt Allen" (deferred to ADR-0006 + L3 Attention Policy). |
| TTS streaming respects `ResponsePlan.required_gate_mode` | §3.4.13, §3.6.6 | `voice_tts.py` consumes events: `sentence` mode → speak each `surface.response_chunk` as it arrives; `full_text` mode → wait for `surface.response_emitted` and speak the full text; `structured` mode → not produced by Day-1 L3 yet, treated as `full_text`. Voice surface does NOT inspect the LLM stream — only the L3-vetted event payloads. |
| TTS provider failure → fallback chain | §3.6.5 (fallback), §3.6.11 | `voice_tts.py` declares `SurfaceFallbackChain = [minimax_ws → macos_say → log_warn]`. MiniMax connect/stream failure → `subprocess.run(["say", "-v", "Tingting", ...])`. macOS `say` failure → warn log only (no infinite loop). ADR-0007 will turn the warn into `surface.failed`. |
| Presentation action vs world action | §3.6.10 | TTS playback is a presentation action. Rate-limiting policy: at most one TTS playback in flight at a time (refuse overlapping `voice_tts.play(text)` calls — second caller waits). No new world action created. |
| Channel → physical surface mapping | §3.6.4 | Day-1 scope only handles the chat-turn streaming path (L5 `cli_render` emits `surface.response_*`, the new `_tts_watcher` consumes them, `voice_tts.py` plays). L3 channel routing (`voice_notify` for an attention-policy-driven spoken alert) is NOT wired by this ADR — a future ADR will introduce the L3→L5 contract for non-chat voice-channel events and reuse the same `voice_tts.TTSPipeline` instance. |
| Backpressure / coalescing | §3.6.12 | Voice phase WS envelopes (listening / transcribing / accepted / empty / error) are L5-only presentation signals; if a wake turn produces empties in quick succession, the broadcaster sends each without coalescing — Day-1 simplification. Inherent panel UI handles render-side smoothing. |
| Event Log append-only, registered types only | §5.4 | All `utterance.received` payload extensions land in `state/event_log.py` registry. No new event types are introduced by this ADR. |
| Mac domain owns "Mac voice session" | §3.7.2 | Voice subsystem runs entirely on Mac inside the existing Inherent daemon process. No cross-domain transport. |

## 4. Architecture

### 4.1 Process topology (unchanged from ADR-0003)

Two processes, one IPC contract on `127.0.0.1:8006`:

- **Python daemon** (`runtime/inherent_loop.serve_inherent`) — adds three subsystems below the existing FastAPI + watcher pair:
  - Wake listener background thread
  - TTS playback subsystem
  - Mic input lock (mutex shared by wake and PTT paths)
- **Swift Inherent app** — keeps long-press Return capture and AppleScript ducking on the PTT path. No new client-side capabilities; `OutputSpeechPlayer.swift` is intentionally left unused (Python plays TTS directly to OS audio, same as legacy default).

### 4.2 L5 module shape

Seven new flat files at `jarvis/surface/` (consistent with the layer's flat-file convention — `cli.py`, `cli_render.py`, `inherent_server.py`, `inherent_output.py`, `notify.py`, `sentence_splitter.py`). Five "meat" files port the legacy implementation; two thin helpers compose and persist:

| File | Responsibility | Legacy meat ported from |
|---|---|---|
| `voice_audio.py` | Silero VAD (ONNX, 32 ms / 512-sample chunks, two modes `record` / `tts`) + PortAudio mic capture with VAD-gated end-of-speech detection. Exposes `capture_utterance(*, max_duration_s, min_voiced_s) -> AudioBytes`. | `core/vad_silero.py` (verbatim ONNX runner + state machine) + `core/audio_recorder.py` (`PortAudio InputStream` + VAD gating). |
| `voice_asr.py` | ASR provider interface + `recognize(audio_bytes) -> TranscriptionResult` + `normalize(text) -> str` (three-layer cascade) + `is_empty_or_too_short(text, audio_bytes) -> bool` (unified filter). Provider: SenseVoice via sherpa-onnx (`data/sensevoice-small-int8/`). MLX-Whisper fallback unchanged from legacy. | `core/speech_recognizer.py` + `core/asr_normalizer.py`. |
| `voice_wake.py` | `WakeListener` daemon thread: openwakeword `hey_jarvis_v0.1` model, 16 kHz / 1280-sample frames, threshold 0.5; on detect, acquires `VOICE_INPUT_LOCK` and runs a full turn (record → recognize → normalize → emit). Pauses while `voice_tts.is_speaking()` is true (NO barge-in per scope). | `core/wake_word.py` + `core/inherent_wake_listener.py`. |
| `voice_tts.py` | `TTSPipeline`: MiniMax `speech-2.8-turbo` WebSocket (`api-uw.minimax.io` primary, `api.minimax.chat` fallback) + `AudioStreamPlayer` (PortAudio output, 48 kHz, mono float32 ring buffer + sample-accurate gain ramps). Sentence-chunked playback driven by `surface.response_*` events with gate-mode-aware policy (§3.6.6). | `core/tts.py` + `core/tts_minimax_ws.py` + `core/audio_stream_player.py`. |
| `voice_ducking.py` | macOS AppleScript output mute / restore with refcounted depth. Context-manager API: `with voice_ducking.duck(): ...`. | `core/media_ducking.py` verbatim. |
| `voice_pipeline.py` | L5 composition site used by both wake and PTT paths. Takes raw audio bytes, runs `recognize → normalize → is_empty_or_too_short → optional artifact write → emit_event`, returns the `Event` row. Owns `VOICE_INPUT_LOCK` (`threading.Lock`) so wake + PTT cannot double-capture. | NEW — no legacy equivalent (legacy ran composition inline in `JarvisApp._process_turn`, which is exactly the spec deviation §8.1 fixes). |
| `voice_artifact_store.py` | Opt-in raw-WAV retention for debug; controlled by `JARVIS_VOICE_RETAIN_RAW` env var. Writes under `data/voice_artifacts/{turn_id}.wav`. | NEW — legacy did not retain raw audio. |

**Layer-rule compliance (`.importlinter` `surface` row):** all seven modules above import only `stdlib`, `jarvis.shared`, `jarvis.state.event_log`, `jarvis.constitution`. They do NOT name `jarvis.decision`, `jarvis.execution`, `jarvis.deployment`, `jarvis.runtime`, or `jarvis.cli`. The wake listener and TTS pipeline are CONSTRUCTED by `runtime/inherent_loop.py` (the only cross-layer wiring site), which passes them their dependencies (the broadcaster, a fresh `sqlite3.Connection` factory, and a `ResponsePlanProvider` Protocol satisfied by the L3 `ResponsePlan` type via duck-typing à la `cli.py:ResponsePlanLike`).

### 4.3 Threading & concurrency

| Subsystem | Thread | Notes |
|---|---|---|
| FastAPI / WS endpoints | event loop | unchanged (ADR-0003) |
| `_user_intent_watcher` (extended) / `_response_watcher` | event-loop async tasks | `_user_intent_watcher` polling extended to `WHERE type IN ('surface.user_intent', 'utterance.received')`; `_response_watcher` unchanged (ADR-0003) |
| `_tts_watcher` (new) | event-loop async task | independent cursor over `surface.response_open / _chunk / _emitted`; calls `voice_tts.TTSPipeline` methods which dispatch synthesis to its own daemon thread. Runs in parallel with `_response_watcher` — both are read-only polls, no contention. |
| Wake listener (`voice_wake.WakeListener`) | daemon thread `jarvis-wake` | listens on a `sd.InputStream`; on detection, acquires `VOICE_INPUT_LOCK`, opens a separate capture stream, runs ASR on a single-worker `ThreadPoolExecutor` `jarvis-voice-asr`, emits the event on a fresh `sqlite3.Connection` (per the ADR-0003 §SQLite-thread-safety pattern) |
| PTT inbound (`/inherent/asr-submit`) | event loop → `asyncio.to_thread` | runs ASR via the same single-worker `ThreadPoolExecutor` (queues if wake is already busy) |
| TTS synthesis | daemon thread `jarvis-tts-synth` | one MiniMax WS connection per turn (`prewarm` on first `response_chunk`) |
| TTS playback | daemon thread `jarvis-tts-play` | feeds PortAudio ring buffer |
| Wake/PTT mutual exclusion | `threading.Lock VOICE_INPUT_LOCK` | held for the full duration of `voice_pipeline.run_turn(...)` so the other path cannot also capture |
| Worker thread → broadcaster | `asyncio.run_coroutine_threadsafe(broadcaster.broadcast_voice(...), loop)` | `voice_pipeline.run_turn` runs in a worker thread; broadcaster's `asyncio.Lock` is event-loop-scoped, so phase envelopes are scheduled onto the daemon event loop via the daemon's stored loop reference. |

`VOICE_INPUT_LOCK` is a **non-blocking acquire** for the wake listener: if PTT is already running, the wake detection is dropped (logged at INFO). For PTT, the acquire is blocking with a 2-second timeout — if the wake listener is mid-turn the user retries, but typical wake turns finish in <8 s so 2 s timeout leaves slack.

## 5. Lifecycles

### 5.1 Wake-word turn

```
idle
  ↓ (sd.InputStream 16 kHz / 1280-sample frames in jarvis-wake)
  ↓ openwakeword.predict(frame) > 0.5
detected
  ↓ try_acquire(VOICE_INPUT_LOCK)
  ↓   if fail (PTT busy): drop, return to idle
  ↓ close wake stream
  ↓ broadcaster.broadcast_voice("listening", turn_id=<mint>)
  ↓ voice_ducking.duck()      (refcounted; OS output muted)
listening
  ↓ voice_audio.capture_utterance(max_duration_s=5, min_voiced_s=1)
  ↓   uses Silero VAD `record` mode (prob 0.4, dB −45)
recording  (≤5 s hard cap, early-cut on VAD end-of-speech)
  ↓ voice_ducking.restore()
  ↓ broadcaster.broadcast_voice("transcribing", turn_id)
transcribing
  ↓ voice_pipeline.run_turn(audio_bytes, turn_id, channel="inherent_wake", language="zh-CN")
  ↓   voice_asr.recognize(audio)  →  TranscriptionResult(text, confidence, language_detected?, emotion?)
  ↓   voice_asr.is_empty_or_too_short(text, audio):
  ↓     true  → broadcaster.broadcast_voice("empty", turn_id); release lock; return to idle
  ↓     false → voice_asr.normalize(text) → normalized
  ↓   optional voice_artifact_store.persist(audio, turn_id)
  ↓   emit_event("utterance.received", payload={transcript: normalized, turn_id, channel: "inherent_wake",
  ↓                                              language: "zh-CN", confidence, language_detected?,
  ↓                                              emotion?, audio_artifact_ref?})
  ↓ broadcaster.broadcast_voice("accepted", turn_id, transcript=normalized, emotion?)
  ↓ release VOICE_INPUT_LOCK
emitted
  ↓ (downstream — owned by existing ADR-0003 _user_intent_watcher path … BUT see note below)
  ↓ runtime spawns drive_turn(... trigger=utterance.received ...)
  ↓ … decision/execution … → surface.response_open → ... _chunk × N → ... _emitted
  ↓ in parallel: voice_tts.TTSWatcher consumes the same events from the L2 log (see §5.3)
spoken
  ↓ AudioStreamPlayer drains; voice_tts.is_speaking() → False
idle
  ↓ wake listener reopens sd.InputStream
```

**Important wiring detail:** the existing `_user_intent_watcher` polls for `surface.user_intent` events only (set in `runtime/inherent_loop.py` per ADR-0003). For voice turns we need it to also consume `utterance.received`. This ADR extends `_user_intent_watcher` to poll `WHERE type IN ('surface.user_intent', 'utterance.received') ORDER BY id`, dispatching both to `drive_turn` with the trigger event passed through. `drive_turn`'s existing signature already takes an `Event`; no L3 change needed.

### 5.2 Long-press Return PTT turn

```
idle (Swift Inherent app foreground)
  ↓ NSEvent local keyDown monitor: keyCode 36 or 76, modifiers ≤ shift
  ↓ schedule DispatchWorkItem with voiceHoldMs = 220 ms  (Swift legacy unchanged)
  ↓ keyDown duration ≥ 220 ms → beginEnterVoiceCapture
listening (Swift-driven, panel UI shows listening)
  ↓ Swift duckSystemAudioForVoice() — AppleScript via SystemAudioDucker
  ↓ NativeVoiceRecorder starts AVAudioEngine inputNode tap, mono PCM16
recording (user holds, no VAD on Swift side)
  ↓ NSEvent keyUp → finishEnterVoiceCapture
  ↓ wavData = await NativeVoiceRecorder.stop()
  ↓ Swift restoreSystemAudioForVoice()
transcribing (Swift sets card phase)
  ↓ POST /inherent/asr-submit  (multipart: wav file + turn_id?)
  ↓ ─── server side ──────────────────────────────────────────
  ↓ inherent_server.asr_submit handler:
  ↓   read multipart WAV
  ↓   turn_id = mint_turn_id()  (server-mint; client-supplied turn_id ignored Day-1 to avoid trust)
  ↓   await asyncio.to_thread(voice_pipeline.run_turn,
  ↓                            audio_bytes, turn_id,
  ↓                            channel="inherent_ptt", language="zh-CN")
  ↓   inside run_turn: blocking acquire on VOICE_INPUT_LOCK (timeout 2 s)
  ↓     if timeout: HTTP 503 "busy", Swift card returns to retry state
  ↓   recognize → normalize → empty-check → optional artifact → emit utterance.received
  ↓   release lock
  ↓ HTTP 200 {"status": "accepted", "transcript": normalized, "turn_id": turn_id}
  ↓ ─── back to Swift ─────────────────────────────────────────
submitting (Swift sets phase, panel shows accepted text)
  ↓ (no separate broadcaster.broadcast_voice("transcribing"/"accepted") on PTT path:
  ↓  Swift drives panel state directly from HTTP response — legacy parity)
emitted
  ↓ (same as wake from this point on)
```

`POST /inherent/asr-submit` request shape (multipart/form-data):

| Field | Type | Required | Notes |
|---|---|---|---|
| `audio` | file (`audio/wav`, PCM16, ≤ 30 s, ≤ 5 MB) | yes | reject larger with 413 |
| `language` | string (BCP-47) | no | default `zh-CN` |

Response (200):
```json
{"status": "accepted", "transcript": "正常化后的文本", "turn_id": "T1a2b3c4d"}
```
Error responses:
- 400 — empty body / missing field / unsupported codec
- 413 — audio too large
- 415 — unsupported content type
- 422 — ASR returned empty (filter tripped)
- 503 — `VOICE_INPUT_LOCK` busy (wake listener mid-turn), client retries
- 500 — internal (logged with `turn_id`)

### 5.3 TTS output

TTS is event-driven, not function-driven. `runtime/inherent_loop.py` spawns a fourth background task `_tts_watcher` that polls the L2 log for `surface.response_*` events in monotonic-id order and feeds them to `voice_tts.TTSPipeline`.

```
new surface.response_open(turn_id, query, required_gate_mode?)
  ↓ gate_mode = payload.get("required_gate_mode", "sentence")  # see "Plan vs event" below
  ↓ pipeline.begin_turn(turn_id, gate_mode)
  ↓ pipeline opens MiniMax WS in background; ring buffer cleared
  ↓ voice_ducking.duck()   (TTS playback ducks other apps)

new surface.response_chunk(turn_id, text)
  ↓ if gate_mode == "sentence":
  ↓   pipeline.speak_sentence(text)  — sentence is already pre-chunked by L3 cli_render
  ↓ elif gate_mode == "full_text" or "structured":
  ↓   pipeline.buffer_chunk(text)    — accumulate, do NOT play yet

new surface.response_emitted(turn_id, full_text?)
  ↓ if gate_mode in ("full_text", "structured"):
  ↓   pipeline.speak_buffered()      — synthesize and play accumulated text as one shot
  ↓ pipeline.end_turn(turn_id)
  ↓ voice_ducking.restore()
  ↓ broadcaster.broadcast_voice("spoken", turn_id)   (optional UI feedback)
```

**Plan vs event:** `ResponsePlan.required_gate_mode` is an L3→L5 contract object (per spec §3.6.3, NOT an event field). The current `surface.response_open` event payload (`jarvis/surface/cli_render.py:140-156`) does NOT carry `required_gate_mode` — `cli_render._emit_response_open` only puts `query` in the payload, then the chunking branch at `cli_render.py:175 (if response_plan.required_gate_mode == "sentence")` decides whether to emit `surface.response_chunk` rows. This ADR adds `required_gate_mode` to `surface.response_open`'s `optional_payload` and updates `_emit_response_open` to set it from the plan, so the L5 voice consumer can route without importing L3. Default for missing field (legacy events) is `"sentence"` — this matches the behavior when L3 lets chunks through.

**Voice fallback chain (§3.6.11):**

```
SurfaceFallbackChain (TTS):
  primary    = minimax_ws         (MiniMax speech-2.8-turbo)
  fallback_1 = macos_say          (subprocess["say", "-v", "Tingting"]; per-sentence file fallback)
  fallback_n = log_warn_only      (terminal — ADR-0007 will turn into surface.failed)
  queue_policy = at_most_one_in_flight
  cooldown    = 30 s on provider failure
```

If MiniMax WS open fails (DNS / 5xx / timeout > 3 s), the pipeline switches to `macos_say` for the rest of the turn AND records `tts_provider="say"` in a per-turn internal counter. After 30 s cooldown it retries MiniMax on the next turn. ADR-0007 will replace the "warn only" leaf with `surface.failed(surface_id="voice", reason, fallback_used)`.

## 6. Wire Contract Changes (port 8006)

Inbound — one endpoint promoted from 501:

```
POST /inherent/asr-submit   →   200 {"status","transcript","turn_id"}  | 400 | 413 | 415 | 422 | 503 | 500
```

Outbound (`/inherent/ws`) — one new envelope `op`:

```jsonc
// added by this ADR (legacy parity: ui/web/server.py:411-414 _emit_inherent_voice)
{"op": "voice", "payload": {
  "phase":      "listening" | "transcribing" | "accepted" | "empty" | "error" | "spoken",
  "turn_id":   "T1a2b3c4d",
  // optional, phase-dependent:
  "transcript": "...",         // present on "accepted"
  "emotion":    "HAPPY" | ...,  // SenseVoice-only, optional, on "accepted"
  "reason":     "no_speech" | "asr_error" | ... // on "error"
}}
```

The `spoken` phase is broadcast by `voice_tts.TTSPipeline.end_turn(turn_id)` when playback drains, so the Inherent panel can collapse / fade after TTS finishes. Producers split: phases `listening / transcribing / accepted / empty / error` originate from the wake path (§5.1); phase `spoken` originates from the TTS pipeline regardless of which input path produced the turn.

**Producers of `op:"voice"` envelopes:** the wake path (listening / transcribing / accepted / empty / error) AND the TTS pipeline (spoken). PTT (`POST /inherent/asr-submit`) does NOT broadcast the input phases — it drives the Swift card phases directly from its HTTP response (`{"status","transcript","turn_id"}`) per legacy parity. PTT-triggered turns DO receive the `spoken` envelope when TTS finishes (since TTS doesn't know whether the trigger was wake or PTT — it just sees `surface.response_*` events).

Why a broadcaster method, not an event: per spec §3.6.3, voice phase signals are L5-internal presentation contracts ("AttentionRouting and PresentationIntent are L3→L5 message contracts, NOT written to Event Log"). The voice phase envelope is the same kind of signal in the opposite direction (L5→client UI) — it does not carry truth, only UI feedback. Routing it through the Event Log would (a) pollute the log with non-durable signals, (b) require a new registered event type with no projection consumer, (c) couple wake-path latency to SQLite write throughput.

`InherentBroadcaster.broadcast_voice(phase, turn_id, **payload)` is therefore a direct method called synchronously from `voice_pipeline.run_turn` (running in a worker thread). The broadcaster's `asyncio.Lock` is acquired via `asyncio.run_coroutine_threadsafe` from the worker thread targeting the daemon's event loop (the same loop that owns the WS connections).

## 7. Event Type Registry Changes

Single change to `jarvis/state/event_log.py`. The `utterance.received` entry gains optional fields:

```python
EventTypeSchema(
    event_type="utterance.received",
    owner_layer="L5",
    required_payload=("transcript", "turn_id"),
    optional_payload=(
        "channel",            # existing
        "language",           # existing
        "confidence",         # NEW — float 0..1, provider-specific semantics
        "language_detected",  # NEW — BCP-47 from ASR (e.g. SenseVoice <|zh|> tag)
        "emotion",            # NEW — provider-specific tag (SenseVoice only)
        "audio_artifact_ref", # NEW — relative path under data/voice_artifacts/, set only when JARVIS_VOICE_RETAIN_RAW=1
    ),
    schema_version=1,
)
```

`schema_version` stays at 1: adding optional fields is a backwards-compatible change per the registry's append-only optional contract (no existing consumer reads these fields; missing keys remain valid). If any future consumer needs to assume these fields exist, bump to 2.

Required addition to `surface.response_open` registry entry: add `required_gate_mode` to `optional_payload` (verified absent today — see §5.3 "Plan vs event"). `cli_render._emit_response_open` will pass `response_plan.required_gate_mode` into the payload at emit time. No other event types added.

## 8. Three Legacy Spec Deviations Fixed

| # | Legacy behavior | Spec violation | Fix in this ADR |
|---|---|---|---|
| 1 | ASR normalization runs in `JarvisApp._process_turn` (`jarvis-legacy/jarvis.py:925`), AFTER the wake listener already emitted the raw transcript to the UI's "accepted" state. The LLM saw normalized text; the UI showed raw text. | §3.6.2: "ASR corrections / phonetic aliases 属于 Voice adapter 内部，在生成 `utterance.received` 前应用" | `voice_pipeline.run_turn` calls `voice_asr.normalize(text)` BEFORE `emit_event("utterance.received", ...)` and before `broadcaster.broadcast_voice("accepted", ...)`. Both UI and L3 see the same normalized text. |
| 2 | Wake listener (Python `sd.InputStream`) and PTT recorder (Swift `AVAudioEngine`) both consume the default mic independently. Legacy ducks OS audio but does not coordinate mic streams — a long-press during an active wake turn double-captures. | §3.6.5 fallback / coherence requirement; the spec does not name this case explicitly but the voice-as-single-surface model implies one input stream at a time. | `voice_pipeline.VOICE_INPUT_LOCK` (`threading.Lock`). Wake listener does a non-blocking acquire; PTT does a blocking 2 s acquire. Whichever path runs first blocks the other for the turn's duration. |
| 3 | Two different empty-utterance filters: wake listener drops non-zh short fragments (`inherent_wake_listener.py:179-185`); `/inherent/asr-submit` server returns `status:"empty"` via a different code path. Threshold differences caused inconsistent UX. | §3.6.2 canonicalization is per-adapter, but two filters violate the "one canonical event-emit path" invariant. | One `voice_asr.is_empty_or_too_short(text, audio)` helper used by both paths. Returns `True` if `len(text.strip()) < 2`, OR audio RMS < threshold, OR text contains only non-zh CJK punctuation. |

These three are pure ports with reshaping; no behavior is added beyond what the spec mandates.

## 9. Configuration

New keys in `config/...` (matching the existing legacy config shape so secrets / endpoints are not lost in the migration):

| Key | Default | Notes |
|---|---|---|
| `voice.asr.provider` | `sensevoice` | also `mlx_whisper` (Apple Silicon native) |
| `voice.asr.sensevoice_model_dir` | `data/sensevoice-small-int8/` | sherpa-onnx model artifacts |
| `voice.asr.mlx_whisper_model` | `mlx-community/whisper-large-v3-turbo` | fallback path |
| `voice.vad.model_path` | `data/silero_vad.onnx` | Silero ONNX |
| `voice.wake.model_name` | `hey_jarvis_v0.1` | openwakeword |
| `voice.wake.threshold` | `0.5` | openwakeword detection |
| `voice.wake.frame_size` | `1280` | 80 ms at 16 kHz |
| `voice.tts.provider` | `minimax_ws` | also `macos_say` (fallback only) |
| `voice.tts.minimax_voice` | `Chinese (Mandarin)_ExplorativeGirl` | preserved from legacy |
| `voice.tts.minimax_endpoint` | `wss://api-uw.minimax.io` | primary |
| `voice.tts.minimax_fallback_endpoint` | `wss://api.minimax.chat` | secondary |
| `voice.recorder.max_utterance_s` | `5.0` | hard cap on wake-path recording |
| `voice.recorder.min_voiced_s` | `1.0` | minimum voiced duration before VAD end-of-speech can trigger |
| `voice.normalizer.corrections` | `[]` | manual context-guarded fixes (see §asr_normalizer.py:93-108 legacy) |
| `voice.normalizer.aliases` | `{}` | structured `{canonical: [alias, ...]}` |
| `voice.normalizer.fuzzy_enabled` | `false` | Levenshtein fallback |

Environment variables:

| Var | Default | Notes |
|---|---|---|
| `MINIMAX_API_KEY` | (none) | required for TTS primary path; if unset, MiniMax fails fast and falls to `macos_say` |
| `JARVIS_VOICE_RETAIN_RAW` | `0` | `1` enables raw-WAV retention under `data/voice_artifacts/` |
| `JARVIS_VOICE_DISABLE_WAKE` | `0` | `1` disables the wake listener thread (PTT-only mode) |

## 10. Failure Modes & Fallback

| F# | Failure | Behavior |
|---|---|---|
| F1 | Wake-word model file missing on disk | `WakeListener` logs ERROR at start and exits its thread; daemon stays up (PTT still works); does NOT crash the daemon. |
| F2 | Silero VAD model file missing | Wake listener and `/inherent/asr-submit` both fail-fast with HTTP 500 / log ERROR. Daemon stays up. |
| F3 | ASR provider crash mid-turn | `voice_pipeline.run_turn` catches the exception, releases `VOICE_INPUT_LOCK`, and signals the caller. **Wake path:** broadcasts `voice("error", reason="asr_error")` and returns to idle. **PTT path:** raises `VoicePipelineError`; the `/inherent/asr-submit` handler maps it to HTTP 500 with structured body `{"detail": "asr_error", "turn_id": ...}` (Swift card surfaces a retry hint). No `op:"voice"` envelope on PTT. |
| F4 | Empty / too-short utterance | `voice_pipeline.run_turn` skips `emit_event`, releases the lock, and signals the caller. **Wake path:** broadcasts `voice("empty")` and returns to idle. **PTT path:** returns HTTP 422 with body `{"detail": "empty", "turn_id": ...}`; Swift card returns to retry state. No `op:"voice"` envelope on PTT. |
| F5 | `VOICE_INPUT_LOCK` contention | PTT: HTTP 503, Swift card retries. Wake: drop detection, log INFO. |
| F6 | MiniMax WS open fails (timeout 3 s / 5xx / DNS) | TTS pipeline switches to `macos_say` for the current turn; 30 s cooldown before next MiniMax retry. |
| F7 | `macos_say` subprocess fails | Log WARN with turn_id and full text; assistant response is silent (text-only via panel still works). ADR-0007 will emit `surface.failed`. |
| F8 | Wake listener catches an unexpected exception | Log ERROR with traceback; sleep 2 s; reopen `sd.InputStream`; resume. (Legacy parity — same self-heal pattern.) |
| F9 | `/inherent/asr-submit` audio > 5 MB or > 30 s | HTTP 413 with explicit message; Swift card surfaces "音频过长". |
| F10 | TTS gate-mode `structured` arrives but no structured-block field is present | Treat as `full_text` (degrade gracefully). Log DEBUG once per turn. |

## 11. Test Strategy

| Tier | Coverage |
|---|---|
| Unit | `voice_asr.normalize` (3-layer cascade — context-guarded, structured alias, fuzzy). `voice_asr.is_empty_or_too_short`. `voice_pipeline.VOICE_INPUT_LOCK` ordering invariant via two-thread harness. `voice_tts` gate-mode routing (`sentence` vs `full_text`). `voice_ducking` refcounted depth (mock `osascript`). |
| Integration (no audio HW) | `POST /inherent/asr-submit` against the FastAPI app with a stub `voice_asr.recognize` that returns a canned `TranscriptionResult`; asserts `utterance.received` row in event log with normalized transcript, lock released, HTTP 200 returned. `_tts_watcher` consumes a sequence of `surface.response_*` rows and calls a fake `TTSPipeline` with the expected sentence/full_text payloads. `_tts_watcher` and `_response_watcher` running in parallel against the same event stream: no missed envelopes, no ordering violations. `_user_intent_watcher` extension: a row of `utterance.received` triggers `drive_turn` the same way as `surface.user_intent`. |
| Canary (`tests/canary/`) | New canary `test_voice_normalize_before_emit` — `emit_event("utterance.received", ...)` callers in `jarvis/surface/` must call `voice_asr.normalize` first; AST scan asserts the call order. New canary `test_voice_lock_held_during_emit` — wake and PTT paths must both acquire `VOICE_INPUT_LOCK` before `emit_event`. New canary `test_layer_5_voice_imports` — none of the 7 new files name `jarvis.decision`, `jarvis.execution`, `jarvis.deployment`, `jarvis.runtime`, `jarvis.cli`. |
| Smoke (Allen manual) | (a) start daemon, long-press Return in Inherent, say "现在几点", confirm `utterance.received` row + panel shows "几点" normalized + TTS speaks the time. (b) say "Hey Jarvis, 现在几点" without touching the keyboard, confirm same result. (c) start a long TTS response, attempt to wake during playback, confirm wake is suppressed until playback ends. |

Test artifacts: legacy `scripts/bench_voice_pipeline.py` is NOT ported — its scope is benchmarking, not correctness. A future ADR can promote it if needed.

## 12. Dependencies

Added to `pyproject.toml` (all already in legacy `requirements.txt`; preserve versions):

- `openwakeword` (ONNX wake detector)
- `sherpa-onnx` (SenseVoice runtime)
- `sounddevice` (PortAudio bindings)
- `numpy` (already a transitive dep)
- `websockets` (MiniMax WS client; if `httpx` ws is preferred, swap — legacy uses `websockets`)
- `onnxruntime` (Silero VAD; pinned to match `sherpa-onnx`)

Optional (Apple Silicon only):
- `mlx-whisper` — only when `voice.asr.provider == "mlx_whisper"`.

Model artifact placement (already organized in legacy `data/`):
- `data/sensevoice-small-int8/{model.int8.onnx, tokens.txt}`
- `data/silero_vad.onnx`
- openwakeword `hey_jarvis_v0.1.onnx` — downloaded at first run by openwakeword's bootstrap; cached in `data/openwakeword/`.

A pre-flight check at daemon startup (`runtime/inherent_loop.serve_inherent`) verifies the three on-disk model files exist before spawning `WakeListener`. If any is missing, log ERROR and skip wake thread; PTT still works (it does not need the wake model, and SenseVoice + Silero are checked at first use).

## 13. Migration Order (high-level, for the writing-plans pass)

The plan that follows this ADR will partition implementation into roughly:

1. Registry change (`utterance.received` optional fields, `surface.response_open.required_gate_mode`) + canaries.
2. `voice_ducking.py` (smallest, no deps beyond stdlib + `osascript`).
3. `voice_audio.py` (VAD + recorder; depends on stdlib + sounddevice + onnxruntime).
4. `voice_asr.py` (recognizer + normalizer; depends on sherpa-onnx, optional mlx-whisper).
5. `voice_artifact_store.py` (tiny; raw-WAV retention helper).
6. `voice_pipeline.py` (composition: ties recognize + normalize + filter + emit + lock).
7. `inherent_server.py` `/inherent/asr-submit` real implementation + `inherent_output.py` `broadcast_voice` method.
8. `voice_wake.py` (daemon thread; depends on `voice_pipeline`).
9. `voice_tts.py` (MiniMax WS + AudioStreamPlayer; depends on stdlib + websockets + sounddevice).
10. `runtime/inherent_loop.py` wiring: spawn `WakeListener` + `_tts_watcher`; extend `_user_intent_watcher` to also consume `utterance.received`.
11. Smoke + canaries pass.

## 14. Reference Sources (verbatim port permissions)

The following legacy files MAY be ported verbatim (with namespace and import-path adjustments, plus the three deviation fixes in §8). Verbatim ports preserve subtle behavior — VAD state-machine thresholds, MiniMax WS framing, PortAudio ring buffer ramp math — that would be brittle to rewrite.

- `/Users/alllllenshi/Projects/jarvis-legacy/core/vad_silero.py` → `jarvis/surface/voice_audio.py` (VAD half)
- `/Users/alllllenshi/Projects/jarvis-legacy/core/audio_recorder.py` → `jarvis/surface/voice_audio.py` (recorder half)
- `/Users/alllllenshi/Projects/jarvis-legacy/core/speech_recognizer.py` → `jarvis/surface/voice_asr.py` (recognize half)
- `/Users/alllllenshi/Projects/jarvis-legacy/core/asr_normalizer.py` → `jarvis/surface/voice_asr.py` (normalize half)
- `/Users/alllllenshi/Projects/jarvis-legacy/core/wake_word.py` → `jarvis/surface/voice_wake.py` (engine half)
- `/Users/alllllenshi/Projects/jarvis-legacy/core/inherent_wake_listener.py` → `jarvis/surface/voice_wake.py` (orchestration half) — with the three deviation fixes applied
- `/Users/alllllenshi/Projects/jarvis-legacy/core/tts.py` → `jarvis/surface/voice_tts.py` (pipeline half)
- `/Users/alllllenshi/Projects/jarvis-legacy/core/tts_minimax_ws.py` → `jarvis/surface/voice_tts.py` (provider half)
- `/Users/alllllenshi/Projects/jarvis-legacy/core/audio_stream_player.py` → `jarvis/surface/voice_tts.py` (playback half)
- `/Users/alllllenshi/Projects/jarvis-legacy/core/media_ducking.py` → `jarvis/surface/voice_ducking.py`

Additional port (inline, not its own file):

- `/Users/alllllenshi/Projects/jarvis-legacy/core/tts_preprocessor.py` → folded into `voice_tts.py` as a private `_preprocess_for_speech(text) -> str` helper (~150 LOC, strips emoji / brackets / asterisks before TTS synthesis — preserves legacy text-cleanup behavior; no semantic gate).

The following are NOT ported in this ADR:

- `core/interrupt_monitor.py` — barge-in, deferred to ADR-0006.
- `desktop/inherent-swift/InherentCard/OutputSpeechPlayer.swift` — unused in legacy default path; left as-is in Swift, no migration.
- `desktop/inherent-swift/InherentCard/NativeVoiceRecorder.swift`, `SystemAudioDucker.swift` — Swift-side, unchanged.

## 15. Out-of-Scope / Open Questions Deferred

| Item | Deferred to |
|---|---|
| Barge-in (Allen speaks during TTS) | ADR-0006 |
| Streaming ASR / interim transcripts | future ADR |
| `surface.failed` durable emit | ADR-0007 |
| Cross-domain voice (RPi mic, ESP32 ambient) | future ADR + §3.7.4 cross-domain whitelist update |
| Multi-language wake word | future ADR (openwakeword supports custom models) |
| Confidence-as-projection / projection-level confidence threshold | future ADR — once Day-N consumers actually read the field |
| Image-while-speaking ("look at this and tell me what it is") — multimodal staging × voice | ADR-0004 followup |

---

**Definition of done for ADR-0005:**

1. `POST /inherent/asr-submit` returns 200 with normalized transcript for a valid WAV; the corresponding `utterance.received` event lands in the L2 log.
2. "Hey Jarvis" wake produces a `utterance.received` event end-to-end without touching the keyboard.
3. A normal assistant response routes through `voice_tts.py` and plays via MiniMax (or `macos_say` if MiniMax unavailable), respecting `required_gate_mode`.
4. All seven new `voice_*.py` files pass `lint-imports` against the L5 layer contract.
5. The three legacy spec deviations (§8) are demonstrably fixed (canaries + integration tests).
6. Allen runs the three smoke tests in §11 manually and confirms behavior matches.
