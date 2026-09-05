# Goal: wire-routine-streaming

## Goal
A routine turn (conversational, low risk, no tool or evidence need) streams permitted segments to the surface while the LLM is still generating, behind a default-off flag, with the committed prefix immutable, exactly one cost disposition, no duplicate delivery, and TTS still starting at `surface.response_emitted`.

## Why
This is ADR-0008 §8 Step 8 and the first user-visible latency win of the program; every piece of the D2/D5 machinery exists with zero production callers.

## Current behavior
- `drive_turn` opens every ResponseRun with `legacy_full_text_policy` (jarvis/runtime/__init__.py:_start_drive_turn_response, :1513-1560; jarvis/decision/response_run.py:120-134 says "nothing can emit early by construction").
- `decide()` has one text-producing path: the tools-offered loop calling batch `chat()` through `_run_llm_chat_with_cost_guard` (jarvis/decision/__init__.py:1245-1360, :1264); `_finalize_response` gates the complete draft (:2860-2941). No no-tool path exists.
- `routine_stream_policy` needs a complete `ResponseRiskContext` up front; `_context_risk` returns `routine` only for `route == "casual_or_explanatory"` with `tools_offered=False`, no subject, no linked actions, no pending confirmation, complete context, and `attention_channel` in {voice_notify, queue_review, silent_log} (jarvis/decision/stream_gate.py:31-51; jarvis/decision/stream_risk.py:201-232, :205-206, :218). No route classifier exists in production; `casual_or_explanatory` appears only in tests.
- `final_attention_channel` is known only after `decide()` returns (jarvis/runtime/__init__.py:2160-2167).
- `terminalizer.complete()` runs before `render_response`, and `render_response` emits its own post-hoc `surface.response_open/chunk/emitted` from the complete plan (jarvis/runtime/__init__.py:2273-2295; jarvis/surface/cli_render.py:438-467, :193-227) with no guard against segments already emitted.
- `CostRecorder.stream_events` wraps `LLMClient.stream_events` with exactly-once settlement (jarvis/decision/cost_guard.py:320-349; jarvis/decision/llm.py:526-566; jarvis/decision/llm_stream.py:495-501); the static canary `_LLM_METHODS = {"chat", "chat_stream", "stream_events"}` enforces one cost disposition per call site (tests/canary/test_canary_cost_recorded_emitted_per_llm_call.py:45).
- `emit_permitted_segment` writes `surface.response_open` and `surface.response_chunk` with the gate event as `source_event_id` (jarvis/surface/stream_emission.py:19; jarvis/state/stream_emission.py:252-352); `_response_watcher` dispatches by event type and needs no change (jarvis/runtime/inherent_loop.py:986-1053).
- `realtime.streaming_output` is the TTS-side flag (config/jarvis.yaml:197); no L3 text-streaming flag exists. The ADR-0008 §6 errata sentence currently says `routine_streaming` does not exist.
- The finalizer card (docs/goals/immutable-prefix-finalizer.md) provides `finalize_stream`, `StreamFinalizationFailure` (reasons `suffix_rejected`, `committed_prefix_invalid`, `policy_mismatch`), prefix reconstruction, and `corrects_response_id`.

## Target behavior
- ADR-0008 D2 (pre-route policy plus per-segment permit; policy only tightens mid-run; unknown buffers), D3 (finalizer), D12 route table, and §4.3 (commit before publish) are the contract.
- Flag: `realtime.response.routine_streaming.enabled`, default false, valid only with `response_run_lifecycle` true; off means byte-identical behavior to today.
- Pre-route is deterministic and LLM-free, computed in L3 before the ResponseRun opens: `casual_or_explanatory` only when the trigger is a user utterance, Tier 0 did not hit, `active_subject_ref` is None, no pending confirmation, no linked or pending actions, references resolved, context complete, history status ok, and the utterance matches no entry of a data-driven tool-cue table (config file next to the Tier 0 patterns, bilingual, shipped broad and conservative: action verbs, file/repo/app/web/task nouns, imperative shapes). Anything else takes today's path unchanged. The chosen route is recorded on `response.started` (a `route` payload field).
- On the routine route the run opens with `routine_stream_policy` and a pinned `attention_channel = voice_notify` (ordinary answers now default to it); `decide()` calls `CostRecorder.stream_events` with `tools=None`, feeds deltas through `SemanticAssembler`, `SegmentRiskClassifier`, `stream_emission_gate`, and `emit_permitted_segment` (each segment committed then published), then `finalize_stream(committed_prefix, uncommitted_suffix, policy)`.
- Success: `DecideResult` carries the finalized `ResponsePlan` and the count of already-emitted segments; `drive_turn` completes the run through the terminalizer and `render_response` emits only `surface.response_emitted` for that response_id, never a second `surface.response_open` or duplicate chunks. Ordering: permitted chunks, then `response.completed`, then `surface.response_emitted`.
- `policy_mismatch` (for example decide() resolving a different attention channel) or `committed_prefix_invalid`: the run is terminalized failed with `committed_prefix_hash`, and a correction ResponseRun with `corrects_response_id` runs the existing full-text path for the same turn; the spoken/shown prefix is never rewritten. `suffix_rejected`: one regeneration of the suffix only, then the same failure path.
- Cancel mid-stream through the existing independent response cancel: the stream handle is cancelled, the run is terminalized cancelled with `committed_prefix_hash`, no further chunks are published, exactly one cost disposition is recorded. No delivery terminal is written for a cancelled run in this card (the Inherent v1 watchdog resets the card; ADR-0014 D15's `surface.response_failed` is a later card).
- A tool-cue or high-risk classification never reaches early output: cue-matched requests route non-streaming; a segment classified consequential/unknown buffers and seals the run per D2.
- TTS behavior is unchanged: both the legacy pipeline and voice_media still schedule speech at `surface.response_emitted`.

## Affected contracts and files
- L3 jarvis/decision/__init__.py — pre-route step, the no-tool streaming path inside decide(), `DecideResult` fields for emitted-segment count and route; jarvis/decision/stream_risk.py or a new sibling — the tool-cue table loader; config/tool_cues.yaml (or the existing tier0 config directory convention).
- L3 jarvis/decision/response_run.py — policy selection helper for the routine route.
- L2 jarvis/state/event_log.py — optional `route` on `response.started`.
- runtime jarvis/runtime/__init__.py — thread the stream seam through `DecideContext` the way `cancellation_checkpoint` is threaded; choose the policy at run open; suppress post-hoc open/chunk emission for routine runs; failure and correction-run orchestration; cancel plumbing.
- L5 jarvis/surface/cli_render.py — `render_response` gains a "delivery terminal only" mode bound to an already-open response.
- config/jarvis.yaml — the flag under `realtime.response`.
- tests/integration, tests/scenarios — see Acceptance evidence.

## Boundaries and non-goals
- Layers that may change: L3, L2 (one optional field), runtime, L5 `cli_render.py` only, config, tests.
- Must not change: `voice_media.py`, `voice_tts.py`, `_tts_watcher`, `inherent_output.py`; the gate's hash pinning; the terminal-only-through-CAS ownership; the one-cost-disposition rule; `legacy_full_text_policy` for non-routine turns; Tier 0; the tool loop for tools-offered turns.
- Non-goals: incremental TTS from chunks (next card); speech/document sibling runs (D7); `ActionGroup`; fast/deep preset routing and its eval (D12 second half); a delivery terminal for cancelled runs.

## Rejected approaches
- Offering tools on the routine route and sealing on the first tool-call delta — `_context_risk` and §8 Step 8 pin `tools=None` for routine; offering tools makes every segment unknown-risk.
- An LLM call to classify the route — D12 forbids an extra LLM round for routing.
- Streaming through `render_response` by splitting partial text — bypasses the gate and permits; every early segment must carry a committed `gate.evaluated` source.

## Acceptance evidence
- Positive (hermetic, tests/integration or tests/scenarios, no tests/unit): raw pytest output for a turn driven through the real `drive_turn`/`decide()` with a fake typed provider stream showing: the first `surface.response_chunk` row is committed before the fake provider signals completion (assert by event order against a provider-completion marker event or trace); exactly one `surface.response_open` for the response_id and zero chunks re-emitted by `render_response`; `response.completed` precedes `surface.response_emitted`; `ResponsePlan.text` equals the committed prefix plus the approved suffix; exactly one cost disposition for the request; a cue-matched request (for example "帮我打开这个文件") takes the non-streaming path with tools offered and no early chunk; a segment classified consequential buffers and seals the run; cancel mid-stream yields `response.cancelled` with `committed_prefix_hash`, no later chunk, one cost disposition; a forced `policy_mismatch` produces a failed run with `committed_prefix_hash` and a correction run with `corrects_response_id` whose delivery contains the prefix unchanged. A data-driven test for the tool-cue table.
- Positive (live, required): daemon from this worktree on its own runtime root and port with the flag on and DeepSeek `fast`; ask one casual question and show the event trail with the first `surface.response_chunk` timestamp earlier than the run's completion (quote both timestamps and the response_id); ask one action-shaped question and show it routed non-streaming (`route` on `response.started`, no early chunk); show `surface.playback_started` still occurs for the casual answer after `surface.response_emitted`.
- Regression: `PYTHONPATH=. .venv/bin/python -m pytest -q tests/canary/test_canary_response_terminal_only_through_cas.py tests/canary/test_canary_cost_recorded_emitted_per_llm_call.py tests/canary/test_stream_risk_boundaries.py tests/integration/test_stream_emission_gate.py tests/integration/test_typed_llm_stream.py tests/integration/test_wave4a_response_run.py` passes; with the flag off the full hermetic suite matches baseline; `lint-imports`, `ruff check .`, `mypy --strict jarvis tests scripts tools` exit 0. Raw output shown.

## Docs to sync
- docs/adr/0008-real-time-response-streaming.md §6 — the errata sentence saying `routine_streaming` does not exist is replaced by the real key; D2/D12 judged unchanged unless the pre-route rule deviates, then amend D12 in place.
- docs/spec.html §5.4 — the `route` field on `response.started` where the spec lists payloads; otherwise say so.

## Open questions
(none)

## /goal condition
Implement docs/goals/wire-routine-streaming.md on the current branch. The goal is met when all of the following appear in the transcript: (1) the diff adds the default-off `realtime.response.routine_streaming.enabled` flag, a deterministic LLM-free pre-route with a data-driven tool-cue table, the routine no-tool streaming path through `CostRecorder.stream_events`, assembler, classifier, gate, `emit_permitted_segment`, and `finalize_stream`, the delivery-terminal-only render mode, the failure and correction-run orchestration, cancel plumbing, and the optional `route` field, with no change to voice_media.py, voice_tts.py, `_tts_watcher`, inherent_output.py, gate hashing, terminal ownership, or the tools-offered loop; (2) raw pytest output of the hermetic scenario covering the nine cases in Acceptance evidence plus the tool-cue table test, ending in a pass line; (3) raw output of the live run quoting the response_id with the first chunk timestamp earlier than completion, the action-shaped question routed non-streaming, and playback after `surface.response_emitted`; (4) raw output of the six named regression files, the full hermetic suite with the flag off, `lint-imports`, `ruff check .`, and `mypy --strict jarvis tests scripts tools`, each ending with a pass line or exit 0; (5) ADR-0008 §6, D2, D12 and docs/spec.html §5.4 each updated or explicitly judged unchanged, following the rule: update the canonical document that owns a changed contract, do not document what the code makes clear, do not duplicate a fact across documents; (6) each slice committed with the project commit skill and `git status` clean; (7) a Progress line per slice in the card. Or stop after 80 turns.

## Progress
