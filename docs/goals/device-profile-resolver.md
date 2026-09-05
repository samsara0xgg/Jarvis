# Goal: device-profile-resolver

## Goal
The realtime input path knows which audio route it is on (input and output device identity and class), re-resolves that profile on any route change through the existing fault and epoch path, and exposes an `allowed_barge_mode` that is never natural unless the profile is explicitly accepted, so every later barge-in decision fails closed by construction.

## Why
ADR-0006 D9 makes barge-in mode a function of the device profile and F18 requires a route change to revoke natural mode before reopening. Today nothing knows the output route and nothing can revoke anything, which blocks keyword barge-in and natural mode alike.

## Current behavior
- Input device is the system default resolved through sounddevice with no name matching or preference (jarvis/surface/voice_backend.py:_default_input_device_profile, :265-283); output streams open with no device identity or classification (jarvis/surface/voice_tts.py:517-540).
- Route change detection is poll-only and input-only: `AudioIngress._run_worker` compares `backend.current_device_uid()` every `route_poll_s` (config/jarvis.yaml:238, default 1.0) and raises `BackendFault("default_device_changed")` on mismatch (jarvis/surface/voice_audio.py:1585-1610). `notify_route_change()` exists with zero callers (jarvis/surface/voice_audio.py:1979-2030). No CoreAudio listener exists.
- `BackendCapabilities` are hard-coded `aec=False, natural_barge_in=False` (jarvis/surface/voice_backend.py:474-479); `DuplexVoiceSession.start` traces `natural_barge_in_enabled=False` (jarvis/surface/voice_session.py:611).
- `InputCapabilityState` has seven values and one fault entry point `_handle_fault` reached from fault poll, route poll, and `notify_route_change` alike (jarvis/surface/voice_audio.py:404-413 and the setters listed around :1116-2161).
- No `DeviceProfileKey`, `DeviceProfileSnapshot`, `AudioRouteObserver`, `route_kind`, or `allowed_barge_mode` exists anywhere; the config `single_audio_ingress` block has no `barge_in` or profile keys (config/jarvis.yaml:195-248).
- The Inherent broadcaster already has a `voice_capability` op (jarvis/surface/inherent_output.py:18-23) carrying the input capability snapshot; the existing trace for capability changes is `audio_input_capability_changed` (jarvis/surface/voice_audio.py:2020-2030). ADR §10.1 names no route trace.
- `_spawn_voice_input_owners` selects the single input owner from static config only (jarvis/runtime/inherent_loop.py:2174-2206).

## Target behavior
- ADR-0006 D9 (branch text §3 D9) is the contract for the software half: `DeviceProfileKey` (input uid, output uid, route kind) and `DeviceProfileSnapshot` (key plus backend capabilities and the resolved `allowed_barge_mode`), resolved at stream open and on every route change; F17/F18 in §6 govern downgrade: a route change closes the failed epoch through `_handle_fault`, revokes any barge mode above the new profile's allowance before reopen, and publishes the new snapshot.
- Route kinds are conservative: `headphones` only when the default output device is positively classified as a headphone or headset route (CoreAudio transport type Bluetooth, USB headset, or headphone jack, read through the same ctypes approach the repo uses for IOKit; a name-only match is not sufficient), `speakers` for built-in or line-out output, `unknown` otherwise. `unknown` resolves like `speakers`.
- `allowed_barge_mode` is `ptt_only` or `two_stage_keyword` from config, and `natural` only when the profile key is listed under an explicit accepted-profiles config list; that list ships empty, so natural is unreachable in this card. `BackendCapabilities.aec` stays False everywhere.
- The route observer polls both default input and default output identities on the existing `route_poll_s` cadence (a CoreAudio default-device listener may replace polling only if it feeds the same `notify_route_change` path); an output route change with unchanged input still produces a new stream epoch and a re-resolved snapshot, per F18.
- The snapshot and mode are visible: a new `audio_route_changed` trace point (previous and next profile key, reason) and the existing `voice_capability` broadcast gains `route_kind` and `allowed_barge_mode` fields; the Swift v1 client ignores unknown fields.
- Config keys under `realtime.single_audio_ingress`: `route_observer.enabled` (default false: no output polling, behavior identical to today), `barge_in.detection_mode` (`ptt` default, `two_stage_keyword`), `barge_in.accepted_natural_profiles` (list, default empty). Unknown or malformed values fail closed to `ptt`.
- PTT upload continues to work in every profile and during a route change.

## Affected contracts and files
- L5 jarvis/surface/voice_backend.py — profile key/snapshot types, output device identity and transport classification, capability plumbing.
- L5 jarvis/surface/voice_audio.py — route observer loop on both devices, `notify_route_change` becomes the single entry, trace.
- L5 jarvis/surface/voice_session.py — config parsing for the new keys, snapshot exposure on the session.
- L5 jarvis/surface/inherent_output.py — two additive fields on `voice_capability`.
- runtime jarvis/runtime/inherent_loop.py — start the observer with the session when enabled.
- config/jarvis.yaml — the three keys, defaults off/ptt/empty.
- tests/integration — fake-backend route change harness; data-driven classification table.

## Boundaries and non-goals
- Layers that may change: L5 (`jarvis/surface`), runtime wiring, config, tests.
- Must not change: the single capture owner and owner registry; PTT `/inherent/asr-submit`; VAD, endpointing, or wake code; any claim of AEC; `natural_barge_in` remaining False in capabilities; v1 wire ops beyond additive fields; L3.
- Non-goals: the hardware or VoiceProcessingIO AEC spike; keyword barge-in detection itself; enabling natural mode; the D9 acceptance thresholds (they gate a later "accept profile" decision, not this card).

## Rejected approaches
- Name-based headphone detection alone (for example matching "AirPods") — a renamed or unknown device would be granted headphone privileges; transport type is the evidence, names are at most a hint.
- A second observer path outside `_handle_fault` — D3's Input FSM has one fault entry and the errata records that; a parallel path would race the epoch.
- Defaulting `route_observer.enabled` to true — output polling is new behavior and the card must be byte-identical to today when off.

## Acceptance evidence
- Positive (hermetic): an integration harness with a fake `AudioDuplexBackend` shows raw pytest output for: profile resolved at open with the expected key; an output-only uid change produces `audio_route_changed`, a new stream epoch through `_handle_fault`, and a re-resolved snapshot while the input uid is unchanged; an input change does the same; `allowed_barge_mode` is `ptt_only` for `speakers` and `unknown` regardless of `detection_mode` other than `two_stage_keyword`, and never `natural` even when `detection_mode` is set to an unsupported value; a profile listed under `accepted_natural_profiles` resolves `natural` only for `headphones`; a data-driven table of (input transport, output transport, name) to `route_kind` passes; the `voice_capability` payload carries the two new fields.
- Regression: `PYTHONPATH=. .venv/bin/python -m pytest -q tests/integration/test_wave3_single_audio_ingress.py tests/integration/test_inherent_server_asr_submit.py tests/integration/test_voice_vad_endpoint.py` passes unchanged; with `route_observer.enabled: false` the existing route-change-during-open test behaves as before; full hermetic suite, `lint-imports`, `ruff check .`, `mypy --strict jarvis tests scripts tools` exit 0. Raw output shown.
- Live (required when a second output device exists): start a daemon from this worktree on its own runtime root and port with `route_observer.enabled: true`, switch the macOS default output device (for example with `SwitchAudioSource -t output -s "<name>"` if installed, otherwise via the sounddevice-visible device list and `osascript`), and show the `audio_route_changed` trace plus the new stream epoch in the daemon log. If only one output device exists on the machine, state that, print the exact command Allen should run, and record it as an Allen follow-up.

## Docs to sync
- docs/adr/0006-full-duplex-voice-session.md D9 — judged unchanged unless the route-kind rule or mode names deviate; §10.1 — add the `audio_route_changed` trace point in place; §6 F17/F18 — judged unchanged.
- docs/spec.html — judged unchanged unless §3 names the capability broadcast fields; say so.

## Open questions
(none)

## /goal condition
Implement docs/goals/device-profile-resolver.md on the current branch. The goal is met when all of the following appear in the transcript: (1) the diff adds `DeviceProfileKey`, `DeviceProfileSnapshot`, route-kind classification by transport type, an input-plus-output route observer feeding `notify_route_change`, `allowed_barge_mode` resolution with natural unreachable by default, the `audio_route_changed` trace, the two additive `voice_capability` fields, and the three config keys with off/ptt/empty defaults, with no change to the capture owner, PTT path, VAD/endpointing/wake code, or any AEC claim; (2) raw pytest output of the new harness covering the seven cases in Acceptance evidence, ending in a pass line; (3) raw output of the three named regression files, the full hermetic suite with live tests excluded, `lint-imports`, `ruff check .`, and `mypy --strict jarvis tests scripts tools`, each ending with a pass line or exit 0; (4) either the live route-switch trail showing `audio_route_changed` and a new stream epoch, or an explicit statement that only one output device exists with the exact follow-up command for Allen; (5) ADR-0006 D9, §6, §10.1 and docs/spec.html each updated or explicitly judged unchanged, following the rule: update the canonical document that owns a changed contract, do not document what the code makes clear, do not duplicate a fact across documents; (6) each slice committed with the project commit skill and `git status` clean; (7) a Progress line per slice in the card. Or stop after 60 turns.

## Progress
