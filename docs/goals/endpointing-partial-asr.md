# Goal: endpointing-partial-asr

## Goal
An utterance ends on an acoustic pause first and on a stable partial transcript that looks complete second, bounded by a hard maximum hold, while final SenseVoice ASR stays the only persisted text.

## Why
Today a mid-sentence pause splits an utterance and a long trailing silence delays the answer; ADR-0006 D7 is the contract for fixing both and nothing of it is built.

## Current behavior
- The endpoint rule is purely acoustic: VAD silence after `min_voiced` or the `max_utterance_s` frame cap (jarvis/surface/voice_session.py:UtteranceAssembler.feed, :436-442). No hold verdict, no `EndpointDecision`, no semantic input.
- ASR is single-shot after commit: `VoicePipeline.run_turn` calls `recognize` once (jarvis/surface/voice_pipeline.py:156); `SenseVoiceRecognizer.recognize` builds one stream and decodes once (jarvis/surface/voice_asr.py:396-405). No partial decode, no revision or stable-prefix concept anywhere under jarvis/surface or jarvis/runtime.
- Silero state is reset per utterance and must stay so (jarvis/surface/voice_audio.py:SileroVad.prepare_utterance, :173-195; jarvis/surface/voice_session.py:318-320).
- Shipped config lives under `realtime.single_audio_ingress` (config/jarvis.yaml:213-236, parsed by jarvis/surface/voice_session.py:realtime_input_session_config_from_mapping, :914-971) with `pre_roll_ms`, `min_voiced_s`, `max_utterance_s`, `armed_no_speech_timeout_s`. None of D7's `partial_interval_ms`, `endpoint_candidate_ms`, `endpoint_max_hold_ms`, `post_roll_ms` exist. Frame size is the hard-coded 512-sample Silero chunk (jarvis/surface/voice_audio.py:47).
- Traces: voice_session emits `endpoint_candidate` (:443-452) and `audio_input_endpoint_committed`; voice_audio's legacy path emits `vad_endpoint_candidate` (:280-296); voice_pipeline emits `asr_final` (:150-158).
- PTT audio arrives through `/inherent/asr-submit` (jarvis/runtime/inherent_loop.py:2758) and never touches the assembler.
- No recorded replay corpus exists under data/ or tests/.

## Target behavior
- ADR-0006 D7 is the contract (branch text, §3 D7): rolling partial decode of a bounded audio snapshot on one serialized ASR lane; latest-only coalescing queue with at most one decode in flight; final ASR has strict priority and an endpoint commit cancels queued partials and invalidates late revisions by `utterance_id/revision`; `stable_prefix` advances only after the same normalized code-point prefix survives two consecutive revisions; the six-step decision (acoustic pause opens a hold with a short candidate window; speech resume returns to speech-active without commit; complete-looking stable prefix finalizes early; incomplete-looking prefix holds at most `max_hold_ms`; PTT release goes straight to final ASR; only the normalized `utterance.received` commit produces the committed state).
- Budget degrade is concrete: if one partial decode runs longer than `partial_interval_ms`, or the coalescing queue drops three consecutive snapshots, the rest of that utterance uses acoustic endpointing only and a `partial_asr_degraded` trace records the reason.
- Completeness heuristic is local and deterministic: terminal punctuation or a clause that ends without a dangling connective/particle counts as complete; the rule table lives in code with a data-driven check, not in the prompt and not via an LLM call.
- Config keys live under `realtime.single_audio_ingress.partial_asr` (`enabled`, default false; `interval_ms`; `candidate_ms`; `max_hold_ms`; `post_roll_ms`) with initial values taken from the D7 calibration table. With `enabled: false` behavior is byte-identical to today.
- The assembler gains an explicit endpoint phase enum with the values `speech_active`, `endpoint_pending`, `finalizing_asr`, `committed` (the D3 Input FSM names for these semantics). New traces: `asr_partial` (utterance_id, revision, stable_prefix_len, decode_ms) and `endpoint_decision` (verdict, reason, held_ms). Existing trace names are unchanged.
- A file-replay `AudioDuplexBackend` that feeds a WAV through the real `AudioIngress` at real-time pace exists for tests and live checks; it is the seed of the §10.3 Tier 2 corpus runner.
- Final SenseVoice text remains the only transcript that is persisted or sent to L3; partial text never leaves L5.

## Affected contracts and files
- L5 jarvis/surface/voice_session.py:UtteranceAssembler, RealtimeInputSessionConfig, realtime_input_session_config_from_mapping — phase enum, hold logic, config parsing.
- L5 jarvis/surface/voice_asr.py — a partial-decode entry on the SenseVoice recognizer over a bounded snapshot; no new recognizer.
- L5 jarvis/surface/voice_pipeline.py — final decode keeps priority; partial lane cancellation on commit.
- L5 jarvis/surface/voice_backend.py — file-replay backend implementing the existing `AudioDuplexBackend` Protocol.
- config/jarvis.yaml — the `partial_asr` block, default off.
- tests/integration — scripted-partial harness and replay tests; data-driven completeness table.

## Boundaries and non-goals
- Layers that may change: L5 (`jarvis/surface`), config, tests. Runtime wiring only if a new config key must be threaded through `jarvis/runtime/inherent_loop.py:_spawn_single_ingress_session`.
- Must not change: the single capture owner and SPSC subscriber structure (D2); per-utterance Silero reset; the PTT `/inherent/asr-submit` path; pre-roll and VAD thresholds; L3 packet or prompt contents; SenseVoice as the sole authoritative recognizer.
- Non-goals: keyword or natural barge-in; enabling natural mode; collecting the Tier 2 corpus (30 to 100 utterances per profile); device profiles; any LLM-based endpointing.

## Rejected approaches
- A streaming recognizer model (for example a sherpa zipformer) as the partial producer — D7 forbids a second authoritative recognizer and it would double model memory.
- LLM call to judge completeness — adds a network round trip inside the endpoint window and the ADR pins local heuristics.
- Reusing `max_utterance_s` as the semantic hold bound — it caps total capture length, not the post-pause hold, and would silently lengthen every turn.

## Acceptance evidence
- Positive (hermetic): an integration harness under tests/integration (no tests/unit) drives the assembler with a fake recognizer that returns scripted partial revisions and shows raw pytest output for: stable prefix advances only after two identical revisions; a complete-looking stable prefix commits before `max_hold_ms`; an incomplete-looking prefix holds until `max_hold_ms` then commits; speech resume during the hold returns to `speech_active` with no commit; a slow partial decode degrades to acoustic endpointing and emits `partial_asr_degraded`; a partial revision arriving after commit is discarded and only the final text reaches `utterance.received`.
- Positive (replay live, required): with `partial_asr.enabled: true`, a WAV produced by macOS `say` containing a sentence with an inserted 600 ms mid-sentence pause and a second WAV with two separate sentences are fed through the file-replay backend into the real Silero and SenseVoice models; show the trace trail proving the first WAV commits as one utterance and the second as two, with `endpoint_decision` verdicts and held_ms shown. Canary values: the two utterance_ids and their `endpoint_reason`.
- Regression: `PYTHONPATH=. .venv/bin/python -m pytest -q tests/integration/test_voice_vad_endpoint.py tests/integration/test_wave3_single_audio_ingress.py tests/integration/test_inherent_server_asr_submit.py` passes unchanged; with `enabled: false` the existing two-utterance test still reports `endpoint_reason == "acoustic_pause"`; full hermetic suite, `lint-imports`, `ruff check .`, and `mypy --strict jarvis tests scripts tools` exit 0. Raw output shown.
- Mic smoke with Allen speaking is a follow-up after this card, not part of its evidence.

## Docs to sync
- docs/adr/0006-full-duplex-voice-session.md D3 (as amended by the pending errata) — record the endpoint phase enum as the built Input FSM slice; D7 — judged unchanged unless the budget rule or heuristic deviates from its text, then amend D7.
- docs/spec.html — judged unchanged unless a §3 contract names the endpoint rule; say so explicitly.

## Open questions
(none)

## /goal condition
Implement docs/goals/endpointing-partial-asr.md on the current branch. The goal is met when all of the following appear in the transcript: (1) the diff adds partial-decode, stable-prefix, hold, and hard-max endpointing under `realtime.single_audio_ingress.partial_asr` with `enabled` defaulting to false, a file-replay backend implementing the existing `AudioDuplexBackend` Protocol, and no change to the single capture owner, Silero reset, PTT path, VAD thresholds, or any file under jarvis/decision; (2) raw pytest output of the new integration harness covering the six scripted cases in Acceptance evidence, ending in a pass line; (3) raw output of the replay live run showing the one-utterance and two-utterance results with utterance_ids, `endpoint_reason`, and `endpoint_decision` traces quoted; (4) raw output of the three named regression test files, the full hermetic suite with live tests excluded, `lint-imports`, `ruff check .`, and `mypy --strict jarvis tests scripts tools`, each ending with a pass line or exit 0; (5) ADR-0006 D3 and D7 and docs/spec.html each updated or explicitly judged unchanged, following the rule: update the canonical document that owns a changed contract, do not document what the code makes clear, do not duplicate a fact across documents; (6) each slice committed with the project commit skill and `git status` clean; (7) a Progress line per slice in the card. Or stop after 60 turns.

## Progress
- partial decode + completeness heuristic — 1b53026 — `SenseVoiceRecognizer.partial_text` on the serialized decode lock; `looks_complete` rule table.
- semantic endpoint hold under `partial_asr` — f8c7383 — `EndpointPhase`, `PartialAsrLane`, hold/max-hold/degrade in the assembler, config block default off; harness 21 passed, regression trio 88 passed, full suite 736 passed.
- heuristic calibration from the `say` probe — e2b04d2 — SenseVoice appends "。" to every snapshot, so the dangling check runs before punctuation; `candidate_ms` 320 (a 288 ms comma pause must not open a hold).
- file-replay backend + replay script — 4eafafb — `FileReplayBackend` through the real `AudioIngress`, 4 replay tests; first live run split one.wav because the stable prefix lagged the hypothesis.
- completeness only on a converged hypothesis — 8e3e89f — live replay: one.wav = 1 utterance Udf7b9be65a8f8da9 (hold → resume held_ms=384 → commit stable_prefix_complete, transcript "我想问一下，如果明天下雨的话，我们还去公园吗？"), two.wav = U9525ca0537fabc63 + U4588eb8d8a461784 both stable_prefix_complete; harness+replay 29 passed, full suite 744 passed.
- docs sync — 58bd1f4 — ADR-0006 D3 amended; D7 judged unchanged (budget rule and heuristic precedence are instances of its text, the converged-hypothesis guard uses the unstable suffix D7 already lists as an input); docs/spec.html judged unchanged (no §3 contract names the endpoint rule).
