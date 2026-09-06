# Goal: heard-prefix-observable

## Goal
The heard-prefix cursor-quality bug is pinned by a test that drives the real
`StreamingTTSPipeline` and asserts on the emitted `surface.playback_checkpoint`
payload, and no test in `tests/integration/test_wave2_streaming_media.py` asserts
only on an in-memory object it constructed itself.

## Why
This corrects a hub authoring error. The previous card's rulings said "ONE new
integration test in `tests/integration/` that drives the real `PlaybackLedger`".
That phrase labelled a unit test as an integration test by directory. `tests/unit`
was retired long ago, so every Python test in this repository already lives under
`tests/integration/`; the directory constrained nothing. The lane implemented
exactly what it was told and produced
`test_heard_prefix_quality_survives_an_earlier_report_gap` — a single-class
isolation test that constructs a `PlaybackLedger`, calls its methods, and asserts
on `snapshot()`.

Allen's ruling, 2026-09-05: the prohibition on unit tests in
`.claude/rules/python-testing.md` ("`tests/unit` is retired. Do not add new Python
unit tests.") WINS over the same file's "data-driven input to expected output
checks" clause. A pure-function or single-class assertion is a unit test even when
it is data-driven. The data-driven clause does not license one.

The production fix itself (`3260f6d`) is correct and stays. Only the way it is
verified changes.

## Current behavior
- `tests/integration/test_wave2_streaming_media.py:3004`
  `test_heard_prefix_quality_survives_an_earlier_report_gap` constructs a
  `GenerationLease` and a `PlaybackLedger` directly, calls `begin_segment` /
  `accept_samples` / `record_submitted` / `finish_segment` / `record_audible` by
  hand, and asserts on `ledger.snapshot()`. Nothing outside `PlaybackLedger`
  participates and no artifact is emitted. That is a unit test.
- The bug it pins is real and was previously uncovered, so it cannot simply be
  deleted (see Boundaries, replace-never-delete-first).
- The reproduction sequence, re-pinned against the current tree:
  1. A segment closes while its `output_end_cursor` is still greater than
     `_estimated_audible_cursor`, so the escape hatch in `finish_segment`
     (`jarvis/surface/voice_ledger.py:192`, condition at `:207-211`) does not fire
     and the chunk keeps its birth sentinel `cursor_quality = "unknown"` set at
     `jarvis/surface/voice_ledger.py:165`.
  2. `record_submitted` (`jarvis/surface/voice_ledger.py:213`) sees
     `output_start_cursor != self._submitted_cursor` — a callback-report gap — and
     takes the branch at `:226-235`: the LEDGER quality is pinned to an observed
     `"unknown"` for the rest of the lease and it returns early. The already
     closed chunk sits at or behind `_submitted_cursor`, so it is not marked
     `audibility_class = "unknown"` and stays `"normal"`.
  3. A later `record_audible` (`jarvis/surface/voice_ledger.py:245`) crosses the
     chunk's `output_end_cursor` and, via the first-observation branch at
     `:263-275`, gives that chunk `cursor_quality = "estimated"`.
  4. `snapshot()` (`jarvis/surface/voice_ledger.py:285`) admits the chunk into
     `heard_parts`, accumulates its quality at `:306-312`, and reports it at
     `:324` (`cursor_quality=self._cursor_quality if heard_quality is None else
     heard_quality`) instead of the lease watermark. That is the fix under test.
- The consumer that makes this matter is the fold at
  `jarvis/state/conversation_playback.py:212-213`, which drops the heard evidence
  entirely unless `cursor_quality` is in `{"estimated", "measured_dac"}`. It reads
  the value off the emitted event payload, never off a snapshot object.
- In production the gap of step 2 originates in the callback-report ring drop at
  `jarvis/surface/voice_tts.py:373-375` (ring constructed at `:681` with
  `capacity=2048`), drained into the ledger at `jarvis/surface/voice_tts.py:1299`
  → `record_submitted` at `:1303` and `record_audible` at `:1336`.

## Target behavior
- One test in `tests/integration/test_wave2_streaming_media.py` drives a real
  `voice_media.StreamingTTSPipeline` over a real `voice_tts.AudioStreamPlayer` and
  a real `PlaybackLedger` through the four-step sequence above, and asserts on the
  `surface.playback_checkpoint` row read back out of the event log.
- The assertion lands on that emitted payload, built at
  `jarvis/surface/voice_media.py:2856-2866` inside `_checkpoint_if_advanced`
  (`jarvis/surface/voice_media.py:2826`). The exact keys the test asserts on:
  - `heard_text` — non-empty, and equal to the first segment's spoken text.
  - `cursor_quality` — exactly `"estimated"`.
  The row also carries `session_id`, `response_id`, `turn_id`,
  `playback_generation_id`, `heard_through_sequence`, `submitted_samples` and
  `heard_text_hash`; assert on `heard_through_sequence` only as far as it is
  needed to select the right row.
- With `jarvis/surface/voice_ledger.py:324` reverted to
  `cursor_quality=self._cursor_quality`, that same test fails on the emitted
  payload carrying `"unknown"`.
- The existing ledger-level test at `:3004` is gone, deleted in the same commit
  that adds the replacement.

## Affected contracts and files
- L5 `tests/integration/test_wave2_streaming_media.py` — the replacement
  pipeline-level test replaces `test_heard_prefix_quality_survives_an_earlier_report_gap`
  at `:3004`; up to four bare candidates in the same file get a recorded
  disposition (see Boundaries).
- Machinery to REUSE, not rebuild. All in
  `tests/integration/test_wave2_streaming_media.py`:
  - `_FakeProvider` `:163` — fake TTS provider; `segment_gates` and `final_gates`
    hold a segment's audio or its `TTSSegmentFinished` so the second segment can
    be kept open while the first checkpoints.
  - `_FakeSession` `:57` and `_Behavior` `:48` — per-response behavior knobs
    (`samples`, `final_delay_s`, `sample_rate_hz`).
  - `_CallbackPump` `:205` — a context manager thread that drives
    `player._callback` continuously, standing in for PortAudio. No audio device.
  - `_player` `:243` — the standard `AudioStreamPlayer` for these tests. NOTE it
    hardcodes `estimated_output_latency_s=0.0`, which makes the escape hatch fire
    and defeats step 1. A non-zero latency is needed; `:2916` builds a player
    inline with `estimated_output_latency_s=0.2` for exactly this reason.
  - `_config` `:281` — `StreamingMediaConfig` with short timeouts.
  - `_emit_response` `:298` and `_submit_response` `:363` — emit the
    open/chunk/emitted event triple and feed it through `pipeline.submit_event`.
  - `_terminal_rows` `:373` — read back the terminal playback rows.
  - Working examples of exactly this shape, both of which poll the event log for a
    `surface.playback_checkpoint` row and assert on its parsed payload:
    `test_checkpoint_persists_during_later_provider_feed_and_retries` `:1163` and
    `test_structured_chunks_preserve_heard_prefix_on_mid_second_interrupt` `:1520`.

## Boundaries and non-goals
- Layers that may change: tests only. Production code: NONE. If you find yourself
  editing anything under `jarvis/`, stop — that means you found a real bug, which
  is a different card. The one permitted touch of `jarvis/` is the TEMPORARY
  revert of `jarvis/surface/voice_ledger.py:324` for the acceptance step, restored
  immediately and shown restored by a clean `git status`.
- Replace, never delete first. The bug the `:3004` test pins is real and was
  previously uncovered. Delete it ONLY in the same commit that adds its
  replacement. Never leave the behavior uncovered in between, not even across two
  commits in the same session.
- If it cannot be driven end to end, STOP and report. If the gap sequence cannot
  be produced through the pipeline with the machinery named above, do not fall
  back to the ledger-level test and do not invent a large new harness. Stop,
  report what blocks it, and leave the existing test in place. An honest blocked
  report is worth more than a second unit test.
- Must not change: `snapshot()`'s behavior in `jarvis/surface/voice_ledger.py`,
  the fold in `jarvis/state/conversation_playback.py`, the checkpoint payload in
  `jarvis/surface/voice_media.py`, or any assertion in the four tests named below
  beyond what the disposition rule permits.
- Scoped to ONE file. Do not touch bare candidates in any other file. The other
  64 bare candidates elsewhere in the tree are a non-goal.

### Same-file cleanup: three-way disposition, mandatory per test

The hub's AST pass flagged four bare candidates in
`tests/integration/test_wave2_streaming_media.py`. For EACH of the four, do
exactly one of three things and record WHICH ONE and one line of reason in
Progress. A candidate with no recorded disposition is an incomplete run.

The four candidates:
1. `test_structured_lexer_carries_every_split_tag_without_losing_chunk_identity`
   — `tests/integration/test_wave2_streaming_media.py:1623`
2. `test_structured_lexer_handles_adjacent_voice_document_transition` — `:1665`
3. `test_bounded_smoke_uses_independent_gate_and_marks_ab_not_run` — `:2750`
4. `test_segment_closed_before_audible_horizon_still_becomes_heard` — `:2916`

The three dispositions:
- **(a) Rewrite** so the assertion lands on an observable artifact.
- **(b) Confirm covered and delete** — confirm the behavior is already covered by
  a pipeline-level test in the same file, NAME that test, then delete the bare
  one.
- **(c) False positive, leave it** — report that it is a false positive of the
  heuristic and why, and leave the test untouched.

THE HEURISTIC HAS FALSE POSITIVES. `test_bounded_smoke_uses_independent_gate_and_marks_ab_not_run`
is ten lines and may well be a config/shape assertion that is perfectly fine as
it stands. Do not force a rewrite to satisfy a heuristic. Judgment beats
compliance — and say in Progress which of the two you used for each candidate.
(c) is a legitimate, expected outcome, not a failure to do the work.

## Rejected approaches
- Keeping the ledger-level test as an additional "unit-level pin" alongside the
  pipeline test — it is the exact thing the prohibition forbids, and having a
  passing replacement does not make a second copy admissible.
- Reading the ledger's `snapshot()` object out of the pipeline and asserting on
  it — still an in-memory implementation detail, not the artifact the fold
  consumes. The row is the contract.
- Fixing the rule file so the data-driven clause covers this — the owner ruled
  the prohibition wins; `.claude/rules/` is not edited by this card.
- Sweeping the other 64 bare candidates in the same run — unbounded, and each
  file needs its own judgment call.

## Acceptance evidence
Record a pre-change baseline BEFORE any edit.

- Baseline: raw output of
  `PYTHONPATH=. .venv/bin/python -m pytest -q -m "not live_llm and not live_codex"`,
  quoted with its passed/deselected counts, stated as the launch baseline.
- Positive (bug still pinned, fix reverted): with
  `jarvis/surface/voice_ledger.py:324` temporarily reverted to
  `cursor_quality=self._cursor_quality`, raw output of
  `PYTHONPATH=. .venv/bin/python -m pytest -q tests/integration/test_wave2_streaming_media.py -k <new test name>`
  showing it FAIL, with the failing assertion line visible. The new test asserts
  on the emitted `surface.playback_checkpoint` event payload read back from the
  event log — specifically its `heard_text` and `cursor_quality` keys.
- Positive (fix in place): the same command with the revert undone, showing it
  PASS, plus `git status` clean and `git diff --stat` empty for `jarvis/`.
  Same observable artifact: the emitted `surface.playback_checkpoint` payload.
- Regression, named tests, each with the artifact it asserts on:
  - `test_checkpoint_persists_during_later_provider_feed_and_retries` — asserts on
    the emitted `surface.playback_checkpoint` payload and on the assembled
    conversation-history note JSON.
  - `test_structured_chunks_preserve_heard_prefix_on_mid_second_interrupt` —
    asserts on the emitted `surface.playback_interrupted` payload and on the
    assembled conversation-history note JSON.
  - `test_escape_hatch_quality_survives_a_later_audible_report` — currently
    asserts on an in-memory `PlaybackLedger.snapshot()`; it is NOT in scope for
    this card and must keep passing unchanged.
  - Any test given disposition (a) — name it and name the artifact its rewritten
    assertion lands on.
- Regression, full suite: raw output of
  `PYTHONPATH=. .venv/bin/python -m pytest -q -m "not live_llm and not live_codex"`.
  STATE THE ARITHMETIC EXPLICITLY, not a bare number: baseline N, minus the
  deleted `:3004` test, minus any tests deleted under disposition (b), plus the
  one replacement, plus/minus any parametrized-case delta from a disposition (a)
  rewrite, equals the reported total. The net count MAY GO DOWN; that is expected
  and is not a regression as long as the arithmetic accounts for it. Deselected
  count unchanged.
- Gates, each exiting 0 with its printed count:
  `PYTHONPATH=. .venv/bin/lint-imports`, `PYTHONPATH=. .venv/bin/ruff check .`,
  `PYTHONPATH=. .venv/bin/mypy --strict jarvis tests scripts tools`.
  `PYTHONPATH=.` is mandatory: without it the editable install resolves to the
  main checkout and a provenance test fails spuriously against the wrong tree.
- Swift: `desktop/` is not touched, so `scripts/test_inherent_swift.sh` is NOT
  run. Say so explicitly in the transcript.
- Live run: NOT REQUIRED. No audio device is involved; every test here uses
  `_CallbackPump` and a lazy-open player. Do not switch the system default audio
  output.

## Docs to sync
- None. This card changes how an existing fact is verified, not what the fact is:
  no documented contract, invariant, ownership boundary, or externally relevant
  behavior moves. State that judgement in one line in the transcript rather than
  leaving the section silent.
- Do NOT add a sentence to `docs/spec.html` or `docs/adr/` about testing policy.
  That fact belongs to `.claude/rules/`, which this card does not edit.

## Open questions
(none)

## /goal condition
Implement docs/goals/heard-prefix-observable.md on the current branch. Read it
fully before touching code. The goal is met when the transcript shows all of:
(1) BEFORE any edit, raw output of `PYTHONPATH=. .venv/bin/python -m pytest -q -m
"not live_llm and not live_codex"` quoted with its counts, stated as the launch
baseline. (2) A replacement test in
tests/integration/test_wave2_streaming_media.py that constructs a real
`voice_media.StreamingTTSPipeline` over a real `voice_tts.AudioStreamPlayer`,
reuses the existing `_FakeProvider` / `_CallbackPump` / `_config` /
`_emit_response` / `_submit_response` helpers in that file, and asserts on a
`surface.playback_checkpoint` row read back out of the event log — its
`heard_text` non-empty and its `cursor_quality` exactly `"estimated"`. No new
harness is built. The transcript states which helpers were reused. (3) The old
`test_heard_prefix_quality_survives_an_earlier_report_gap` is deleted in the SAME
commit that adds the replacement, shown by that commit's diff; the behavior is
never left uncovered between commits. (4) Proof the replacement still pins the
bug: with `jarvis/surface/voice_ledger.py` line 324 temporarily reverted to
`cursor_quality=self._cursor_quality`, raw pytest output showing the new test
FAIL with the failing assertion visible; then raw output showing it PASS with the
revert undone; then `git status` clean and no diff under `jarvis/`. (5) For EACH
of the four named bare candidates in that file —
`test_structured_lexer_carries_every_split_tag_without_losing_chunk_identity`,
`test_structured_lexer_handles_adjacent_voice_document_transition`,
`test_bounded_smoke_uses_independent_gate_and_marks_ab_not_run`,
`test_segment_closed_before_audible_horizon_still_becomes_heard` — one recorded
disposition with one line of reason in Progress: rewritten to assert on an
observable artifact, or confirmed covered by a NAMED pipeline-level test in the
same file and deleted, or reported as a false positive of the heuristic and left
alone. A false-positive verdict is a legitimate outcome; a rewrite forced only to
satisfy the heuristic is not. No bare candidate in any other file is touched.
(6) Raw output of the full `PYTHONPATH=. .venv/bin/python -m pytest -q -m "not
live_llm and not live_codex"` WITH THE COUNT ARITHMETIC STATED EXPLICITLY —
baseline minus deletions plus additions equals the reported total — because the
net may legitimately go DOWN; the deselected count is unchanged. (7) Each of
`PYTHONPATH=. .venv/bin/lint-imports`, `PYTHONPATH=. .venv/bin/ruff check .`,
`PYTHONPATH=. .venv/bin/mypy --strict jarvis tests scripts tools` exiting 0 with
its printed count, each run with the `PYTHONPATH=.` prefix shown. (8) A statement
that `scripts/test_inherent_swift.sh` was not run because `desktop/` is untouched,
and that no live run is required because no audio device is involved. (9) Every
entry under Docs to sync updated or explicitly judged unchanged, following the
rule: when the implementation changes a documented contract, invariant, ownership
boundary, or externally relevant behavior, update the canonical document that owns
that fact; do not document what the code already makes clear; do not duplicate a
fact across documents. This card is expected to need no doc change — say so
explicitly rather than silently skipping. (10) Each slice committed with the
project commit skill, `git status` clean, one Progress line per slice. If the gap
sequence CANNOT be driven end to end through the pipeline with the existing
machinery, STOP AND REPORT what blocks it and leave the existing test in place —
do not fall back to a ledger-level test and do not build a large new harness. If
the card contradicts the repository, stop and report; do not redesign. Or stop
after 30 turns.

## Progress
- (implementation session appends here)
