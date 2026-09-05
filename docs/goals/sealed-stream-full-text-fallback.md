# Goal: sealed-stream-full-text-fallback

## Goal
A routine stream that seals before its first permit delivers the text it already generated as an ordinary full-text answer on the same ResponseRun — one LLM generation, spoken like any other answer — instead of failing the run and opening a correction run that regenerates from nothing.

## Why
ADR-0008 D2 rule 1 makes ambiguity default to `full_text` (docs/adr/0008-real-time-response-streaming.md:180) and rule 4 lets full text speak once the complete ResponsePlan passes the gate (:183). A zero-permit seal is exactly that case, but the code routes it through D3's prefix-invalid machinery instead, which exists to protect words already spoken. With zero permits nothing was spoken, so there is no prefix to protect. Live observation A3(a): "从一数到二十…" and "为什么天空是蓝色的" sealed at the first candidate (gate reasons `outside_evaluated_candidate_form`, `routine_ceiling_not_met`) and were never spoken at all (docs/goals/incremental-tts-from-permitted-segments.md:93).

## Current behavior
- `surface.response_open` for a streamed run is written only by `append_permitted_segment` on the first permit, guarded by an existing-opens check (jarvis/state/stream_emission.py:317-355). Zero permits therefore means no open, hence nothing for the L5 media owner to schedule.
- A stream seals on the first `admit()` whose gate returns no permit (jarvis/decision/__init__.py:2954-2956); `assemble()` then drops every later delta (:2963-2964), so `_stream_routine_text` returns `prefix=""`, `suffix=<the whole generated voice text>`, `emitted_segments=0` (:2989-2996).
- `_run_routine_stream` compares `attention_policy` against the pinned channel (:3014), then calls `finalize_stream` on that whole suffix (:3026-3032; jarvis/decision/stream_finalize.py:43-91). A suffix the classifier will not call routine returns `suffix_rejected` (stream_finalize.py:84-91); one regeneration follows with `gate_segments=False` (:3033-3048), then the second `finalize_stream`.
- Any surviving `StreamFinalizationFailure` returns a `DecideResult` with `stream_failure` set and `response_plan=None` (jarvis/decision/__init__.py:3049-3058). `drive_turn` fails the run with the durable prefix hash, unregisters it, and opens a full-text correction ResponseRun that re-asks the LLM (jarvis/runtime/__init__.py:2261-2303); correction runs keep `legacy_full_text_policy` (:1583, :1577). So a sealed stream costs two generations and, per A3(b), the second one can repeat the committed prefix (`voice_text` = S1+S1).
- The runtime keys delivery off the route label alone: `streamed = result.route == "casual_or_explanatory"` (jarvis/runtime/__init__.py:2307) drives both the runtime-side `turn.ended` (:2424) and `delivery_terminal_only=streamed` (:2460). A `DecideResult` with `route=None` is therefore already the full-text contract: `render_response` writes one `surface.response_open` with `kind="text"` (jarvis/surface/cli_render.py:181), the `required_gate_mode`-split `surface.response_chunk` rows, and one `surface.response_emitted` (:275-297) — the trail `voice_media` already speaks from.
- `_finalize_response(draft_text, packet, ctx, scratch)` (jarvis/decision/__init__.py:3155) is the ordinary full-text path: it runs `pre_emit_gate` (:3226), emits `gate.evaluated` with `gate="pre_emit"`, `outcome=<plan.permission>`, `attempt=0` (:3121-3132), owns the retry chain and `turn.ended` (:3374-3384), and returns a `DecideResult` with `route` unset (:3429-3434). `_run_routine_stream` is called with the same `packet` and `scratch` it needs (:1277-1278, :2999).
- The stream classifier's positive form allow-list is deliberately narrow and bilingual (jarvis/decision/stream_risk.py:164-178, `_supported_candidate`). It is the reason ordinary answers seal, and this card does not touch it.

## Target behavior
- A routine stream whose `emitted_segments == 0` never produces a `StreamFinalizationFailure`, never fails its run, and never opens a correction run. The generated text becomes a full-text ResponsePlan candidate for the same ResponseRun, judged by the ordinary Pre-emit Gate, and on pass is delivered through `render_response`'s emitted-time path (`kind="text"` open, chunks, one `surface.response_emitted` carrying `voice_text`), which `voice_media` already speaks.
- Exactly one LLM generation on the happy path. No second provider request, no regeneration, no re-gating of segments.
- The degrade is expressed entirely inside `decide()`: `_run_routine_stream` returns the full-text `DecideResult` shape (`response_plan` set, `route` unset, `emitted_segments=0`, `stream_failure=None`, `turn.ended` emitted by `_finalize_response`). `jarvis/runtime/__init__.py` needs no read of a new field and no new branch; `streamed` is already False for that shape.
- On Pre-emit Gate rejection the existing behavior applies unchanged — the retry chain inside `_finalize_response`, and if the run still cannot be terminalized, the existing failure/correction path with the same event sequence as today.
- Seal after at least one permit is unchanged: `attention_policy` comparison, `finalize_stream` on the suffix, one `suffix_rejected` regeneration, correction run on a surviving failure. The spoken/shown prefix is never rewritten (ADR-0008 D3, docs/goals/wire-routine-streaming.md:32).
- Retry de-duplication (A3(b)): when the one `gate_segments=False` regeneration returns text that begins with the committed prefix — compared after NFKC normalization and whitespace/punctuation trimming — the duplicated prefix is stripped before `finalize_stream` sees the suffix, so `ResponsePlan.text` is never `prefix + prefix + …`.
- Audit trail: the `gate.evaluated` rows already written for the sealed stream's candidates stay exactly as they are (`gate="stream_emit"`, `outcome="buffer_full_text"`, jarvis/decision/stream_gate.py:117). The fallback then writes its own `gate.evaluated` with `gate="pre_emit"`, `outcome=<plan.permission>`, `attempt=0` — one row per gate verdict, like any full-text run.

## Affected contracts and files
- L3 jarvis/decision/__init__.py — `_run_routine_stream` (:2999) gains the zero-permit branch that hands the generated text to `_finalize_response` and returns its result; the `suffix_rejected` regeneration block (:3033-3048) gains prefix de-duplication.
- L3 jarvis/decision/stream_finalize.py — only if the de-duplication is better owned next to the finalizer than in its caller.
- runtime jarvis/runtime/__init__.py — only if the `DecideResult` shape cannot carry the degrade; the preferred shape needs no change here.
- tests/integration/test_wire_routine_streaming.py, tests/integration/test_stream_finalizer.py — see Acceptance evidence.
- docs/adr/0008-real-time-response-streaming.md:242 — one sentence.

## Boundaries and non-goals
- Layers that may change: L3 (`jarvis/decision/__init__.py`, `jarvis/decision/stream_finalize.py`), runtime only if the `DecideResult` shape forces a read, tests, and one ADR sentence.
- Must not change: jarvis/decision/stream_risk.py (the allow-list and its thresholds), jarvis/decision/stream_gate.py, jarvis/state/stream_emission.py, jarvis/surface/ (including `cli_render`'s per-turn emit rule), the A2/A3 flags and their defaults, desktop/.
- Non-goals: broadening the routine allow-list so fewer answers seal; making the correction run silent-proof; A3 observation (d) (a cancelled run leaving no `turn.ended`); any change to the ≥1-permit seal path beyond de-duplication.

## Rejected approaches
- Keeping the correction run but seeding it with the already generated text — a second ResponseRun and a second cost disposition for an answer nothing ever exposed; ADR-0008 D3's correction machinery exists to protect a spoken prefix, and there is none.
- Broadening `_supported_candidate` so these answers stream instead of sealing — a quality change to the classifier that needs its own live evaluation; it would also leave the zero-permit seal unfixed for every answer that still falls outside the form.
- A new runtime branch keyed on `emitted_segments == 0` — the runtime already dispatches on `route`; a full-text `DecideResult` needs no new contract.
- Retrying generation without segment gating on a zero-permit seal — the text is already in hand; a second request buys nothing and re-opens the duplication bug.

## Acceptance evidence
- Positive (hermetic, tests/integration): raw pytest output showing (a) a zero-permit seal driven through the real `drive_turn`/`decide()` against the scripted localhost provider makes exactly one provider request, writes `surface.response_open` with `kind="text"`, the chunk rows, and one `surface.response_emitted` whose `voice_text` is the whole generated answer, writes no `response.failed`, opens no second `response.started`, returns `stream_failure=None`, and the emitted trail is scheduled for playback by the media owner using the test doubles the A3 tests already use (`_FakeProvider`, `_player`, `_CallbackPump`, `_config`, `_emit_response`, `_submit_response` from tests/integration/test_wave2_streaming_media.py, wired as in tests/integration/test_incremental_tts.py:319-334, :476-495); (b) a zero-permit seal whose text the Pre-emit Gate rejects still takes today's failure/correction path with an unchanged event sequence; (c) the existing seal-after-one-permit test (`test_consequential_segment_buffers_seals_and_suffix_is_regenerated_once`, tests/integration/test_wire_routine_streaming.py:397) and the correction-run test (:447) still pass untouched; (d) a `gate_segments=False` regeneration whose text starts with the committed prefix yields a `ResponsePlan.text` with the prefix present exactly once.
- Positive (live, required): daemon from this worktree on its own runtime root and port, overlay per docs/goals/incremental-tts-from-permitted-segments.md's live run (realtime.enabled, transactional_event_append, lifecycle_terminal_cas, exactly_once_cost_accounting, response_run_lifecycle, independent_response_cancel, `realtime.response.routine_streaming.enabled=true`, `realtime.streaming_output.enabled` + `speak_from_segments=true`, single_audio_ingress off), `MINIMAX_API_KEY` present, output switched to "BlackHole 16ch" with `SwitchAudioSource` and restored afterwards. Q1 "从一数到二十，用中文数字" seals at the first candidate: quote the response_id, exactly one LLM request in the realtime trace, the `surface.response_emitted` row carrying the full count, and `surface.playback_started` / `surface.playback_completed` for the same response_id. Q2 "为什么天空是蓝色的" still streams: `surface.response_open` with `kind="stream"` and at least one permit. If Q2 seals too, record its gate reasons as an owner follow-up on the allow-list, not a blocker. Canary = the two response_ids with their event sequences.
- Regression: `PYTHONPATH=. .venv/bin/python -m pytest -q -m "not live_llm and not live_codex"` prints `N passed, 64 deselected` where N = the integration count at launch (868 at 8e51deb) + the new cases; `PYTHONPATH=. .venv/bin/lint-imports`, `PYTHONPATH=. .venv/bin/ruff check .`, `PYTHONPATH=. .venv/bin/mypy --strict jarvis tests scripts tools` exit 0. Raw output shown. Always set `PYTHONPATH=.`: without it `jarvis` resolves through the editable install rooted at the main checkout and `tests/integration/test_wave2_streaming_media.py::test_voice_bench_provenance_fails_closed_before_provider_use` fails for that reason alone.

## Docs to sync
- docs/adr/0008-real-time-response-streaming.md:242 — the D3 bullet that owns the sealed-stream fail/correction rule ("the finalizer writes no terminal. If it finds the committed prefix itself invalid, the caller terminalizes the current response as failed/limited … and creates a separate correction ResponseRun") gains: a stream sealed with zero permits degrades to `full_text` in place, without regeneration.
- docs/adr/0008-real-time-response-streaming.md D2 (:180, :183) — judged unchanged; the degrade is rule 1 and rule 4 applied, not a new rule.
- docs/spec.html — judged unchanged; it owns no fact about stream sealing or the correction run.
- docs/goals/wire-routine-streaming.md — history, untouched.

## Open questions
(none)

## /goal condition
Implement docs/goals/sealed-stream-full-text-fallback.md on the current branch. The goal is met when all of the following appear in the transcript: (1) the diff makes a routine stream with `emitted_segments == 0` return the full-text `DecideResult` shape from `decide()` — plan set, `route` unset, `stream_failure` None, `turn.ended` from the ordinary Pre-emit Gate path — so the same ResponseRun delivers the already generated text with exactly one LLM generation, no `response.failed` and no correction run, and separately strips a duplicated committed prefix from the one `gate_segments=False` regeneration on the seal-after-a-permit path, with no change to jarvis/decision/stream_risk.py, jarvis/decision/stream_gate.py, jarvis/state/stream_emission.py, jarvis/surface/, the A2/A3 flags, or desktop/; (2) raw pytest output covering the four hermetic cases in Acceptance evidence — zero-permit seal delivers as `kind="text"` with one provider request and is scheduled for playback by the A3 media doubles, zero-permit seal with a Pre-emit rejection keeps today's failure/correction sequence, the existing seal-after-one-permit and correction-run tests still pass, and a prefix-repeating regeneration is de-duplicated — ending in a pass line; (3) raw output of the live run quoting both response_ids: Q1 "从一数到二十，用中文数字" sealed with exactly one LLM request in the trace, its `surface.response_emitted` carrying the full count, and `surface.playback_started` plus `surface.playback_completed` for that response_id; Q2 "为什么天空是蓝色的" still opening `kind="stream"` with at least one permit, or its gate reasons recorded as an owner follow-up; (4) raw output of `PYTHONPATH=. .venv/bin/python -m pytest -q -m "not live_llm and not live_codex"` ending in `N passed, 64 deselected` with N = 868 + the new cases and zero failures, plus `lint-imports`, `ruff check .`, and `mypy --strict jarvis tests scripts tools` each exiting 0; (5) the ADR-0008 D3 sentence at :242 amended with the zero-permit degrade, and ADR-0008 D2 and docs/spec.html each explicitly judged unchanged, following the rule: update the canonical document that owns a changed contract, do not document what the code already makes clear, do not duplicate a fact across documents; (6) each slice committed with the project commit skill and `git status` clean; (7) a Progress line per slice in the card. Or stop after 50 turns.

## Progress
- Zero-permit degrade — 9731365 — `_run_routine_stream` hands a stream
  sealed before its first permit to `_finalize_response`; hermetic
  `11 passed` on tests/integration/test_wire_routine_streaming.py, the new
  degrade test asserting one provider request, `kind="text"`, one
  `surface.response_emitted` carrying the whole answer, no `response.failed`,
  and `surface.playback_started`/`surface.playback_completed` from the real
  L5 media owner; the Pre-emit-refusal test comparing 9 events and both gate
  verdicts against the same turn with routine streaming off. Suite
  `914 passed, 64 deselected`.
- Prefix de-duplication — 93a10a4 — `_without_repeated_prefix` strips a
  regeneration's restatement of the committed prefix; the new test fails
  (`ResponsePlan.text` carries the sentence twice) with the call reverted and
  passes with it, existing seal/correction tests untouched. Suite
  `915 passed, 64 deselected`.
- Live run — daemon from this worktree, DeepSeek v4-flash direct + MiniMax TTS,
  overlay from config/jarvis.yaml with realtime.enabled, the three
  concurrency_safety switches, response_run_lifecycle,
  independent_response_cancel, routine_streaming.enabled,
  streaming_output.enabled + speak_from_segments, single_audio_ingress off;
  output "BlackHole 16ch" during, "MacBook Pro Speakers" restored after. One
  fresh runtime root per question — a root that already holds a completed turn
  makes `pre_route` return `unknown` (the A3(c) history observation), and the
  stream route never opens. Q1 "从一数到二十，用中文数字" on ~/.jarvis-lane-a-q1
  port 8031, RESP867aea258dd0415c9124bc5d50356add: `response.started` route
  `casual_or_explanatory` / `routine_stream`, sealed at sequence 0
  (`gate.evaluated` id 5, `stream_emit`/`buffer_full_text`, candidate_risk
  `unknown`, reasons `outside_evaluated_candidate_form` +
  `routine_ceiling_not_met`), exactly one LLM request (trace
  `llm_sdk_request_call_started_upper_bound` x1, one `cost.recorded`, one
  `response.request_admitted`), trace `routine_stream_degraded_to_full_text`
  (148 chars), `surface.response_open` id 10 `kind="text"` /
  `attention_channel: voice_notify`, `surface.response_emitted` id 17 carrying
  the whole count 一…二十, `surface.playback_started` id 18 and
  `surface.playback_completed` id 25 for the same response_id, 0
  `response.failed`, 1 `response.started`.
- Owner follow-up (not a blocker, allow-list quality): Q2 "为什么天空是蓝色的"
  on ~/.jarvis-lane-a-q2 port 8032, RESPa0ab87fcf8954980b2ac0841383ad5c8 sealed
  too — first candidate "这个问题其实很好回答。" (segment_hash 85c1e9f7…),
  `stream_emit`/`buffer_full_text`, reasons `outside_evaluated_candidate_form` +
  `routine_ceiling_not_met`: the sentence carries no `_EXPLANATORY_FORM`
  keyword, so `_supported_candidate` refuses it. The degrade delivered it
  anyway on one generation — `kind="text"` open id 10, emitted id 32,
  `surface.playback_started` id 33 (the answer was still speaking at 547
  characters when the daemon was stopped); before this card the same turn cost
  a `suffix_rejected` regeneration and a correction run.
- Acceptance evidence (b), read as the retry chain, not a run failure — a
  Pre-emit Gate refusal after the degrade is structurally unreachable in
  production: `pre_route` returns `unknown` whenever a task is open
  (jarvis/decision/pre_route.py:119), and `scratch.active_subject_ref` is only
  set on resolver/tool paths the routine stream never takes, so
  `_active_subject_or_default` returns None on this path and `pre_emit_gate`
  short-circuits to pass-through. The card's "keeps today's failure/correction
  sequence" is therefore tested as Target-behavior line 22 states it — the
  retry chain inside `_finalize_response`, compared event for event against the
  same turn with routine streaming off — with the subject forced identically on
  both sides. Owner call to confirm this reading; nothing in the card can be
  satisfied literally.
- Verifier pass — 82e75bc, 2ec098a — confirmed and fixed: (1) the
  de-duplication cut inside a word ("好的。" + "好的话我们继续。" became
  "好的。话我们继续。") and re-normalized every slice, 18s on 20k characters of
  punctuation — now one pass, a repeat accepted only when the next character is
  not compared text, 0.007s, with two tests (a regeneration that only starts
  like the prefix is kept whole; one that restates nothing but the prefix ships
  the exposed sentence once); (2) the ADR sentence read as if the run's
  `emission_mode` changed — it still records `routine_stream`, only delivery
  changes; (3) `tests/scenarios/test_live_crash_recovery.py` `_Sealed` docstring
  listed outcomes a zero-permit seal no longer has. Confirmed clean: the
  DecideResult shape and the untouched runtime, the boundary files (empty diff
  for stream_risk / stream_gate / stream_emission / surface / desktop / config),
  the three-test revert check (3 failed with the change reverted, 9 existing
  passed), the ADR D2 and spec.html unchanged judgements, and the enveloped /
  cancel / cost / attention / duplicate-open / gate-mode risk review. Noted, not
  fixed: `?? .venv` in `git status` is a symlink that `.gitignore`'s `.venv/`
  pattern cannot match — pre-existing worktree setup, never staged. Suite
  `917 passed, 64 deselected`; the card's "868 + new cases" is the launch-time
  number — the integration baseline is 912 at d120d1a (verifier measured the
  same at 5a75f2a), and 912 + 5 = 917.

