# Goal: speaking-window-vad-profile

## Goal
While Jarvis is producing audio, the Wave-3 capture VAD runs on the configured stricter threshold profile instead of the record profile, and a live run measures what that changes.

## Why
`_MODE_THRESHOLDS` declares a `"tts"` profile (jarvis/surface/voice_audio.py:78-81) whose comment says it exists "so playback bleed doesn't false-trigger a barge-in" (:76-77), and the `SileroVad` docstring repeats the claim (:113-115). Nothing in `jarvis/` implements it: every construction site passes `mode="record"` (jarvis/runtime/inherent_loop.py:2005, :2666), `SileroVad.thresholds()` (:163-168) has no caller in `jarvis/` or `tests/`, and no test ever constructs `mode="tts"`. The profile is declared, unreachable, and entirely untested, and the comments assert an intent the code does not implement. That is the whole justification: make the declared behavior real, or the comment is a lie.

No ADR requires this. ADR-0006 D9 does not name `_MODE_THRESHOLDS`, does not name the record/tts profiles, and defines no selection mechanism; it governs `DeviceProfileKey` / `allowed_barge_mode` by route kind and AEC validation, and it states outright that "Legacy's stricter VAD threshold is not AEC." Do not cite D9 as a mandate for this card. Likewise, the AEC spike's "7.53 false candidates/min" are `SileroVad` IDLE→ACTIVE transitions inside the endpointing gate, while D9's 0.5/min budget governs D8's barge-in candidate window (a wake hit while output is active) — no capture frame, energy verdict, or Silero probability reaches `voice_interrupt.py` at all. The two numbers measure different things and must not be compared.

The mechanism is nearly free, and `vad_mode` is already emitted into the traces (jarvis/surface/voice_audio.py:297, :310), so once the switch exists the open question — does the strict profile actually reduce playback-bleed false starts, and at what cost to endpointing — becomes a measurement instead of an argument. That measurement is the deliverable.

## Current behavior
- `VadThresholds` (jarvis/surface/voice_audio.py:65-73) carries `prob_threshold`, `db_threshold`, and shared `smoothing_window=5`, `required_hits=3`, `required_misses=24`. `_MODE_THRESHOLDS` (:78-81) holds `"record"` (prob 0.4 / dB -45.0) and `"tts"` (prob 0.5 / dB -22.0). The two profiles differ in exactly the two thresholds; every debounce and smoothing parameter is identical.
- `SileroVad.__init__` (:128-158) validates `mode`, stores `self._mode` (:138) and binds `self._t = _MODE_THRESHOLDS[mode]` once (:140). `_advance_state` (:273-317) re-reads `self._t` every frame at the gate `smooth_prob >= self._t.prob_threshold and smooth_db >= self._t.db_threshold` (:283-286), so rebinding `self._t` between frames is sufficient to change behavior.
- `self._mode` is what the traces report: `vad_speech_started` carries `vad_mode=self._mode` (:297) and `vad_endpoint_candidate` the same (:310). A rebind of `self._t` alone would leave `vad_mode` stale.
- `SileroVad.thresholds(cls, mode)` (:163-168) is a pure lookup with no caller in `jarvis/` or `tests/`; its only users anywhere are `scripts/spike_voiceprocessingio_aec.py`.
- Both construction sites pass `mode="record"`: the legacy wake path in `_spawn_wake_listener` (jarvis/runtime/inherent_loop.py:2005, whose own 16 kHz / 80 ms `RawInputStream` opens at :1949-1954), and the Wave-3 single-audio-ingress path (:2666), which hands the VAD to `voice_session.DuplexVoiceSession` (:2667-2678).
- Frame path in Wave 3: `DuplexVoiceSession._capture_loop` (jarvis/surface/voice_session.py:1180-1203) → `UtteranceAssembler.feed` (:576-651) → `self._vad.feed(frame.pcm16_mono)` (:627) → `SileroVad.feed` (jarvis/surface/voice_audio.py:203-228) → `_advance_state`. The VAD runs on the `jarvis-utterance-ingress` consumer thread, not inside the PortAudio callback.
- The speaking signal already exists and is already read synchronously: `output_active` is a constructor parameter of `DuplexVoiceSession` (jarvis/surface/voice_session.py:869), stored as `self._output_active` (:879), wired from `tts.is_output_active` (jarvis/runtime/inherent_loop.py:2673), already called by `_wake_loop` (jarvis/surface/voice_session.py:1066-1071) and already handed to `BargeInRouter` (:894). Its implementations are cheap: a `threading.Event.is_set()` in `voice_media.py` and a short locked check in `voice_tts.py`. It is simply never passed to `_capture_loop` or to `UtteranceAssembler`, which is constructed without it (:912-920).
- `UtteranceAssembler` reads `vad.endpoint_silence_frames` once at construction (:436-438). Since both profiles share `required_misses=24`, that snapshot stays correct across a profile switch.
- Session config lives under `realtime.single_audio_ingress` (config/jarvis.yaml:240-291), parsed by `realtime_input_session_config_from_mapping` (jarvis/surface/voice_session.py:1437-1505) into `RealtimeInputSessionConfig` (:61-79). A `ValueError` from that parse is already converted into an `invalid_input_config:<msg>` downgrade before any device opens (jarvis/runtime/inherent_loop.py:2524-2532).

## Target behavior
- A new config key `realtime.single_audio_ingress.output_active_vad_mode`, a string naming a `_MODE_THRESHOLDS` profile, defaulting to `"record"` — the shipped default; see Progress slice 4's ruling below for why — carried on `RealtimeInputSessionConfig`. An unknown value raises `ValueError` during config parse, which the existing pre-device path already downgrades cleanly. Setting it to `"record"` restores today's behavior exactly, with no code change — this key is the calibration knob and the experiment control, not a feature flag.
- While `output_active()` is true, frames fed through the Wave-3 capture path are classified against the configured profile's thresholds; while it is false, against `"record"`. The existing `output_active` callable is the only signal.
- Switching a profile rebinds which `VadThresholds` the detector points at and updates the mode the detector reports. It must not reset or rebuild anything else: the ONNX session, the LSTM state, the smoothing deques, the hit/miss counters and the IDLE/ACTIVE state all survive a switch untouched, and the detector object is never reconstructed. A switch to the mode already in effect is a no-op.
- `vad_mode` in `vad_speech_started` and `vad_endpoint_candidate` reports the profile actually in effect for that frame. Burn documents read that field; a stale value would silently corrupt every future measurement.
- If the `output_active` callable raises, the frame keeps whatever mode is currently in effect (it is not forced strict). Failing to the stricter profile on an unknown output state could truncate a user utterance already in progress; this is deliberately not the wake loop's fail-to-suppressed behavior, because the two paths fail into opposite harms.
- With `output_active_vad_mode: "record"`, or with `single_audio_ingress` disabled, or on the legacy wake path, behavior is byte-identical to today.

## Affected contracts and files
- L5 `jarvis/surface/voice_audio.py:SileroVad` — a way to point the detector at another `_MODE_THRESHOLDS` profile between frames, updating `self._t` and the reported mode and nothing else. First production caller for the `"tts"` profile.
- L5 `jarvis/surface/voice_session.py:UtteranceAssembler`, `DuplexVoiceSession`, `RealtimeInputSessionConfig`, `realtime_input_session_config_from_mapping` — thread the existing `output_active` callable into the capture path, consult it per frame, parse and validate the new key.
- L6 `jarvis/runtime/inherent_loop.py:_spawn_single_ingress_session` — only if the new value needs threading past the already-parsed `session_config`; the `output_active` wiring at :2673 already exists and does not change.
- `config/jarvis.yaml` — the `output_active_vad_mode` key inside `single_audio_ingress`, with a comment stating what it selects and that `"record"` reverts.
- `tests/integration/test_voice_vad_endpoint.py` — the two new tests (see Acceptance evidence). Reuse this file; do not add a new one.
- `docs/live-burn-<date>-speaking-window-vad-profile.md` — the measured numbers from the live run.

## Boundaries and non-goals
- Layers that may change: L5 (`jarvis/surface`), L6 wiring in `runtime/` only if a config value must be threaded, config, tests, docs. No change under `jarvis/decision/`, `jarvis/execution/`, or `jarvis/state/`.
- Must not change: the shape of the endpointing state machine, `required_hits` / `required_misses` / `smoothing_window` for either profile, the per-utterance `prepare_utterance` reset contract, the single capture owner and SPSC subscriber structure, the PTT `/inherent/asr-submit` path, `_least_quality`, the ledger, or any wire payload.
- The legacy `_spawn_wake_listener` path (jarvis/runtime/inherent_loop.py:2005) is an explicit NON-GOAL. It owns its own PortAudio stream and its own lifecycle, and it stays on `mode="record"` unchanged.
- Non-goals: AEC of any kind; barge-in behavior, `allowed_barge_mode`, or the device-profile-resolver (`voice_backend.py`); `measured_dac` (no code path produces it); enabling natural barge-in; retuning the threshold values themselves; any second speaking signal or clock.

## Rejected approaches
- Rebuilding the `SileroVad` object (or calling `reset()` / `prepare_utterance()`) to switch profiles — it would drop the LSTM state, the smoothing deques, and the hit/miss counters mid-utterance, which is a far larger behavior change than the two thresholds it is trying to apply. The two profiles share every debounce parameter, so nothing needs to move but `self._t` and the reported mode.
- Inventing a new "speaking" signal — an output-state flag on the assembler, a playback-generation lookup, an event subscription, or a timer. `output_active` already exists, is already synchronous and cheap, and is already consulted on the wake path and by `BargeInRouter`. A second signal would immediately be able to disagree with the first.
- Reading the Event Log or the realtime trace sink per frame to learn whether output is active — the VAD runs on a real-time consumer thread at 32 ms per frame; a log read there is both a latency hazard and a layer inversion.
- A boolean feature flag (`strict_vad_during_output: true|false`) instead of the profile key — a boolean cannot express "revert to record" without a code path for it, and real rooms and real speakers differ, so the reversal and the calibration must be the same knob. Per R5 there is exactly one key and no separate flag.
- Changing the legacy wake path to match — it has a separate stream, separate lifecycle, and no `output_active` in scope; touching it doubles the blast radius for no measurement.
- Rebinding `self._t` without updating the reported mode — cheapest possible diff, and it would make `vad_mode` lie in exactly the traces the burn documents read.

## Acceptance evidence
- Positive (hermetic), exactly two tests in `tests/integration/test_voice_vad_endpoint.py`, raw pytest output shown:
  1. Strict applies while speaking: with `output_active` returning true, frames whose smoothed probability and dB fall between the two profiles are classified as silence (they would be speech under `"record"`), and the emitted `vad_speech_started` / `vad_endpoint_candidate` traces carry `vad_mode="tts"`.
  2. Reverts after: when `output_active` flips back to false mid-stream the same frames classify as speech again and the traces carry `vad_mode="record"`, and the detector's accumulated hit/miss counters and IDLE/ACTIVE state are the ones from before the switch — the switch changed thresholds only.
  Do not add a test per threshold field, per profile key, or per VAD state, and do not add a test that merely restates the `_MODE_THRESHOLDS` table.
- Regression: `PYTHONPATH=. .venv/bin/python -m pytest -q -m "not live_llm and not live_codex"` equals the branch baseline measured at launch plus this card's two new tests, with any other delta explained. The session must run and quote that baseline count itself before making any change — the branch is moving under this card, so a number copied from another card's Progress line is not the baseline. Also show `PYTHONPATH=. .venv/bin/python -m pytest -q tests/integration/test_voice_vad_endpoint.py tests/integration/test_wave3_single_audio_ingress.py tests/integration/test_endpointing_partial_asr.py` passing.
- Gates, raw output, each exiting 0: `PYTHONPATH=. .venv/bin/lint-imports`, `PYTHONPATH=. .venv/bin/ruff check .`, `PYTHONPATH=. .venv/bin/mypy --strict jarvis tests scripts tools`. `PYTHONPATH=.` is mandatory on all three: without it the editable install resolves to the main checkout and a provenance test fails spuriously.
- Swift: no `desktop/` change is expected. Run `bash scripts/test_inherent_swift.sh` only if `desktop/` is in fact touched, and otherwise state in the transcript that `git status` / the diff shows no `desktop/` path.
- Live run (REQUIRED — this is the point of the card, not a formality). Two arms of the same scripted turn against the real daemon on lane B's own runtime root and its own port, with `realtime.enabled: true`, `single_audio_ingress.enabled: true` and `streaming_output.enabled: true` (without the streaming pipeline the session downgrades to the legacy wake path and measures nothing), and `JARVIS_REALTIME_TRACE_JSONL` pointed at a separate file per arm. Arm A sets `output_active_vad_mode: "record"` (control, today's behavior); arm B sets `"tts"`. Each arm: wake, ask a question whose spoken answer runs for a comparable duration, stay silent through the whole answer, then speak again immediately as the answer ends. Numbers that must be quoted:
  - the count of `vad_speech_started` trace rows emitted while output was active, per arm — that is the IDLE→ACTIVE transition count, and the two counts together are the card's result;
  - the spoken-answer duration for each arm, so the two counts are comparable;
  - at least one `vad_speech_started` or `vad_endpoint_candidate` row quoted raw from each arm's JSONL, showing `vad_mode` as `"tts"` during output in arm B and as `"record"` in arm A;
  - a raw row pair from arm B showing `vad_mode` back to `"record"` after output ended;
  - for the utterance spoken at the end of each arm: the `audio_input_capture_started` row and the resulting `utterance.received` transcript, so onset capture and transcript completeness are visible in both arms.
- Audio route: the live run uses the DEFAULT output and input route and switches no device. There is no `SwitchAudioSource` call, no loopback device, and therefore no restore trap — the standing audio rule is satisfied by never switching, not by restoring. State that explicitly in the transcript. The measurement depends on real speaker-to-microphone bleed, so a loopback or file-replay capture would not measure the thing the card exists to measure.
- STOP CONDITION (do not ship, do not tune): if arm B shows the strict profile makes endpointing worse — the end-of-arm utterance's speech onset is missed or late where arm A caught it, or its `utterance.received` transcript is truncated relative to arm A, or arm B loses an utterance arm A captured — then stop, report both arms' numbers, leave the code uncommitted or committed with the default flipped to `"record"` as the session judges safest, and say plainly that the strict profile is not adopted. Do not adjust threshold values, `required_hits`, `required_misses`, or the answer script to make arm B look better. The config key exists so the result is reversible, not so it can be fudged.
- The daemon on port 8006 is never signalled or stopped; every artifact of this card lives under lane B's own runtime root.

## Docs to sync
- `docs/adr/0006-full-duplex-voice-session.md` §10.1 (the required-trace list, which names `vad_speech_started`) — add one line only if the shipped behavior changes what `vad_mode` means for a reader of that trace, namely that it now varies within a session; otherwise state explicitly that it was judged unchanged. D9 is NOT amended by this card and must not be cited as its authority.
- `docs/adr/0006-full-duplex-voice-session.md` D8/D9 and `docs/spec.html` — judged unchanged unless the implementation actually alters a contract they own; say so explicitly rather than silently. `docs/spec.html` contains no VAD threshold or Silero fact today.
- `docs/live-burn-<date>-speaking-window-vad-profile.md` — new; the two arms, their IDLE→ACTIVE counts, answer durations, the raw `vad_mode` rows, the end-of-arm utterance outcome, and the adopt / do-not-adopt verdict.
- Rule for all of the above: update the canonical document that owns a changed contract, do not document what the code already makes clear, do not duplicate a fact across documents.

## Open questions
(none)

## /goal condition
Implement docs/goals/speaking-window-vad-profile.md on the current branch. The goal is met when all of the following appear in the transcript: (1) the diff adds the config key `realtime.single_audio_ingress.output_active_vad_mode` defaulting to `"tts"`, makes the Wave-3 capture path consult the existing `output_active` callable per frame and point the VAD at that profile while output is active and at `"record"` otherwise, updates the mode reported in `vad_speech_started` / `vad_endpoint_candidate` so `vad_mode` is never stale, and does NOT reconstruct or reset the SileroVad object, its LSTM state, deques or hit/miss counters on a switch, does NOT add a second speaking signal, a boolean feature flag, an event-log or trace read on the frame path, and does NOT touch the legacy `_spawn_wake_listener` wake path, `jarvis/decision/`, `jarvis/execution/`, or any wire payload; (2) raw pytest output of exactly two new tests in tests/integration/test_voice_vad_endpoint.py — one showing the strict thresholds apply and `vad_mode` reads `"tts"` while output is active, one showing both revert when it goes false with the detector's counters and state carried across the switch — ending in a pass line, and no per-field, per-key or per-state test added; (3) the launch baseline: raw output of `PYTHONPATH=. .venv/bin/python -m pytest -q -m "not live_llm and not live_codex"` run BEFORE any change with its count stated, then the same command after, equal to that baseline plus the two new tests, with any other delta explained; plus raw passing output of tests/integration/test_voice_vad_endpoint.py, test_wave3_single_audio_ingress.py and test_endpointing_partial_asr.py; (4) raw output of `PYTHONPATH=. .venv/bin/lint-imports`, `PYTHONPATH=. .venv/bin/ruff check .` and `PYTHONPATH=. .venv/bin/mypy --strict jarvis tests scripts tools`, each ending in a pass line or exit 0, each shown with the `PYTHONPATH=.` prefix; (5) either raw output of `bash scripts/test_inherent_swift.sh` if `desktop/` was touched, or an explicit statement that the diff contains no `desktop/` path; (6) the live run, two arms of the same scripted turn on lane B's own runtime root with `single_audio_ingress` and `streaming_output` enabled and a per-arm `JARVIS_REALTIME_TRACE_JSONL`, arm A with `output_active_vad_mode: "record"` and arm B with `"tts"`, quoting: the number of `vad_speech_started` rows emitted while output was active in each arm, each arm's spoken-answer duration, at least one raw JSONL row per arm showing `vad_mode` as `"record"` (arm A) and `"tts"` (arm B) during output, a raw arm-B row showing `vad_mode` back to `"record"` after output ended, and for the utterance spoken at the end of each arm its `audio_input_capture_started` row and its `utterance.received` transcript; (7) an explicit statement that the live run used the DEFAULT audio output and input route, switched no device, ran no `SwitchAudioSource` and so needed no restore trap, and that the port-8006 daemon was never signalled or stopped; (8) the stop condition honored: if arm B missed or delayed a speech onset arm A caught, truncated a transcript, or lost an utterance, the run STOPS with both arms' numbers reported and the strict profile explicitly not adopted, with no threshold, counter, or answer-script tuning attempted to rescue it; (9) `docs/live-burn-<date>-speaking-window-vad-profile.md` written with both arms' numbers and the adopt / do-not-adopt verdict, and ADR-0006 §10.1, ADR-0006 D8/D9, and docs/spec.html each updated or explicitly judged unchanged, following the rule: update the canonical document that owns a changed contract, do not document what the code already makes clear, do not duplicate a fact across documents — and with ADR-0006 D9 never cited as this card's authority; (10) each slice committed with the project commit skill, `git status` clean, and a Progress line per slice appended to the card. Or stop after 45 turns.

## Progress
- (empty until the implementation session starts)
- 2026-09-05 slice 1 (858fed6, `feat(surface): run the strict VAD profile while
  output is active`): `SileroVad.set_mode` rebinds `self._t` and `self._mode`
  together and nothing else; the Wave-3 assembler consults the existing
  `output_active` per classified frame; `output_active_vad_mode` parses through
  the existing `thresholds()` lookup and ships in `config/jarvis.yaml`
  defaulting to `tts`. No `runtime/` change was needed — `output_active` was
  already wired at `inherent_loop.py:2673` and the value rides the session
  config. Launch baseline measured by this session at 81a772d: 1047 passed / 64
  deselected; after: 1049 passed / 64 deselected (+2, exactly the card's two
  tests, no other delta). Gates: lint-imports KEPT (1 kept, 0 broken), ruff all
  checks passed, mypy strict clean (242 files). No `desktop/` path in the diff,
  so `scripts/test_inherent_swift.sh` was not run.
- 2026-09-05 slice 2 (live burn, `docs/live-burn-2026-09-05-speaking-window-vad-profile.md`):
  both arms run on lane B's own root/port with per-arm trace files. `vad_speech_started`
  rows while output was active: 0 in arm A and 0 in arm B; arm B produced no
  `vad_mode: "tts"` row at all. Answer durations 15.378 s (A, `playback_completed`)
  and 45.002 s (B, `playback_failed` / `partial_tts_provider_failure`). Cause is
  structural, not a wiring defect: the assembler feeds the VAD only between
  wake-arm and commit, and a wake during output is suppressed by D8, so no frame
  is classified while output is active. Stop condition did NOT fire — arm B lost
  no onset, truncated no transcript, captured one utterance more than arm A — so
  the code ships unchanged, but the profile is not adopted as a measured
  improvement. Owner follow-up: a mic-in-the-loop run with a human speaking over
  the answer's tail, and a re-run once natural barge-in can arm capture during
  output, are the two ways to make this measurement non-vacuous.
- 2026-09-05 slice 3 (verifier pass, `realtime-integration..HEAD` re-scoped to
  `81a772d..HEAD` because lane/a landed on the integration branch after this
  branch merged it): no blocking defect; the verifier's mutation run confirms
  test 2 dies if `set_mode` also calls `reset()` and both tests die if the mode
  goes stale or the switch never happens. Three findings fixed — the per-frame
  `LOGGER.warning` on the `output_active` failure path was removed (it could
  format a traceback ~31 times a second on the capture thread, and the wake loop
  already reports the same failure); `set_mode`'s docstring now names the shared
  debounce fields that make a mid-utterance switch safe; the test fixture now
  varies the probability and grows the LSTM so a cleared window or LSTM is
  observable rather than indistinguishable from a fresh one. ADR-0006 §10.1 was
  trimmed to the one fact it owns and no longer names `vad_endpoint_candidate`,
  which is not in that list. Not fixed, by design: the exception-path semantics
  have no test (the card caps this file at exactly two) and the `"tts"` default
  ships unexercised by any live run — both are owner decisions recorded in the
  burn document, not defects.
- 2026-09-05 slice 4 (hub ruling, deviates from this card's Target behavior):
  `output_active_vad_mode` now defaults to `"record"`, not `"tts"`. The card
  specified `"tts"` on the assumption the burn would measure it; the burn
  returned a structural null, so there is zero evidence either way. Shipping
  `"tts"` would have made the -22 dB gate start applying silently the first time
  natural barge-in could arm capture during output — an unmeasured behaviour
  change that would surface inside a future barge-in card and be attributed to
  it rather than to this one. The default is `"record"` BECAUSE the A/B was
  null, not because `"tts"` was rejected on evidence; those are different claims
  and only the first is true. The mechanism and the knob are unchanged, so
  adoption stays an explicit, measured decision for whoever lands natural
  barge-in. The two tests now pin `output_active_vad_mode="tts"` on their own
  config rather than leaning on the shipped default, which is what they should
  always have done — they are about the switch, not about which profile ships.
  Gates after the flip: lint-imports KEPT (1 kept, 0 broken), ruff all checks
  passed, mypy strict clean (242 files), 1049 passed / 64 deselected, and
  `111 passed` across test_voice_vad_endpoint.py, test_wave3_single_audio_ingress.py
  and test_endpointing_partial_asr.py.
- Owner follow-ups carried out of this card, neither a blocker: (a) no human
  voice was ever in the loop — the live stimulus was TTS clips over the real
  acoustic path, because the implementation session cannot speak, so a run with
  a person talking over the answer's tail is still owed; (b) the exception-path
  semantics (a raising `output_active` keeps the current profile rather than
  failing strict) have no test, because this card capped the file at exactly two
  tests and forbade per-state tests — a card-level spec-vs-evidence gap for a
  later card to close deliberately.
