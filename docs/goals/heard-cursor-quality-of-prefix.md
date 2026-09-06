# Goal: heard-cursor-quality-of-prefix

## Goal

`OutputTimelineSnapshot.cursor_quality` reports the quality of the heard prefix
the same snapshot is reporting, instead of a lease-lifetime ledger watermark, so
a heard prefix that is genuinely `estimated` survives an earlier callback-report
gap and reaches the fold.

## Why

One record reports two different things. `heard_text` /
`heard_through_sequence` describe a per-chunk PREFIX; `cursor_quality` describes
a LEASE-LIFETIME watermark. The fold consumes `cursor_quality` as the quality OF
that heard evidence (`jarvis/state/conversation_playback.py:212-213`), so
reporting a lease-wide watermark in that field is a category error. The damage
is not one row: after ANY callback-report gap the ledger value is pinned to
`"unknown"` for the remainder of the lease, so every later checkpoint on that
lease carries heard text the fold must throw away.

Supporting contract: `docs/adr/0006-full-duplex-voice-session.md:349` states that
heard state advances per COMPLETE SEGMENT crossing the horizon with
`audibility_class=normal` — a per-segment notion, so its quality is per-segment
too.

## Current behavior

Reachable sequence that produces the residue (all `path:line` re-pinned at
`realtime-integration` `cecff8f`):

1. A chunk closes in `finish_segment`
   (`jarvis/surface/voice_ledger.py:192-211`) while
   `chunk.output_end_cursor > self._estimated_audible_cursor`, so the escape
   hatch (`jarvis/surface/voice_ledger.py:207-211`) does NOT fire. The chunk
   keeps the birth sentinel `cursor_quality="unknown"`
   (`jarvis/surface/voice_ledger.py:165`) and `cursor_quality_observed=False`
   (`jarvis/surface/voice_ledger.py:86`).
2. `record_submitted` hits a callback-report gap
   (`jarvis/surface/voice_ledger.py:226-235`). It sets ledger
   `self._cursor_quality = "unknown"` and `self._cursor_quality_observed = True`
   (`jarvis/surface/voice_ledger.py:227-228`), and poisons `audibility_class`
   ONLY on chunks whose end is `None` or beyond `self._submitted_cursor`
   (`jarvis/surface/voice_ledger.py:229-234`). The chunk from step 1 is behind
   that cursor, so it keeps `audibility_class = "normal"`.
3. A later `record_audible(cursor_quality="estimated")`
   (`jarvis/surface/voice_ledger.py:245-274`) advances `bounded` past that
   chunk. Because `chunk.cursor_quality_observed` is False, the chunk takes the
   first-observation OVERWRITE branch
   (`jarvis/surface/voice_ledger.py:269-273`) and becomes `"estimated"`. The
   LEDGER value takes the `_least_quality` branch
   (`jarvis/surface/voice_ledger.py:257-262`) because `_cursor_quality_observed`
   is True, and `_least_quality("unknown", "estimated")` is `"unknown"`
   (`jarvis/surface/voice_ledger.py:340-342`, ranks at
   `jarvis/surface/voice_ledger.py:328-332`) — pinned there for the rest of the
   lease.
4. `snapshot()` (`jarvis/surface/voice_ledger.py:285-319`) admits that chunk into
   `heard_parts` — its gate is per-CHUNK
   (`jarvis/surface/voice_ledger.py:297-303`) — but reports the row's
   `cursor_quality` from the LEDGER value
   (`jarvis/surface/voice_ledger.py:315`).
5. `_checkpoint_if_advanced` writes the row with `heard_text` non-empty
   (`jarvis/surface/voice_media.py:2817`) and `cursor_quality: "unknown"`
   (`jarvis/surface/voice_media.py:2818`).
6. The fold rejects it — `jarvis/state/conversation_playback.py:212-213` accepts
   only `{"estimated", "measured_dac"}` — so `spoken_heard` is not polluted, and
   the proven heard prefix is silently discarded.

## Target behavior

- `snapshot()` computes the reported `cursor_quality` as `_least_quality` folded
  over the chunks actually appended to `heard_parts`
  (`jarvis/surface/voice_ledger.py:304`), i.e. the quality of the prefix it is
  reporting.
- Driven through the step 1-4 sequence above, the snapshot reports
  `cursor_quality == "estimated"` with a non-empty `heard_text`, and the
  `_checkpoint_if_advanced` row it produces passes the fold's gate at
  `jarvis/state/conversation_playback.py:212-213`.
- Empty-prefix case: when no chunk is admitted (`heard_parts` empty), the
  snapshot reports `self._cursor_quality` exactly as today. This is deliberate —
  today an empty prefix with a good ledger quality passes the fold's gate and
  reaches the `HeardPrefix` assignment
  (`jarvis/state/conversation_playback.py:224-231`); substituting `"unknown"`
  there would change behavior for rows this residue is not about.
- The ONLY rows whose reported quality changes are rows that carry non-empty
  `heard_text`.
- The birth sentinel is never fed to `_least_quality`: every admitted chunk
  already passed the `!= "unknown"` gate at
  `jarvis/surface/voice_ledger.py:301`, so the invariant its comment depends on
  (`jarvis/surface/voice_ledger.py:265-268`) still holds.
- `heard_text` and `heard_through_sequence` selection is unchanged; no new
  `CursorQuality` value is introduced; the vocabulary
  (`measured_dac | estimated | unknown`) stays exactly as
  `docs/adr/0006-full-duplex-voice-session.md:347` fixes it.

## Affected contracts and files

- L5 `jarvis/surface/voice_ledger.py:snapshot` (`:285-319`) — the only
  production change: the reported `cursor_quality` is derived from the admitted
  chunks instead of `self._cursor_quality`.
- L5 `jarvis/surface/voice_media.py:2655` (`tts_estimated_audible` trace),
  `:2818` (`surface.playback_checkpoint` payload), `:2960`
  (`_commit_terminal` payload) — the three consumers of the field. They inherit
  the new semantics and are CHECKED, NOT CHANGED.
- L2 `jarvis/state/conversation_playback.py:212-213` — the fold's gate. Stays
  exactly as it is; this card makes the gate meaningful, it does not move it.
- `tests/integration/test_wave2_streaming_media.py` — one new integration test.

## Boundaries and non-goals

- Layers that may change: L5 only. Production change is confined to
  `jarvis/surface/voice_ledger.py:snapshot()`, plus tests.
- MUST NOT CHANGE: `finish_segment` (`:192-211`); `record_submitted`
  (`:213-243`); `record_audible` (`:245-274`); the callback-gap branch
  (`:226-235`); the per-chunk `cursor_quality_observed` flag (`:86`, written at
  `:274`); the fold (`jarvis/state/conversation_playback.py:212-213`).
  `_least_quality` and `_QUALITY_RANK` (`:328-342`) and the `snapshot()`
  per-chunk gate (`:297-303`) are also unedited.
- `finish_segment`'s escape hatch (`:207-211`) is NOT touched by this card, and
  the `docs/goals/live-heard-cursor.md` boundary "`finish_segment`'s escape
  hatch (`:206-210`), which stays" is NOT relaxed. The correct fix does not go
  near it.
- No new config key and no feature flag.
- Non-goals:
  - Making the escape hatch's own assignment survive a later `record_audible`
    (i.e. setting `cursor_quality_observed = True` inside the hatch). That is a
    separate, legitimate question about hatch observation; it is NOT this
    residue and is explicitly out of scope here.
  - Producing `measured_dac`, or broadening the `CursorQuality` vocabulary.
  - Any change to `voice_media.py` or to crash-recovery behavior.
  - Amending `docs/adr/0006-full-duplex-voice-session.md`.
- Test authority, BOUNDED. Four existing tests in
  `tests/integration/test_wave2_streaming_media.py` should still pass:
  `test_checkpoint_persists_during_later_provider_feed_and_retries` (`:1163`),
  `test_structured_chunks_preserve_heard_prefix_on_mid_second_interrupt`
  (`:1520`), `test_segment_closed_before_audible_horizon_still_becomes_heard`
  (`:2916`), `test_escape_hatch_quality_survives_a_later_audible_report`
  (`:2965`). If one of them fails, the lane MAY edit it ONLY when the failure is
  a `cursor_quality` expectation on a row whose `heard_text` is non-empty, and
  MUST quote the before/after assertion in Progress. Any other failure means
  this card's analysis is wrong: STOP AND REPORT, do not redesign in-lane.

## Rejected approaches

- Set `cursor_quality_observed = True` inside `finish_segment`'s escape hatch
  (`jarvis/surface/voice_ledger.py:207-211`) — the handed-down "one-line close".
  In the sequence above the hatch NEVER FIRES: step 1 closes the chunk with
  `chunk.output_end_cursor > self._estimated_audible_cursor`, so the branch
  guard at `:209` is false and no line inside it executes. Setting a flag there
  changes nothing about this residue. It would change a different thing (whether
  the hatch's own assignment survives a later `record_audible`), which is a
  separate question and a non-goal above.
- Poison the per-chunk quality in `record_submitted`'s gap branch so the ledger
  and the chunks agree — touches the gap branch (forbidden above) and would
  empty the heard prefix for chunks already proven audible behind
  `_submitted_cursor`, i.e. it fixes the mismatch by destroying the good side.
- Widen the fold's accepted set at
  `jarvis/state/conversation_playback.py:212-213` to admit `"unknown"` — moves
  the gate instead of making it meaningful, is L2, and would let genuinely
  unknown-quality prefixes into `spoken_heard`.

## Acceptance evidence

- Positive: ONE new integration test in `tests/integration/` drives the real
  `PlaybackLedger` through the step 1-4 sequence above and asserts the snapshot
  reports `cursor_quality == "estimated"` with a non-empty `heard_text`;
  `PYTHONPATH=. .venv/bin/python -m pytest -q <path>::<name>` prints
  `1 passed`, and the same command at the parent commit (fix reverted) prints
  a fail line with the failing assertion visible. One test only — no test per
  quality value, per branch, or per consumer.
- Regression: `PYTHONPATH=. .venv/bin/python -m pytest -q -m "not live_llm and
  not live_codex"` still prints `1066 passed, 64 deselected` (baseline was
  `1065 passed, 64 deselected`; the delta is the one new test).
- Live run: not required — the whole sequence is fully driveable against the
  in-process ledger, so a live daemon adds no evidence this card needs.

## Docs to sync

- none — `docs/adr/0006-full-duplex-voice-session.md:347` fixes only the
  `measured_dac | estimated | unknown` vocabulary, which is unchanged, and
  `:349`'s per-segment heard-state statement is what this card makes TRUE rather
  than what it changes; `docs/spec.html` carries no "heard" contract to sync
  (established by `docs/goals/live-heard-cursor.md`).

## Open questions

(none)

## /goal condition

The goal is met when all of the following appear in the transcript.
(1) `git diff --stat` is shown; the only production file changed is
`jarvis/surface/voice_ledger.py`. Its full diff is shown and touches only
`snapshot()`: `finish_segment` and its escape hatch, `record_submitted` and its
callback-gap branch, `record_audible`, the per-chunk `cursor_quality_observed`
flag, `_least_quality`, `_QUALITY_RANK`, and the `snapshot()` per-chunk
admission gate are all unedited, and no new config key or feature flag appears.
(2) The shown diff computes the reported `cursor_quality` by folding
`_least_quality` over the chunks actually appended to `heard_parts`, and it is
visible that when `heard_parts` is empty the snapshot still reports
`self._cursor_quality` unchanged.
(3) `git diff` shows `jarvis/state/conversation_playback.py` and
`jarvis/surface/voice_media.py` unchanged, and the transcript names
`voice_media.py:2655`, `:2818`, `:2960` as consumers checked and not changed,
and states the fold's accepted set stays `{"estimated","measured_dac"}`.
(4) Exactly ONE new test file entry or test function is added, its name is
given, and its raw pytest output appears TWICE: once at the parent commit with
the fix reverted or stashed, ending in a fail line with the failing assertion
visible, and once with the fix, ending in `1 passed`. Both outputs are raw, not
summarized. No second test is added for another quality value, branch, or
consumer.
(5) Raw output of `PYTHONPATH=. .venv/bin/python -m pytest -q -m "not live_llm
and not live_codex"` is shown and its final line reads `1066 passed, 64
deselected` (the baseline `1065 passed, 64 deselected` plus the one new test).
The counts are quoted from the raw output, never inferred. No live run is
claimed as evidence; the transcript states a live run is not required.
(6) `test_checkpoint_persists_during_later_provider_feed_and_retries`,
`test_structured_chunks_preserve_heard_prefix_on_mid_second_interrupt`,
`test_segment_closed_before_audible_horizon_still_becomes_heard`, and
`test_escape_hatch_quality_survives_a_later_audible_report` in
`tests/integration/test_wave2_streaming_media.py` pass. If any was edited, the
transcript quotes the before and after assertion and shows that the failure was
a `cursor_quality` expectation on a row whose `heard_text` is non-empty; for any
other failure the run stopped and reported instead of redesigning.
(7) Raw output with printed counts and exit 0 for `PYTHONPATH=.
.venv/bin/lint-imports`, `PYTHONPATH=. .venv/bin/ruff check .`, and
`PYTHONPATH=. .venv/bin/mypy --strict jarvis tests scripts tools`. `PYTHONPATH=.`
is present on every command.
(8) Each entry under Docs to sync is updated or explicitly judged unchanged,
following the rule: update the canonical document that owns a changed contract,
invariant, ownership boundary, or externally relevant behavior; do not document
what the code already makes clear; do not duplicate a fact across documents. If
an ADR sentence is found that the change contradicts, the run stops and reports
it instead of editing the ADR.
(9) The change is committed with the project commit skill, raw `git status` is
shown and the tree is clean, and a Progress line with the commit sha and its
evidence line is appended to this card.
Or stop after 20 turns.

## Progress

- `snapshot()` reports the admitted prefix's quality — 3260f6d — new test
  `test_heard_prefix_quality_survives_an_earlier_report_gap` fails
  `assert 'unknown' == 'estimated'` reverted and prints `1 passed` with the fix;
  full hermetic run `1066 passed, 64 deselected` (baseline 1065 + 1);
  lint-imports `1 kept, 0 broken`, ruff `All checks passed!`, mypy strict
  `243 source files`; the four named regression tests pass, one of them edited
  under the card's bounded test authority:
  `test_escape_hatch_quality_survives_a_later_audible_report` moved
  `assert snapshot.cursor_quality == "unknown"` to
  `assert snapshot.cursor_quality == "estimated"` — a `cursor_quality`
  expectation on a row whose `heard_text` (`"已经听到的部分"`) is non-empty and
  is still asserted. No live run: the whole sequence is driveable in-process,
  as the card states.
- Verifier pass (fresh context, opus, `realtime-integration..HEAD`): no
  production defect. Two observations recorded here rather than fixed, because
  fixing either is outside this card:
  - The fold's gate is now structurally satisfied for every checkpoint row
    carrying a non-empty prefix: `jarvis/surface/voice_media.py:2785-2786`
    writes a checkpoint only when `heard_through_sequence` is not None, which
    implies at least one chunk passed the `!= "unknown"` gate at
    `jarvis/surface/voice_ledger.py:302`. `conversation_playback.py:212-213`
    still constrains empty-prefix rows and historical log rows (whose fold
    behavior is unchanged), but it is no longer an independent fail-closed
    check for non-empty prefixes. If anyone later relaxes the L5 gate, L2 is
    no longer the backstop.
  - The `heard_parts`-empty fallback branch carries no test of its own. The
    card caps this work at one new test, so the gap is deliberate.
