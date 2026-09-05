# Goal: immutable-prefix-finalizer

## Goal
L3 can turn a sequence of permitted, committed stream segments plus an uncommitted suffix into a final `ResponsePlan` whose text is byte-for-byte the committed prefix plus the approved suffix, or into a typed failure that leads to a correction ResponseRun, never to a whole-answer retry.

## Why
ADR-0008 D3 names `finalize_stream` as the piece that makes spoken prefixes immutable. Every streaming route card (routine streaming wire, sibling runs) depends on it, and nothing of it exists.

## Current behavior
- Production never streams: `decide()` calls `chat()` synchronously and `pre_emit_gate` builds a frozen `ResponsePlan` (jarvis/decision/__init__.py:422, jarvis/decision/gates.py:pre_emit_gate :697; `ResponsePlan` fields at gates.py:105-138: text, permission, downgrade_required, active_claim_levels, response_hash, output_risk_class, required_gate_mode).
- The ResponseRun is always opened with `legacy_full_text_policy` (jarvis/runtime/__init__.py:1534; jarvis/decision/response_run.py:121-144) and completed with `terminalizer.complete(run.facts, response_hash=...)` before render (jarvis/runtime/__init__.py:2270).
- The D2/D5 stream machinery exists with test-only callers: `chat_stream` (jarvis/decision/llm.py:510), `SemanticAssembler` (jarvis/decision/stream_sentences.py), `SegmentRiskClassifier` (jarvis/decision/stream_risk.py), `routine_stream_policy` / `stream_emission_gate` / `EmissionPermit` (jarvis/decision/stream_gate.py:31, :127-139), `append_stream_gate` / `append_permitted_segment` (jarvis/state/stream_emission.py), `emit_permitted_segment` (jarvis/surface/stream_emission.py:19). A permitted segment commits `gate.evaluated` and a `surface.response_chunk` whose `source_event_id` is the gate event; no `response.completed` or `surface.response_emitted` follows (tests/integration/test_stream_emission_gate.py:141-176).
- No `finalize_stream`, `StreamFinalizationFailure`, `committed_text_prefix`, or `next_segment_sequence` exists in jarvis/. `committed_prefix_hash` is an optional, never-populated payload key on `response.cancelled` / `response.failed` (jarvis/state/event_log.py:769, :779; jarvis/decision/response_run.py:544-591).
- `response.started` has no causal link for a correction run (jarvis/state/event_log.py:728-751); `claim.superseded` carries `superseded_by_claim_id` (jarvis/state/event_log.py:569-572) as the existing pattern.
- Multi-way durable outcomes use frozen dataclass unions (`CancelAccepted | CancelAlreadyTerminal | CancelRejected | CancelTimedOut`, jarvis/decision/response_run.py:424-452).
- Response terminals are written only through `ResponseTerminalizer`, pinned by tests/canary/test_canary_response_terminal_only_through_cas.py.
- `stream_emission_gate` refuses evaluation when policy or context hashes disagree (jarvis/decision/stream_gate.py:73-81).

## Target behavior
- ADR-0008 D3 (branch text, §3 D3) is the contract: `finalize_stream(committed_prefix, uncommitted_suffix, policy) -> ResponsePlan | StreamFinalizationFailure`; every permitted segment is appended to the committed prefix; the final gate may inspect the accumulated full answer but a retry may regenerate only the uncommitted suffix; `ResponsePlan.text` is byte-for-byte `committed_prefix + approved_suffix`; text already spoken is never altered; an invalid committed prefix yields a failure whose handling terminalizes the current response as failed/limited and creates a separate correction ResponseRun, never a whole-answer retry and never a reset of voice history.
- `StreamFinalizationFailure` is a frozen dataclass with `reason: Literal["suffix_rejected", "committed_prefix_invalid", "policy_mismatch"]`, the committed prefix hash, and the gate outcome that produced it, following the Cancel* union pattern. It is a return value, not an exception.
- The committed prefix is derived from the durable chain, not from memory alone: a helper reconstructs `committed_text_prefix` and `next_segment_sequence` for a `response_id` from its `gate.evaluated` permits and `surface.response_chunk` rows, and the finalizer validates the caller-supplied prefix against that reconstruction under the same pinned policy and evidence hashes the gate uses.
- The finalizer writes no terminal. The caller (a runtime code path in a later card; in this card the test harness) passes the returned `ResponsePlan.response_hash` to `ResponseTerminalizer.complete`, or on `committed_prefix_invalid` calls `ResponseTerminalizer.fail` with `committed_prefix_hash` populated and starts a correction run.
- L2: `response.started` gains optional `corrects_response_id`; `response.failed` and `response.cancelled` populate `committed_prefix_hash` whenever a committed prefix exists.
- No production caller is added; `legacy_full_text_policy` and the full-text route are untouched.

## Affected contracts and files
- L3 jarvis/decision/stream_gate.py or a new sibling module — `finalize_stream`, `StreamFinalizationFailure`, prefix reconstruction/validation.
- L3 jarvis/decision/response_run.py — `ResponseTerminalizer.fail`/`cancel` accept and record `committed_prefix_hash`; `start_response_run` accepts `corrects_response_id`.
- L2 jarvis/state/event_log.py — the two schema additions above, and jarvis/state/response_runs.py if the started-row writer needs the new field.
- L2 jarvis/state/stream_emission.py — read side for prefix reconstruction if not already exposed.
- tests/integration — the end-to-end hermetic chain test and the failure-path tests; docs/adr/0008 D3 judged unchanged or amended.

## Boundaries and non-goals
- Layers that may change: L3 (`jarvis/decision`), L2 (`jarvis/state` registry fields and read helpers), tests.
- Must not change: any production call path in jarvis/runtime (no wiring of the streaming route); `ResponsePlan`'s field set; the gate's hash-pinning discipline; the transactional publish guarantees proven by `test_commit_failure_never_publishes_and_retry_retains_sequence`; the terminal-only-through-CAS canary; `legacy_full_text_policy`.
- Non-goals: wiring `chat_stream` into production; sibling speech/document runs; TTS; changing the risk classifier; the runtime correction-run orchestration beyond what the harness demonstrates.

## Rejected approaches
- Raising an exception on finalization failure — the callers need a typed, matchable outcome like the Cancel* union, and exceptions would cross the terminalizer boundary uncontrolled.
- Trusting an in-memory prefix without reconstruction — a crash between permit and finalize would let a never-published segment be spoken as "already committed".
- Whole-answer regeneration when the suffix fails — D3 forbids it; the spoken prefix would change.

## Acceptance evidence
- Positive (hermetic, tests/integration, no tests/unit): raw pytest output for a chain test that drives a fake typed stream through `SemanticAssembler`, `SegmentRiskClassifier`, `stream_emission_gate`, `emit_permitted_segment`, then `finalize_stream`, and asserts `ResponsePlan.text == committed_prefix + approved_suffix` byte-for-byte and that `ResponseTerminalizer.complete` with the returned hash yields `response.completed`; a suffix-rejection test where the first suffix fails, the prefix is unchanged, and a second suffix succeeds; a prefix-invalid test where the failure carries `committed_prefix_invalid`, the harness terminalizes the run failed with `committed_prefix_hash` populated, and a correction run starts with `corrects_response_id` equal to the failed response_id, both events shown; a restart test where the prefix reconstructed from the Event Log equals the in-memory prefix; a policy-mismatch test where a policy hash different from the permits' yields `policy_mismatch`.
- Regression: `PYTHONPATH=. .venv/bin/python -m pytest -q tests/integration/test_stream_emission_gate.py tests/integration/test_typed_llm_stream.py tests/canary/test_stream_risk_boundaries.py tests/canary/test_canary_response_terminal_only_through_cas.py` passes; full hermetic suite, `lint-imports`, `ruff check .`, `mypy --strict jarvis tests scripts tools` exit 0. Raw output shown.
- Live run: not required. There is no production caller in this card and the streaming flags stay off; state this explicitly in the report.

## Docs to sync
- docs/adr/0008-real-time-response-streaming.md D3 — judged unchanged unless the failure reasons or reconstruction rule deviate from its text.
- docs/spec.html §5.4 (event contracts) — add `corrects_response_id` and the populated `committed_prefix_hash` where the spec lists response event payloads; if §5.4 does not enumerate payload fields, say so.

## Open questions
(none)

## /goal condition
Implement docs/goals/immutable-prefix-finalizer.md on the current branch. The goal is met when all of the following appear in the transcript: (1) the diff adds `finalize_stream` returning `ResponsePlan | StreamFinalizationFailure` with the three reasons, a durable prefix reconstruction helper, the `corrects_response_id` and populated `committed_prefix_hash` schema changes, and no change under jarvis/runtime, to `ResponsePlan`'s fields, or to `legacy_full_text_policy`; (2) raw pytest output of the five integration tests named in Acceptance evidence (chain byte-for-byte, suffix rejection then success, prefix invalid with correction run and both events shown, restart reconstruction, policy mismatch), ending in a pass line; (3) raw output of the four named regression files, the full hermetic suite with live tests excluded, `lint-imports`, `ruff check .`, and `mypy --strict jarvis tests scripts tools`, each ending with a pass line or exit 0; (4) an explicit statement that no live run is required because no production caller exists; (5) ADR-0008 D3 and docs/spec.html §5.4 each updated or explicitly judged unchanged, following the rule: update the canonical document that owns a changed contract, do not document what the code makes clear, do not duplicate a fact across documents; (6) each slice committed with the project commit skill and `git status` clean; (7) a Progress line per slice in the card. Or stop after 50 turns.

## Progress
- L2 prefix reconstruction (`committed_text_prefix`, `CommittedPrefix`) + restart test — 9c6ab3a — `pytest tests/integration/test_stream_finalizer.py` 1 passed; hermetic 716 passed / 63 deselected; lint-imports KEPT, ruff clean, mypy strict clean (201 files).
- L3 `finalize_stream` + `StreamFinalizationFailure` (jarvis/decision/stream_finalize.py) + chain / suffix-rejection / policy-mismatch tests — 6140da5 — `pytest tests/integration/test_stream_finalizer.py` 4 passed; hermetic 719 passed / 63 deselected; lint-imports KEPT, ruff clean (204 files), mypy strict clean (202 files).
- L2 `corrects_response_id` on response.started + `committed_prefix_hash` on fail/cancel via ResponseTerminalizer, `start_response_run(corrects_response_id=)` + prefix-invalid/correction-run test — a86e719 — `pytest -s tests/integration/test_stream_finalizer.py` 5 passed with response.failed{committed_prefix_hash=a26e87d6…} and response.started{corrects_response_id=response-stream-test} printed; hermetic 720 passed / 63 deselected; lint-imports KEPT, ruff clean (204 files), mypy strict clean (202 files).
- Docs sync — 9c179be — ADR-0008 D3 amended (durable-chain reconstruction, finalizer writes no terminal, caller fails with committed_prefix_hash and links the correction run by corrects_response_id); §4.2 response.started optional gains corrects_response_id; docs/spec.html §5.4 judged unchanged because it does not enumerate response.* payload fields (only §5.4.4 request_admitted / turn.ended).
