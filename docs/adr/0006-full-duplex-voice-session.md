# ADR-0006 — Full-duplex Voice Session

**Status:** Approved (2026-08-31, Allen)
Approved means the design is approved for implementation; implementation completeness is tracked only by §13 Definition of done.
**Date:** 2026-08-31
**Supersedes:** ADR-0005's explicit no-barge-in rule, wake-listener pause during TTS, whole-turn `VOICE_INPUT_LOCK` ownership, and the assumption that `spoken` means playback completed. The ADR-0005 PTT and whole-WAV paths remain supported as compatibility/fallback paths.
**Depends on:** ADR-0003 (resident Inherent event/watcher surface), ADR-0005 (current wake/PTT/ASR/TTS foundation), ADR-0009 (resident daemon and lifecycle).
**Paired with:** ADR-0008 (real-time response streaming, response cancellation, progress, and true asynchronous actions). ADR-0006 Steps 0–5 may land independently; Step 6 and later require ADR-0008's ResponseRun, interrupt-policy, response-ID, and terminalizer contracts. Conversely ADR-0008 Step 8 requires ADR-0006's accepted streaming-output capability.
**Does not use:** OpenAI Realtime API or any hosted speech-to-speech runtime. Cloud text LLMs and the existing MiniMax TTS provider remain allowed behind Jarvis-owned interfaces.

**Naming amendment (ADR-0014):** this ADR's original `generation_id` name means only an L5 playback lease and is renamed everywhere on the v2 wire, shared contracts, registries, and new event payloads to `playback_generation_id`. A ResponseRun itself is keyed only by `response_id`; document-only responses have no playback generation. Any unmodified prose occurrence of “generation” below describes provider work, not a second ResponseRun identity.

**D3 amendment (2026-09-05):** the built Input FSM slice is recorded under D3; D7 is unchanged; §4.1 `EndpointDecision` gains the `resume` verdict and `speech_resumed` reason.

---

## 1. Context

Jarvis already has several code-level media primitives needed for low-latency voice, but some are not connected to any production control path:

- a persistent PortAudio output stream and ring buffer in `jarvis/surface/voice_tts.py`; gain ramp, PCM duck/unduck, flush, `played_samples`, and the first-chunk callback exist as primitives but have zero production control callers today;
- local Silero VAD and SenseVoice ASR;
- a MiniMax WebSocket provider that can yield PCM chunks;
- wake word and PTT entry points;
- durable `utterance.received` and `surface.response_*` events.

The current composition is nevertheless half-duplex and batch-oriented:

1. `WakeListener` stops reading while TTS is playing. After wake, its persistent input stream remains open but the listener synchronously opens and consumes a second, per-utterance capture stream; the wake stream is unread during capture and overflow is ignored when reads resume.
2. Wake and PTT acquire `VOICE_INPUT_LOCK` for capture, complete ASR, normalization, and event emission.
3. `VoicePipeline.run_turn(audio_bytes)` accepts only a complete utterance.
4. `TTSPipeline` waits for a complete `<voice>` region, calls full `synthesize()`, receives all PCM, and only then writes to the player.
5. `spoken` is emitted after PCM is queued, not after it has actually been heard.
6. There is no production interruption/cancellation path. The unused `AudioStreamPlayer.flush()` primitive has no response generation identity and `write()` clears its abort flag, so it cannot be wired in safely without the new generation-aware control plane.
7. Input, response generation, action execution, and playback are treated as one serial turn even though they have different lifetimes.

This produces the wrong interaction contract. During a 10–40 second task Jarvis may be computing, speaking, waiting on an action, and listening to Allen at the same time. Those activities must be coordinated, but they must not share one lock or one cancellation token.

The six-layer architecture is not the cause of the delay. SQLite polling is currently around 10 ms and is not responsible for multi-second waits. The architectural problem is that every stage waits for the previous stage to finish in full. This ADR changes the L5 media path from “one complete turn” to a long-lived duplex session while preserving the six-layer ownership rules.

### 1.1 Legacy evidence

`jarvis-legacy` contains useful prototypes: `InterruptMonitor`, a persistent output player, turn-level MiniMax sessions, soft ducking, pre/post-roll, playback counters, and voice benchmark fixtures. It does not contain a production-ready full-duplex coordinator:

- Legacy HEAD calls `chat_stream(..., on_sentence=None)`, waits for the complete response, parses `<voice>/<document>`, then submits speech as a batch.
- Its interrupt monitor opens its own microphone stream.
- Cancellation stops TTS but not the cloud LLM.
- Its heard-text calculation estimates characters from audio duration and can count unheard text as heard.
- Its queues, timers, and per-turn event loops allow stale callbacks to leak into the next turn.

Therefore Legacy is an experiment and test-asset source, not a module-level port target.

## 2. Goals and non-goals

### Goals

1. Keep one long-lived local audio ingress and allow input detection during Jarvis playback.
2. Stream TTS PCM into a bounded low-latency player without waiting for complete audio.
3. Make output cancellation immediate and generation-safe.
4. Distinguish speech onset, confirmed barge-in, response cancellation, and action cancellation.
5. Persist what Allen actually heard, not everything the model generated.
6. Support partial ASR and semantic endpointing without making a second ASR engine authoritative.
7. Preserve PTT and the current whole-utterance path as an always-available fallback.
8. Degrade explicitly across headphones, speakers without AEC, speakers with AEC, MiniMax failure, and macOS `say` fallback.

### Non-goals

- Replacing Jarvis's cloud text LLM with a local full model.
- Reconstructing ChatGPT's internal voice implementation.
- Persisting raw audio frames, VAD probabilities, partial transcripts, PCM chunks, or playback cursor ticks in the Event Log.
- Making L5 decide whether an answer is safe, whether an action may run, or whether a real action should be cancelled.
- Claiming natural speakerphone barge-in before an AEC path passes live validation.
- Removing the ADR-0005 PTT endpoint.

## 3. Architectural decision

### D1. Keep the six layers; split media ownership from cross-layer coordination

The complete VoiceSession cannot live in L5 because it must observe L3 ResponseRuns and L4 ActionRuns. `jarvis.runtime` is already the only legal cross-sibling composition layer under `.importlinter`.

```text
L2  Event Log + ResponseLedger projection
        ↑ durable milestones only
        │
L3  ResponseStreamEngine / response policy / cancel decision
        ↕ frozen contracts from jarvis.shared.realtime
L4  ActionRunner / ActionHandle
        ↕
L5  DuplexVoiceSession / AudioIngress / ASR / TTS / Playback
        ↑
runtime.inherent_loop (coordinator, inlined)
        coordinates L2/L3/L4/L5; owns no media or business policy
```

Module ownership is fixed:

| Module | Layer | Responsibility |
|---|---|---|
| `jarvis/shared/realtime.py` | shared | frozen IDs, enums, messages, cancellation reasons, segment metadata |
| `jarvis/surface/voice_session.py` | L5 | `DuplexVoiceSession` façade and media-only state machines |
| `jarvis/surface/voice_audio.py` | L5 | the single `AudioIngress`, frame fan-out, VAD, utterance assembly |
| `jarvis/surface/voice_asr.py` | L5 | final ASR, rolling partial decode, normalization |
| `jarvis/surface/voice_interrupt.py` | L5 | speech candidate and barge-in evidence; never cancels an action |
| `jarvis/surface/voice_tts.py` | L5 | `TTSSession`, bounded PCM flow, fallback rules |
| `jarvis/surface/voice_ledger.py` | L5 | text-span to sample-span mapping and conservative heard boundary |
| `jarvis/runtime/inherent_loop.py` | runtime | the coordinator, inlined: foreground response, session generation, input arbitration, cross-layer cancellation wiring |
| `jarvis/state/conversation.py` | L2 | durable ResponseLedger / conversation heard-state fold (`fold_conversation_history`), composing `conversation_playback.py`'s `PlaybackHistory.fold`; `projections.py` only carries the result into the `SituationPacket` |

The coordinator is inlined in `jarvis/runtime/inherent_loop.py`; the originally planned `jarvis/runtime/realtime_session.py` / `RealtimeSessionCoordinator` is not created, and ADR-0008 D8's foreground-arbitration API lands inline too unless a later ADR extracts a module. `serve_inherent` constructs and closes the coordinator. CLI, scenario, and high-risk fallback paths continue to call existing `VoicePipeline.run_turn`, `drive_turn`, and `render_response` until their individual migration steps are complete.

### D2. One logical capture owner, with a replaceable duplex backend

The invariant is exactly one logical owner of local capture and render clocks, not exactly one fixed `sounddevice.InputStream`. Locking the architecture to separate sounddevice input/output streams would force another media rewrite when macOS VoiceProcessingIO AEC is introduced.

```text
AudioDuplexBackend
  start(frame_sink, render_source)
  stop()
  input_format()
  output_format()
  clock_mapping()
  capabilities()       # natural_barge_in, aec, reliable_dac_time, ...

SoundDeviceBackend
  RawInputStream + OutputStream; no software AEC

VoiceProcessingIOBackend
  owns the paired macOS AudioUnit input/output and echo processing

HardwareAECBackend
  consumes device-processed near-end input and exposes device clock data
```

An AEC backend must receive the final far-end render reference after resampling and Jarvis playback gain, plus the mapping between input ADC time, output DAC time, and the monotonic process clock. `EchoControl` is therefore a backend capability, not a sidecar boolean attached to sounddevice.

The initial `SoundDeviceBackend` capture callback makes fixed-size copies into one preallocated SPSC ring per subscriber. It never shares the callback-owned ndarray/buffer after callback return and never calls `asyncio.Queue` directly from the PortAudio thread. Internal frame slots carry:

```text
stream_epoch
sequence
device_sample_rate
channels
frame_count
adc_time
input_clock_domain
captured_monotonic_ns
discontinuity_before
sample_dtype
interleaving
```

Backend callbacks copy their native PCM layout (SoundDevice v1: little-endian int16, interleaved) only. Outside the callback, one `AudioIngressCanonicalizer` owns channel mixing and stateful resampling to canonical 16 kHz mono float32 frames. Subscriber adapters declare their exact view: Silero VAD receives 32 ms float32, openwakeword receives accumulated 80 ms 16 kHz mono int16, and SenseVoice receives 16 kHz mono data through its existing provider adapter. VoiceProcessingIO may use a different native device format but reaches the same canonical subscriber contract. Any discontinuity resets resampler, wake framer, VAD, and utterance assembly state together.

Initial subscribers are wake-word framing, utterance VAD/assembly, barge-in evidence during assistant output, and optional in-memory timing. The wake subscriber converts the canonical ingress frame cadence to its required 80 ms window; it does not open a second device stream.

Overflow rules are not uniform:

- wake-only/idle rings may drop oldest frames and rebuild their window;
- diagnostics may drop;
- an active utterance may not silently skip audio. Overflow marks a discontinuity and fails/restarts that utterance rather than sending discontinuous audio to ASR;
- a slow partial recognizer receives a coalesced/latest snapshot and cannot block capture.

The current “persistent wake stream stays open but becomes unread while a per-utterance capture stream is opened” behavior is retired in realtime modes. Migration canaries must reject stale/overflowed wake frames after capture handoff. PTT continues to upload a complete WAV, is resampled/normalized by its adapter, and joins the same `UtteranceCommitted` contract after final ASR.

### D3. Four orthogonal state machines

No single `is_speaking` or `current_turn` flag is allowed to represent the session.

#### Input FSM — L5

```text
dormant ── arm/wake/PTT ──> listening
listening ── speech onset ──> speech_active
speech_active ── acoustic pause ──> endpoint_pending
endpoint_pending ── speech resumes ──> speech_active
endpoint_pending ── commit policy ──> finalizing_asr
finalizing_asr ── normalize + utterance.received commit ──> committed
committed ── notify coordinator ──> listening
listening ── idle/privacy/device stop ──> dormant
listening/speech_active/endpoint_pending/finalizing_asr ── backend fault ──> device_unavailable
device_unavailable ── bounded reopen begins ──> recovering
recovering ── new stream epoch + capability re-evaluation succeeds ──> listening | dormant
recovering ── retry budget exhausted ──> device_unavailable
```

`dormant` does not necessarily mean the device is closed: the shared ingress may remain open for wake-word detection while utterance recognition is not armed.

The Input FSM did not ship as one enum. Device lifecycle is `BackendLifecycleState` (`jarvis/surface/voice_backend.py`: `closed | opening | open | closing | uncertain`); the capability surface is `InputCapabilityState` (`jarvis/surface/voice_audio.py`: `stopped | available | suspended | wake_unavailable | output_unavailable | local_capture_unavailable | close_uncertain`); `VadEvent` (`speech_active | silence`) is a per-frame label, not an utterance state. The `endpoint_pending`/`finalizing_asr` hold-and-commit semantics are D7's endpointing algorithm; the built slice (goal endpointing-partial-asr, 2026-09-05) realises them as the assembler's `EndpointPhase` enum: `speech_active ⇄ endpoint_pending → finalizing_asr → committed` for the current utterance, speech resuming during `endpoint_pending` returns to `speech_active`, and `committed` is set only after the normalized `utterance.received` commit. `listening`, `dormant`, `device_unavailable`, and `recovering` remain expressed by the ingress capability states, not by that enum.

`AudioDuplexBackend` owns physical open/read/callback/permission/device-loss failures. The runtime coordinator (D1) owns the cross-layer capability transition and user-visible degradation. Recovery uses bounded exponential backoff and always creates a new `stream_epoch`; subscribers never resume an old epoch. `InputCapabilityState` distinguishes `wake_unavailable` (PTT upload may still work) from `local_capture_unavailable` (text and remote/upload input may work); `text_only` did not ship as a value.

#### Playback FSM — L5

```text
idle → prewarming → buffering → playing → draining → drained
                                ↕
                              ducked

prewarming/buffering/playing/draining/ducked → flushed | failed
drained/flushed/failed → idle
```

`ducked` means Jarvis audio gain is temporarily lowered while speech is being classified. It is not yet a response cancellation.

No playback phase enum shipped. Playback progress lives as cursor and flag fields on `PlaybackLedger` (`jarvis/surface/voice_ledger.py`) and the registration/tombstone sets of `ActivePlaybackRegistry` (`jarvis/surface/voice_media.py`); `ducked` is `AudioStreamPlayer.duck()`'s gain ramp, not a state. The diagram above records the target.

#### ResponseRun FSM — L3, defined in ADR-0008

```text
idle → generating ↔ waiting_action → finalizing → completed
  any non-terminal state → cancelled | failed
```

Shipped verbatim as `ResponseRunState` in `jarvis/decision/response_run.py`.

#### ActionLifecycle — L4 taxonomy, L2 canonical truth

The existing eight-state L4 taxonomy and transition validator remain. `cancelled` remains a terminal state and this ADR does not add a ninth `cancel_requested` state. The canonical action truth is the durable L2 event fold; the in-process L4 FSM is a live validator/cache, not a replacement for Event Log arbitration.

These state machines use separate tokens. A Playback flush does not imply ResponseRun cancellation unless the runtime sends one; ResponseRun cancellation never implies ActionRun cancellation.

### D4. Playback interruption is generation-CAS, not a boolean flush

Each playable response has:

```text
session_id
turn_id
response_id
playback_generation_id  # minted only by L5; monotonically increases within the voice session
segment_sequence    # monotonically increases within the response
```

The player accepts a write only when its `playback_generation_id` equals the active playback generation. The control API is fixed:

```text
PlaybackActor.activate(
  session_id,
  response_id,
  conflict=reject | supersede,
  expected_active_playback_generation_id?
)
  -> GenerationLease(playback_generation_id, timeline_epoch)
   | ForegroundBusy
   | AlreadyStale

PlaybackActor.enqueue_after_drain(
  session_id,
  response_group_id,
  response_id,
  phase=commentary | final
)
  -> PresentationQueueReceipt | QueueFull

interrupt_playback(expected_playback_generation_id, reason)
  -> PlaybackInterruptSnapshot | AlreadyStale
```

Only the playback actor mints monotonically increasing generation IDs and activates a foreground lease. Concurrent response starts are serialized. `conflict=reject` changes nothing. `conflict=supersede` requires the exact expected active generation; the actor first closes/tombstones the old generation, discards unsubmitted PCM, and asks `PlaybackTerminalizer` to commit `interrupted(reason="superseded")`. Only after that commit succeeds may it mint and return the new writable lease. Superseding playback never terminalizes a ResponseRun by itself. Device restart/fallback cannot increment an integer independently—it requests a new lease/timeline epoch from the actor.

Normal commentary-to-final handoff is not supersession. The actor owns one bounded presentation lane per active session. `enqueue_after_drain` accepts only a response in the same `response_group_id`, is idempotent by `(response_id, phase)`, and does not mint a playback generation until the current response reaches a playback terminal. It then activates the queued successor in order. Durable ResponseRun/group milestones preserve semantic continuation debt across restart; L5 itself restarts with an empty lane, and runtime asks L3 to reconcile any unstarted successor rather than replaying historical speech events. A user stop applies L3 policy to the active and queued speech responses in that group; document siblings remain independent.

The single playback actor performs compare-and-swap semantics:

1. if `expected_playback_generation_id != active_playback_generation_id`, return `AlreadyStale` and change nothing;
2. close the expected generation's write eligibility;
3. freeze its ledger and publish a kill/discard boundary;
4. clear old PCM that has not been submitted to the device callback;
5. snapshot the old generation's output timeline;
6. atomically append its unique playback terminal: if the conservative audible horizon already crossed the final segment, `completed` wins; otherwise the cancel produces `interrupted`;
7. allow a separately activated newer generation to proceed.

A late cancel for generation N can never clear generation N+1. Ring cursors are not reset by an arbitrary third thread: one media actor owns reserve/write/discard state, slots carry generation tags, and the callback validates the kill boundary before copying.

Audio already submitted to CoreAudio/PortAudio host buffers cannot be recalled by clearing the ring. Small device buffers, a callback kill flag, DAC-time estimation, and physical loopback measurement account for that tail.

This replaces the current abort boolean, which a later `write()` can clear. Generation/epoch equality is checked at every asynchronous boundary, including TTS reader events, timers, player reserve/write, ASR callbacks, and device restart.

### D5. Streaming TTS has one command writer and one WebSocket reader

The provider contract becomes:

```text
TTSSession
  open(response_id, playback_generation_id) -> awaitable
  send(ResponseSegment) -> awaitable
  audio_events() -> one AsyncIterator[AudioChunk | SegmentFinished]
  finish() -> awaitable
  abort(reason) -> awaitable
  close() -> awaitable
```

Required semantics:

- transport/TLS prewarm is asynchronous and never blocks LLM request dispatch; it is distinct from opening a response-scoped logical session with an idle timeout;
- one response may feed multiple sentences over one WebSocket where the provider supports it;
- segment commands enter one bounded queue; sends are serialized and unexpected concurrent sends fail explicitly;
- the media actor is the only caller of `TTSSession.send()`; other producers enqueue `ResponseSegment` commands to that actor rather than racing `send()` directly;
- exactly one reader task owns `recv()` and routes PCM to the current segment; two iterators never compete for the same WebSocket;
- PCM is written to the player as it arrives;
- synthesis-to-playback uses bounded queues and backpressure;
- cancel order is **CAS-interrupt playback first**, then cancel L3 generation and close TTS network work asynchronously;
- both send and receive refresh the idle watchdog; the watchdog cannot close a connection with an active feed;
- v1 uses a new per-segment resampler and finalizes it only at `SegmentFinished`;
- no PortAudio callback invokes user code.

`AudioChunk` is a private L5 type, not a cross-layer shared contract. V1 canonical PCM passed to the player is little-endian interleaved float32 with explicit sample rate/channel fields; the player assigns output spans only to samples it actually accepts for the active generation.

Fallback is prefix-safe:

1. If a provider fails before any PCM for the segment becomes visible to playback, the segment may be retried from the start on the next provider.
2. “Visible to playback” means the active-generation player successfully accepted at least one resampled sample, not merely that the provider yielded bytes.
3. If any PCM has been accepted, automatic replay from the segment start is forbidden because it would repeat speech.
4. After partial output failure, the safe v1 behavior is to terminate that speech ResponseRun, record the partial-prefix failure, and leave the panel/document route independent. It does not skip into the next sentence and create a semantically broken spoken answer.
5. ADR-0007 remains responsible for the general `surface.failed` fallback event family.

### D6. PlaybackLedger uses sample spans and is conservative

Every permitted speech segment is assigned a `SpeechChunk`:

```text
SpeechChunk
  response_id
  playback_generation_id
  segment_sequence
  text
  text_start
  text_end
  timeline_epoch
  output_start_cursor
  output_end_cursor?       # set only when SegmentFinished closes the chunk
  sample_rate
  cursor_quality
  audibility_class         # normal | attenuated | muted | unknown
  segment_hash
```

The ledger does not mix provider, resampler, ring, and device cursors. A segment is `open` while PCM streams, receives output-timeline spans only when generation-valid resampled samples are accepted, and becomes `closed` at `SegmentFinished`. It records:

```text
provider sample position       # diagnostic only
enqueued output cursor         # accepted into player timeline
submitted output cursor        # handed to host callback
estimated audible cursor       # DAC-time/monotonic mapping minus safety margin
```

The backend uses `outputBufferDacTime` (or its native equivalent) to map the continuous output timeline to the monotonic clock. Starvation, inserted silence, device restart, the post-resample gain/kill envelope, and known system-output mute state advance or reset explicit timeline epochs; they are never hidden inside one cumulative counter. `cursor_quality` is `measured_dac`, `estimated`, or `unknown`, and all boundary calculations round backward. Ring empty is not completion: the audible horizon must conservatively pass the segment end.

The current `_played_samples` counter is explicitly forbidden as a heard-state input: it increments before playback gain is applied and before the device's DAC horizon. V1 advances heard state only through a complete segment whose final post-gain frames have crossed the conservative audible horizon with `audibility_class=normal`. Any interval that is muted, below the configured conservative intelligibility gain, affected by an unknown external/system gain, or killed mid-segment marks that segment `attenuated|muted|unknown` and does not advance `heard_through_sequence`. This deliberately under-counts ducked speech rather than claiming that quiet samples were heard.

V1 heard-text rule:

- a completely played segment counts as heard only when the post-gain ledger classified the whole segment `normal`;
- an interrupted partial segment does not count unless the provider later supplies reliable word timestamps;
- `heard_text` is the concatenation through the last fully played segment;
- no character-ratio estimate and no forward punctuation snapping are allowed.

`text_start/text_end` are Unicode code-point offsets into the exact normalized speech string. Speech segments have a separate safe-subclause limit (initially 60 characters or roughly 2.5 seconds, whichever boundary is reached first); every split subclause goes through its own stream gate and permit. An arbitrary buffer cut is never spoken.

Durable replay uses L5-owned content mappings, not a self-hash of claimed heard text:

- `surface.playback_started` binds a playback lease to its committed surface source, response/turn, phase/channel, session, and normalized speech hash. An incremental lease (`incremental: true`, minted from the first permitted segment of a `kind="stream"` response) binds only its first segment's hash at activation; the full normalized speech hash is bound by `surface.playback_completed` against the concatenation of prepared segments.
- Before provider submission or player segment activation, `surface.playback_segment_prepared` binds the exact normalized speech text and hash to its original surface chunk UID, raw segment hash, sequence, and playback activation UID. The voice text of `surface.response_emitted` that no chunk carried is one final segment sourced from that emitted row's UID. L5 owns normalization; L2 validates these facts without reimplementing speech extraction.
- Checkpoints and playback terminals reference the activation and carry the explicit conservative `heard_text` plus hash. L2 accepts only the exact concatenation of prepared segments through the cursor, with monotonic sequence/sample counts. A self-consistent hash alone is insufficient. Older rows without these mappings remain unknown.
- The canonical terminal key remains `response_id + playback_generation_id`; its session cannot be rebound. Only the current activation can advance the cursor. Starting another lease preserves previously proven heard words, and late callbacks cannot extend them.
- Typed conversation context keeps `spoken_heard` and `panel_available` separate. Audit drafts remain excluded from the conversational prompt. The projection retains at most 20 turns and 8 responses per turn, with 65,536 text characters and 1,024 facts per response. The L3 prompt limits each text excerpt to 2,048 characters and its serialized history JSON to 12,000 characters, marking omissions explicitly.

This may under-count a few words Allen actually heard. That is preferable to contaminating future context with words he did not hear.

For macOS `say`, no sample-accurate provider cursor exists. It is treated as an atomic segment: killing the subprocess leaves that segment uncommitted in heard history. Natural barge-in may still stop it, but the event records `cursor_quality="unknown"`.

### D7. Endpointing is acoustic first, semantic second, with a hard bound

The system keeps SenseVoice as the sole authoritative transcript producer. V1 partial ASR may repeatedly decode a bounded rolling audio snapshot on a serialized ASR lane; it is not a second authoritative recognizer.

Partial work uses a latest-only/coalescing queue with at most one decode in flight. Final ASR has strict priority: endpoint commit cancels all queued partial work, invalidates late revisions by `utterance_id/revision`, and runs final decode next. If partial decode exceeds its CPU/queue budget, the turn automatically degrades to acoustic endpointing rather than building an O(n²) backlog.

`stable_prefix` advances only after the same normalized code-point prefix survives two consecutive revisions; a revision may replace only the unstable suffix. Final ASR is free to replace the entire partial hypothesis and is the only text persisted.

Endpoint decision inputs:

- Silero speech probability and duration;
- recent speech resume events;
- stable/unstable partial transcript;
- punctuation and obvious unfinished syntax;
- PTT release or explicit end command;
- hard maximum hold time.

The algorithm is:

1. Acoustic pause creates `EndpointDecision(verdict="hold")` and starts a short candidate window.
2. If speech resumes, return to `speech_active` without committing.
3. If the stable prefix looks complete, move to `finalizing_asr` early.
4. If it looks incomplete, hold only until `endpoint.max_hold_ms`.
5. PTT release moves directly to prioritized final ASR.
6. Only normalized `utterance.received` commit produces the `committed` state.

Initial configuration values are calibration candidates, not promises:

| Parameter | Initial candidate |
|---|---:|
| frame | 32 ms |
| pre-roll | 500 ms |
| post-roll | 160–240 ms |
| partial decode interval | 240 ms |
| acoustic endpoint candidate | 192–320 ms silence |
| semantic max hold | 900 ms |
| maximum utterance | 30 s |

The current production issues are fixed before tuning: reset Silero state for every utterance, prewarm it with silence, and make the stop rule actually honor consecutive misses instead of ending at the first non-speech frame after ACTIVE.

### D8. Barge-in is two-phase and uses an out-of-band control path

The normal intent watcher cannot be the only stop path: it currently waits for `drive_turn` and can leave a stop utterance queued behind the response it should interrupt.

**What shipped (`keyword-ptt-safe-barge-in`).** Both stages are *spoken*,
per D9: there is no VAD candidate stage, because without validated AEC the
speaker's own audio produces speech verdicts constantly. The candidate is a
**wake hit while output is active**, which opens a bounded
`candidate_window_ms` window and arms capture so the partial-ASR lane runs;
the confirm is a configured **interrupt keyword found in a partial-ASR
revision inside that window**, or a **PTT upload during output**, which is a
deliberate press and cannot be echo. A window that elapses with no keyword is
dropped as typed telemetry and cancels nothing. `natural` is downgraded to
`keyword_two_stage` with one warning, and `keyword_two_stage` without
`partial_asr.enabled` fails closed to PTT confirms only. Config:
`realtime.single_audio_ingress.barge_in.{enabled, candidate_window_ms,
confirm_timeout_ms, interrupt_keywords}`, off by default.

The shipped `BargeInRouter` (L5, `jarvis/surface/voice_interrupt.py`) is the
receiver of `BargeInSignal` values and nothing else: it owns the window, the
keyword matcher, and one injected interrupt callable bounded by
`confirm_timeout_ms`. It is a different object from the transcript-routing
`InterruptUtteranceRouter` described at the end of this decision, which stays
unbuilt and keeps its name. Phase 1's `duck_gain` ramp and F11's unduck are
**deferred, not rejected**, to a later card; nothing ducks today.
`ResponseInterruptPolicy.confirmed_playback` now defaults to
`interrupt_expected_playback_generation`, so the runtime's mechanical
application of the L3-issued policy has an effect instead of always ignoring.

Three boundaries of what shipped, all of them consequences of naming the
target from `ResponseRunRegistry.open_runs()` rather than from a playback
lease:

- **A confirmed barge-in can only stop a run that is still open.** A run is
  unregistered when generation ends, while its audio is still in the TTS
  queue, so an interrupt spoken over that playback tail returns `no_open_run`
  and the speech continues. Stopping the tail needs the `foreground_output`
  scope and its playback lease, which is the stop-speech card's work.
- **The target is the single open run, not the run that owns the current
  playback generation.** With more than one open run the runtime cancels
  nothing (`ambiguous_open_runs`); with exactly one it cancels that run even
  if the audible speech belongs to a different, already-closed one.
- **A candidate arms capture, so a dropped window still produces a turn.** The
  speech that failed to confirm is committed as an ordinary
  `utterance.received` and becomes the next question. Without AEC, speaker
  self-wake can therefore start an echo-driven turn — which is why barge-in
  ships off by default.

Two phases:

1. **Speech candidate**
   - detected directly from shared AudioIngress;
   - player ramps to `duck_gain` immediately;
   - ResponseRun, TTS session, and action continue;
   - the utterance continues accumulating, so a confirmed interruption becomes the next full question rather than losing its first words.
2. **Confirmed barge-in**
   - detector emits `BargeInSignal(phase="confirmed")` to the runtime coordinator (D1) through an in-memory priority control queue;
   - runtime mechanically applies the L3-issued `ResponseInterruptPolicy` for this active response;
   - L5 first calls `interrupt_playback(expected_playback_generation_id)` and returns a CAS snapshot or `AlreadyStale`;
   - runtime then submits the policy-bound `ResponseCancelRequest` to L3 and aborts the TTS network task; cloud LLM/TTS cancellation proceeds concurrently after new audio submission has stopped. Already-submitted device-buffer tail is tracked by the DAC/loopback silence SLO, not claimed to vanish at CAS return;
   - the linked ActionRun continues unless a separate, explicit cancel-action decision is later authorized.

False candidate:

- if the signal is echo/noise or speech does not reach the confirmation threshold, the player unducks with a short ramp;
- no durable response-cancel event is emitted;
- false candidates are counted in telemetry.

The out-of-band path may stop presentation only under the already-issued L3 interrupt policy. L5 produces evidence and performs time-critical playback control; only L3 can emit semantic `response.cancelled`. The path is forbidden from invoking tools or cancelling real actions.

Final ASR text from a barge-in passes through a closed, deterministic `InterruptUtteranceRouter` before confirmation grammar, Tier 0, or the LLM. Standalone control phrases such as “别说了”, “停止说话”, and their configured exact English equivalents apply only the active `ResponseInterruptPolicy`, are deduplicated by `utterance_id` against an already-applied early `BargeInSignal`, and do not create an ordinary ResponseRun. A compound utterance such as “别说了，改查 X” stops presentation and submits only the normalized remainder as a new intent. This router can never map a phrase to ActionRun cancellation.

### D9. Headphones, speakers, and AEC are different product modes

Safety is keyed by an observed device profile, not by a remembered global setting:

```text
DeviceProfileKey
  input_uid
  output_uid
  backend
  route_kind              # headphones | speaker | hardware_aec | unknown
  aec_mode
  input_sample_rate
  output_sample_rate

DeviceProfileSnapshot
  key
  stream_epoch
  validation_record_hash?
  allowed_barge_mode      # ptt | keyword_two_stage | natural
```

`AudioRouteObserver` watches default input/output, permission, data-source/transport, and route changes. Any change first revokes the old profile's natural-barge permission, increments `stream_epoch`, and downgrades to the safe `unknown/speaker_no_aec` behavior. Only then may the new exact `DeviceProfileKey` be resolved and a matching accepted validation record restore a stronger mode. An AirPods disconnect therefore cannot inherit the headphones profile while output moves to built-in speakers. Unknown or partially observed profiles fail closed to PTT/two-stage keyword.

| Output profile | Allowed barge-in behavior |
|---|---|
| headphones | natural speech candidate + partial-ASR confirmation |
| speakers, no validated AEC | speech candidate may duck; hard cancel requires PTT or a two-stage wake phrase + interrupt keyword |
| speakers, validated software AEC | natural barge-in after live-burn acceptance |
| hardware AEC mic array | natural barge-in after device-specific acceptance |

Legacy's stricter VAD threshold is not AEC. A single word such as “停” is also unsafe without AEC because Jarvis may say that word itself. Speaker/no-AEC mode requires PTT or wake-phrase-plus-keyword confirmation, checks the candidate transcript against current far-end speech as an exclusion signal, and is validated against synchronized far-end/mic recordings.

Natural speakerphone barge-in remains disabled until `VoiceProcessingIOBackend` or a hardware path such as XVF3800 proves residual echo, near-end recall, double-talk, and false-cancel targets. This ADR does not silently add a media framework or choose hardware. The 2026-09-05 spike measured that path on this MacBook and came back inconclusive — `VoiceProcessingIO` runs and still yields a 16 kHz mono capture, but leaves `record`-profile false candidates 15× over target, does not separate cancellation from its own AGC, and cannot reach the near-end and double-talk gates without a human trial (`docs/live-burn-2026-09-05-voiceprocessingio-aec.md`).

Initial per-device acceptance thresholds, measured separately for built-in speaker/mic and each external profile at three volume/distance settings, are: near-end interrupt recall ≥95%, double-talk near-end recall ≥90%, false hard cancel ≤0.1/hour over at least 10 aggregate hours, false candidate ≤0.5/minute, and physical loopback interrupt silence p95 ≤350 ms. Failing any profile keeps that profile in PTT/two-stage-keyword mode.

Per-turn synchronous `osascript` mute/restore is removed from the realtime hot path. Jarvis's own playback is ducked in-process through the player gain. Existing system-media ducking remains a separate, refcounted presentation feature and cannot be used as echo cancellation.

### D10. Replay, compatibility state, and telemetry are explicit

TTS consumes only `(boot_id, session_id, response_id, playback_generation_id)` tuples registered as active by the current coordinator. Historical `surface.response_chunk` rows are projection/panel data and are never spoken after daemon restart. Same-process poll recovery may deliver a committed segment missed by the direct bus only while that exact active tuple still exists.

Registry lifecycle is fixed:

1. L5 `ActivePlaybackRegistry` registers the tuple before the first `surface.response_chunk` append.
2. The internal direct/watcher envelope carries `boot_id`; durable realtime surface events carry `session_id/response_id/playback_generation_id` when speech playback exists.
3. L3 `response.completed` does not unregister playback; the tuple stays active while legal tail segments synthesize/play.
4. CAS interrupt atomically tombstones the expected generation before queues are cleared.
5. `PlaybackTerminalizer` appends the terminal in the same transaction that wins terminal ownership, then the tuple becomes terminal/tombstoned. All late direct/watcher messages receive stale rejection.
6. Inside the startup barrier, TTS reads `SELECT COALESCE(MAX(id), 0) FROM events` and stores it as `boot_high_water_event_log_id`. Only rows with `events.id > boot_high_water_event_log_id` whose tuple is still in the active registry are eligible for same-process recovery; wall-clock time and `event_uid` are never ordering boundaries.
7. Missing/terminal/tombstoned tuples are never reactivated by an event.

The migration compatibility method is `is_output_active`, a single boolean over pending output work; D3's playback phases are unbuilt, so it discriminates none of them. Old `is_speaking()` callers delegate to it according to rollout mode until removed; they may not infer silence solely from ring bytes during synthesis.

Required counters include stale-generation drops, stale-cancel no-ops, input discontinuities, callback deadline misses, ring starvation, WS reconnect-before-exposure, partial-prefix failures, cursor-quality distribution, and active-generation replay rejects.

## 4. Contracts

### 4.1 Ephemeral shared messages

These frozen contracts are to live in `jarvis/shared/realtime.py` and do not enter the Event Log. None of the four is built yet; `PlaybackProgress.state` follows D3's unbuilt Playback FSM:

```text
PartialTranscript
  session_id: str
  utterance_id: str
  revision: int
  stable_prefix: str
  unstable_suffix: str
  started_at_ms: int
  ended_at_ms: int | None
  confidence: float | None

EndpointDecision
  utterance_id: str
  verdict: hold | resume | commit
  reason: acoustic_pause | semantic_complete | max_hold | speech_resumed | ptt_release | explicit_end
  confidence: float | None

BargeInSignal
  session_id: str
  utterance_id: str                  # minted at speech onset, before final ASR
  response_id: str
  playback_generation_id: int
  phase: started | confirmed | released
  input_sample_cursor: int
  input_sample_rate: int
  input_clock_domain: str
  playback_sample_cursor: int
  output_sample_rate: int
  output_clock_domain: str
  observed_monotonic_ns: int
  confidence: float | None

PlaybackProgress
  response_id: str
  playback_generation_id: int
  state: buffering | playing | draining | silenced
  submitted_samples: int
  heard_through_sequence: int | None
  silence_at_monotonic_ns: int | None
  cursor_quality: str | None
```

Raw frames and PCM `AudioChunk` remain private to L5. Input/output sample cursors are snapshots from different clock domains and are never directly subtracted; the monotonic clock mapping is the comparison surface.

`surface.playback_interrupted` may be committed when CAS closes the playback
lease, before already-submitted host buffers become inaudible. The playback
actor therefore emits the same-boot/same-generation ephemeral
`PlaybackProgress(state="silenced")` only after the conservative DAC horizon
crosses the kill boundary. UI may say “正在停止” on the terminal/control ACK;
only this progress state (or loss of the old boot/device stream) proves “已停止.”

`ResponseInterruptPolicy`, `ResponseCancelRequest`, and cross-layer `ResponseSegment` are defined and owned by ADR-0008's extension to this shared module. ADR-0006 creates the base session/utterance/barge contracts; the two ADRs must not create competing versions of `jarvis/shared/realtime.py`.

### 4.2 Durable event changes

New L5 events:

```text
surface.playback_checkpoint
  required: session_id, response_id, turn_id, playback_generation_id,
            heard_through_sequence, submitted_samples, heard_text_hash
  optional: cursor_quality

surface.playback_completed
  required: session_id, response_id, turn_id, playback_generation_id,
            heard_through_sequence, submitted_samples, speech_text_hash
  optional: total_samples, provider, cursor_quality

surface.playback_interrupted
  required: session_id, response_id, turn_id, playback_generation_id,
            heard_through_sequence, submitted_samples, heard_text_hash, reason
  optional: heard_text, total_samples, interrupted_by_utterance_id,
            interrupted_by_turn_id, provider, cursor_quality

surface.playback_failed
  required: session_id, response_id, turn_id, playback_generation_id,
            heard_through_sequence, submitted_samples, heard_text_hash, reason
  optional: heard_text, provider, cursor_quality, retryable
```

Additive optional fields on existing `utterance.received`:

```text
session_id
utterance_id
endpoint_reason
interrupted_response_id
```

`heard_through_sequence` is a required key whose value may be null when no complete segment was heard. `heard_text` is bounded; when omitted or over the payload cap, the projection reconstructs it from durable permitted response segments through that sequence. A checkpoint is emitted asynchronously only when a whole segment crosses the conservative audible horizon, not for cursor ticks.

`submitted_samples` is the generation-local count handed to the host output timeline, not an assertion that those samples are already acoustic. Heard state uses `heard_through_sequence` and cursor quality.

The sole exit for `completed/interrupted/failed` playback is the L2 atomic append primitive `terminalize_playback` (`jarvis/state/lifecycle_terminal.py`), keyed by `(response_id, playback_generation_id)`: inside the same `BEGIN IMMEDIATE` transaction it verifies no terminal exists, appends the canonical terminal event, and commits, returning `Event | AlreadyTerminal`. There is no separate claim marker or post-claim emit window. Checkpoints are non-terminal. No class named `PlaybackTerminalizer` exists; L5 reaches this primitive through two callers today — `voice_media.py`'s `_commit_terminal` and the boot reconciler in `playback_recovery.py` — and the at-most-one-terminal-per-`(response_id, playback_generation_id)` invariant holds across both because they share the one CAS primitive.

The new `response.*` lifecycle and additive `surface.response_*` fields are owned by ADR-0008.

### 4.3 ResponseLedger projection

L2 folds response lifecycle, permitted durable speech segments, and playback terminal events:

- completed playback → future conversation context contains full delivered speech;
- interrupted playback → future context contains only the conservative `heard_text` prefix;
- generated but unplayed text remains auditable but is never presented to L3 as already heard;
- hard crash recovers only to the last durable whole-segment checkpoint; it never guesses forward from ring state that disappeared with the process;
- a response has at most one playback terminal event per generation.

The projection does not keep raw PCM or per-callback progress.

## 5. Configuration and rollout

New configuration is parsed into a typed object; no realtime constant remains hard-coded in `inherent_loop.py`.

Canonical configuration is the top-level `realtime:` block in `config/jarvis.yaml`, a sibling of `llm`/`supervisor`/`observer`/`tools`/`confirmation`; there is no `voice:` namespace, and the rollout modes below are cumulative capability levels, not a config value. Keys this ADR gates:

- `realtime.enabled` — master switch; nothing below activates without it.
- `realtime.concurrency_safety.{transactional_event_append,lifecycle_terminal_cas}` — Wave-1 durability primitives both switches below require.
- `realtime.streaming_output.enabled` — streaming TTS/player/ledger (the `streaming_output` level).
- `realtime.single_audio_ingress.enabled` — shared capture backend and subscriber ring (the `keyword_barge_in`/`full_duplex` levels).

The two adoption switches are read in `jarvis/runtime/inherent_loop.py`. Per-field tuning (frame sizes, buffer targets, duck gains, timeouts) lives only in `config/jarvis.yaml`; this ADR does not restate it.

Rollout modes are cumulative:

- `legacy` (`realtime.enabled: false`): exact ADR-0005 behavior.
- `streaming_output`: streaming TTS/player/ledger, no barge subscriber during playback.
- `keyword_barge_in`: shared ingress, candidate duck, keyword/PTT hard cancel.
- `full_duplex`: partial ASR, semantic endpoint, natural barge-in only for an accepted output profile.

Startup prewarms VAD, ASR, TTS transport, and output device in the background. Missing optional capabilities downgrade to the highest safe lower mode and produce one clear operator warning.

Startup builds a capability matrix for `sounddevice`, `onnxruntime`, SenseVoice model/runtime (including the current optional/manual `sherpa-onnx` wheel), `soxr`, MiniMax transport, output DAC timing, and selected AEC backend. No partial ASR downgrades `full_duplex` to keyword/PTT endpointing; no MiniMax leaves text plus `say` fallback; no validated AEC prevents natural speaker mode. Legacy's incomplete requirements file is never treated as the dependency source of truth.

Privacy/sleep/device lifecycle is explicit: privacy stop closes the capture backend, ordinary `dormant` may leave wake-only capture active, before-sleep stops capture then output, and wake/permission/default-device changes create a new stream epoch before subscribers resume. Runtime faults use the Input FSM's `device_unavailable/recovering` path and publish a capability delta; no L5 retry loop silently decides that the system is text-only.

## 6. Failure modes

| F# | Failure | Required behavior |
|---|---|---|
| F1 | old PCM arrives after cancel | generation mismatch drops it; metric increments; no ring write |
| F2 | old partial-ASR/Timer callback arrives | session/utterance epoch mismatch drops it |
| F3 | input subscriber ring fills while idle | drop oldest and rebuild wake/pre-roll window; never block callback |
| F4 | input ring fills during active utterance | mark discontinuity; fail/restart utterance; never feed a gapped waveform to ASR |
| F5 | partial ASR is slow/fails | coalesce/cancel partial backlog; endpoint uses acoustic hard bound; final ASR remains authoritative |
| F6 | final ASR fails | no `utterance.received`; UI returns retry; session returns to listening |
| F7 | MiniMax fails before player accepts PCM | retry next provider from segment start |
| F8 | MiniMax fails after player accepts PCM | never replay/skip ahead; terminalize current speech response as partial failure |
| F9 | player underflow | record ring starvation separately from PortAudio status; continue if generation valid |
| F10 | output device changes/restarts | CAS-interrupt active generation, create new stream epoch, reopen device, emit one terminal milestone |
| F11 | false speech candidate | unduck; response/action unchanged; no durable cancel |
| F12 | speaker echo without AEC | PTT or two-stage wake+keyword only; natural mode rejected at config validation |
| F13 | macOS `say` interrupted | kill process; cursor quality unknown; current segment excluded from heard text |
| F14 | shutdown mid-session | stop ingress, CAS-interrupt playback, cancel network tasks, close output/backend, then release system ducking |
| F15 | duplicate direct+watcher delivery | panel drops duplicate by `(event_uid, response_id, sequence)`; speech also validates `playback_generation_id` |
| F16 | daemon restarts with historical chunks | projection/panel may rebuild; TTS never plays a chunk outside the current boot's active-generation registry |
| F17 | input open/read/callback/permission/default-device failure | backend closes the failed epoch; coordinator publishes the precise capability downgrade and performs bounded recovery; text/PTT-upload paths remain available when their capabilities still exist |
| F18 | output route changes from accepted headphones/AEC profile to unknown or speakers | revoke natural barge-in before reopening, interrupt the old playback generation if required, create a new stream/profile epoch, and remain PTT/two-stage until that exact profile is accepted |

## 7. File-level change map

This section is a historical seed for goal cards under `docs/goals/`, not an acceptance contract; §10 Verification and SLOs and §13 Definition of done remain binding.

### New files

- `jarvis/shared/realtime.py`
- `jarvis/state/lifecycle_terminal.py` — created in Step 2; L2 atomic check+append terminal primitive later reused by ADR-0008.
- `jarvis/surface/voice_session.py`
- `jarvis/surface/voice_backend.py`
- `jarvis/surface/voice_interrupt.py` — not yet created.
- `jarvis/surface/voice_ledger.py`
- `jarvis/surface/voice_media.py` — persistent L5 media owner; sole owner of TTS sessions and playback generations, wired from `inherent_loop.py`.
- `jarvis/state/conversation.py` — conversation heard-state fold (D1).
- `jarvis/state/conversation_playback.py` — `PlaybackHistory.fold`, the per-response playback-cursor fold that produces the conservative `heard_text` prefix.
- `jarvis/decision/conversation.py` — builds the bounded spoken-heard/panel-available history context wired into the model prompt from `jarvis/decision/__init__.py`.
- `scripts/bench_voice_realtime.py` — not yet created.
- `scripts/bench_interrupt_latency.py` — not yet created.

### Existing files that must change

- `jarvis/runtime/inherent_loop.py` — construct, start, and close the session and the inlined coordinator (D1); replace long-lived voice watcher assumptions.
- `jarvis/state/event_log.py` — event registry additions plus validated `append_event_in_transaction()` that never commits or publishes; public `emit_event()` remains the one-event compatibility wrapper.
- `jarvis/state/projections.py` — carries `conversation_history` into the `SituationPacket` by calling `conversation.py`'s fold; owns no fold logic.
- `jarvis/decision/packet.py` — carry bounded structured spoken-heard, panel-available, and audit-only response context into the SituationPacket without conflating them.
- `jarvis/surface/voice_audio.py` — one ingress, reset, real endpoint counters, pre/post-roll.
- `jarvis/surface/voice_asr.py` — prewarm, serialized rolling partials, final authority.
- `jarvis/surface/voice_wake.py` — subscribe to ingress; no pause-during-TTS and no second capture stream in realtime modes.
- `jarvis/surface/voice_pipeline.py` — preserve batch adapter; emit the new committed-utterance metadata.
- `jarvis/surface/voice_tts.py` — generation-safe player, response-scoped TTS session, bounded streaming.
- `jarvis/surface/voice_ducking.py` — keep system-media duck separate from Jarvis playback duck.
- `jarvis/surface/inherent_server.py` — PTT compatibility adapter and optional future streaming-upload seam.
- `jarvis/surface/inherent_output.py` — new playback phases without claiming queued PCM is heard.
- `config/jarvis.yaml` — typed realtime configuration.

### Canaries/tests that must be amended, not deleted

- replace “wake listener pauses while TTS” with “one AudioIngress owns the device and barge mode controls subscribers”;
- replace “`VOICE_INPUT_LOCK` is held during emit” with “one ingress owns frames; final transcript is normalized before durable emit; commit writer is serialized”;
- preserve PTT-to-`utterance.received`, normalization-before-emit, layer import, system-turn-never-TTS, wake shutdown, and TTS event ordering tests.

## 8. Legacy reuse matrix

| Legacy asset | Decision |
|---|---|
| `core/audio_stream_player.py` RingBuffer / GainRamp math and tests | current implementation is equivalent; audit and reuse selected tests/math, not the file wholesale |
| `core/vad_silero.py` pre/post-roll and reset-silence warmup | adapt and recalibrate per device |
| `core/interrupt_monitor.py` soft candidate/hard confirm idea | adapt only the state-machine idea into subscriber-only `BargeInDetector` |
| `core/tts_minimax_ws.py` turn-level `task_continue` | adapt the concept into single-reader `TTSSession`; do not port concurrency behavior |
| `core/tts.py` top-level pipeline | reject; its ownership, queues, abort order, and heard mapping are unsafe |
| sentence-boundary fixtures | reuse and extend |
| JSONL benchmark/config snapshots | reuse schema/fixture organization only; redefine timestamps and do not inherit Legacy pass/fail claims |
| `_wp5_truncate` | reject; replace with conservative sample ledger |
| independent mic listener | reject |
| per-turn threads/private event loops/unbounded queues | reject |
| `afplay`/SIGSTOP pause | reject |
| `[Interrupted by user]` fabricated history message | reject; use durable response/playback events |

## 9. Build order

This section is a historical seed for goal cards under `docs/goals/`, not an acceptance contract; §10 Verification and SLOs and §13 Definition of done remain binding.

Every step leaves `realtime.enabled: false` (`config/jarvis.yaml`) as a working fallback and keeps Tier 1 green. Per repository policy, Python verification uses canaries, data-driven regression checks, integration/replay harnesses, and required live burns; these steps do not recreate `tests/unit` or add new Python unit tests.

| Step | Change | Verification |
|---:|---|---|
| 0 | Audit/rewrite selected Legacy fixtures; add ADC/DAC/monotonic latency trace; fix per-utterance Silero reset/prewarm and endpoint consecutive-miss counter; close the synth-before-first-PCM system-mute race by entering `prewarming` before provider I/O and mapping legacy `is_speaking()` to `is_output_active` | data-driven two-consecutive-utterance VAD regression; synth/wake race canary; legacy mode comparison; no inherited Legacy pass/fail conclusions |
| 1 | Create base shared session contracts, config/capability parser, FSMs, playback event schemas/checkpoint, device-profile resolver/observer, and feature flags with no behavior change | transition tables; route-change fail-closed and capability-downgrade integration checks; import-linter |
| 2 | Add L2 same-transaction terminal check+append primitive and L5 PlaybackTerminalizer; make current player generation-CAS with output-timeline/gain ledger; add atomic supersede and bounded after-drain presentation lane | deterministic drain/interrupt/device/crash race harness; 1,000-cycle write/cancel churn; late N cannot affect N+1; commentary→final never self-interrupts |
| 3 | Introduce response-scoped single-reader `TTSSession`; stream first PCM directly; prefix-safe failure | fake provider proves first accepted PCM before final; concurrent-send/watchdog/resampler tests |
| 4 | Rework `TTSPipeline` around one async media owner; begin emitting registered checkpoint/terminal milestones | no `asyncio.run` per segment; audible-horizon drain; existing watcher compatibility |
| 5 | Add `AudioDuplexBackend` + one logical `AudioIngress`; convert wake/VAD/capture to SPSC subscribers; preserve PTT adapter; add input fault/recovery ownership | one capture owner; no realtime production input `stream.read()` path; buffer-lifetime/discontinuity/pre-roll replay; wake/PTT/fault integration |
| 6 | After ADR-0008 interrupt contracts land, add two-phase speaker-safe keyword/PTT barge-in, deterministic control-utterance routing, and priority control | onset→duck→CAS interrupt chain; duplicate early/final stop is idempotent; standalone stop creates no ordinary response; false candidate resumes; action continues |
| 7 | Complete ResponseLedger heard-state reconstruction from response segments + playback checkpoints | hard crash recovers last checkpoint; unplayed generated text excluded |
| 8 | Add latest-only rolling partial ASR and bounded semantic endpoint | CPU/queue budget; final priority; pause/resume/incomplete/max-hold replay corpus |
| 9 | Enable natural headphone barge-in | headphone live burn and false-positive corpus pass |
| 10 | Spike/select VoiceProcessingIO or hardware backend; enable natural speaker mode only after acceptance | synchronized far-end/mic replay + supervised double-talk burn; otherwise keep PTT/two-stage keyword |
| 11 | Default rollout decision and old canary supersession note | all gates, daemon burn, privacy/permission/sleep/wake/device-restart burn; Allen approval |

Steps 1–5 can land before ADR-0008. Step 6 requires ADR-0008's independent ResponseRun terminalizer, `ResponseInterruptPolicy`, and `ResponseCancelRequest`. Step 7 also requires ADR-0008's response IDs and permitted segment schema. Natural barge-in must not be enabled merely because the media components compile.

## 10. Verification and SLOs

### 10.1 Required trace points

```text
audio_frame_arrived
audio_route_changed
vad_speech_started
duck_requested
duck_gain_reached
endpoint_candidate
utterance_committed
asr_final
barge_in_candidate
barge_in_confirmed
barge_in_candidate_dropped
playback_cas_interrupt
response_cancel_requested
last_nonzero_buffer_submitted
estimated_dac_silence
tts_session_opened
tts_text_pushed
tts_first_pcm
first_pcm_accepted
first_nonzero_buffer_submitted
estimated_first_audible
playback_completed
```

Each trace carries session/turn/response IDs and, only for playback work, the
explicit `playback_generation_id` where applicable. Trace rows may be sampled
or written to a dedicated bounded telemetry sink; high-frequency points are
not canonical events.

### 10.2 Initial acceptance targets

These are product gates measured after warmup, not claims about the current implementation:

| Metric | Target |
|---|---:|
| input frame → candidate duck, p95 | ≤ 200 ms |
| confirmed barge-in → estimated DAC silence, p95 | ≤ 250 ms |
| confirmed barge-in → physical loopback silence, Tier-3 p95 | ≤ 350 ms |
| acoustic endpoint candidate → committed utterance, p95 | ≤ 900 ms |
| TTS text push → first PCM, p50 / p95 | ≤ 500 ms / ≤ 1,200 ms |
| first accepted PCM → first non-zero buffer submitted, p95 | ≤ 100 ms |
| stale generation PCM accepted | 0 |
| response interruption that cancels an action | 0 |
| unplayed generated text added to heard history | 0 |
| provider failure that repeats an audible prefix | 0 |

The combined ADR-0006/0008 target for routine warm turns is end-of-utterance to first meaningful audible speech p50 ≤ 2.0 s and p95 ≤ 3.5 s. Complex actions instead target a truthful lifecycle-driven audible acknowledgement within 1.5 s of dispatch; the real task may continue.

### 10.3 Test tiers

**Tier 1 — deterministic, no hardware/LLM:**

- all four FSM legal and illegal transitions;
- VAD reset, pre/post-roll, short pauses, and max hold;
- single-ingress fan-out and overflow;
- TTS first chunk, persistent session, backpressure, abort, and prefix-safe fallback;
- 1,000 concurrent write/flush operations;
- generation rejection after cancel/device restart;
- delayed cancel for generation N while N+1 plays is a no-op;
- concurrent response activation produces one foreground GenerationLease;
- registration-before-first-commit, response-complete/playback-tail, interrupt/queued-chunk, and terminal/unregister races;
- injected crash at terminal append boundary yields one complete terminal event or none-to-retry, never a claim without an event;
- text span → sample span → conservative heard text;
- fake response cancel leaves fake ActionRun running;
- Event Log registry/projection restart tests;
- existing layer and system-turn voice canaries.

**Tier 2 — recorded replay:**

- at least 30–100 utterances per profile for endpoint p50/p95;
- Chinese/English mixed punctuation, hesitation, self-correction, and continuation;
- synchronized far-end/mic TTS echo recordings for near-end recall, false candidate/min, false cancel/hour, and double-talk;
- device buffer starvation and reconnect fixtures.

**Tier 3 — supervised live burns:**

- headphones: natural interruption, follow-up question retains pre-roll;
- built-in Mac speakers without AEC: PTT or two-stage wake+keyword hard cancel;
- built-in speakers with selected AEC: natural interruption only after pass;
- external speaker and selected microphone separately;
- MiniMax disconnect before and after first PCM;
- `say` fallback interruption;
- output device switch, daemon restart, Mac sleep/wake, and shutdown during playback.

## 11. Spec changes and explicit deviations

1. Spec §3.6.1's `audio frames → VAD/ASR local buffer → utterance.received` remains intact; partial transcript is an ephemeral adapter contract.
2. Spec §3.6.5–§3.6.6 remains intact: Voice executes permitted presentation; it does not derive risk.
3. Spec §5.2/§5.4 gains registered `surface.playback_checkpoint/completed/interrupted/failed` events and their causal links; the Event Log still receives only bounded semantic milestones, not audio ticks.
4. Spec §6 gains the ResponseLedger heard-state fold, keyed by
   `(response_id, playback_generation_id)` and conservative checkpoint.
5. ADR-0005's no-barge-in and wake-pause acceptance are explicitly superseded.
6. ADR-0005's `spoken` UI phase may remain for compatibility but can only be emitted after the conservative audible horizon; “PCM queued” gets no completed meaning.
7. The old `VOICE_INPUT_LOCK` canary is replaced because a long-held whole-turn mutex contradicts the logical single-capture-owner design. Commit serialization and backend ownership provide the new invariant.

## 12. Consequences

### Positive

- Local layer boundaries remain intact.
- TTS and playback latency improve before risky LLM changes land.
- Interruption becomes correct across playback, network cancellation, and future context.
- PTT and legacy full-text behavior remain safe fallbacks.
- Headphone mode can become genuinely full duplex without pretending speaker echo is solved.

### Costs

- Runtime gains a real session coordinator and more explicit state.
- Voice tests need deterministic clocks and generations.
- Accurate word-level heard history remains unavailable until a provider offers timing; v1 intentionally under-counts partial sentences.
- Speaker natural barge-in is a separate AEC qualification effort.

## 13. Definition of done

ADR-0006 is complete only when:

1. The single-ingress, generation-safe output, streaming TTS, and playback ledger paths are implemented behind feature flags.
2. Keyword barge-in stops audible output within the SLO without cancelling linked actions.
3. Interrupted responses reconstruct conservative heard text after daemon restart; a hard crash recovers only through the last durable playback checkpoint and never guesses forward.
4. PTT and legacy mode remain green.
5. Natural headphone mode passes replay and supervised live burns.
6. Natural speaker mode is either independently accepted with AEC evidence or remains disabled.
7. Tier 1, replay, and required live burns are recorded in project progress docs.
8. Allen changes this ADR's status to Accepted/Approved.
