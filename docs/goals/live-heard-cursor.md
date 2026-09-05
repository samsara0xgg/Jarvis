# Goal: live-heard-cursor

Lane B — branch `realtime-integration`, parent tip `f914d20`.

## Goal

The playback ledger's per-chunk cursor-quality merge takes the first observed
quality instead of merging it with the `"unknown"` sentinel, so a segment that
closes before the audible horizon reaches it can still become heard, and
`spoken_heard` stops being permanently `None` on live runs.

## Why

`_least_quality` is documented as combining two *already observed* qualities
(`jarvis/surface/voice_ledger.py:330-332`), but the per-chunk call site feeds it
the birth sentinel. Because `_QUALITY_RANK` ranks `unknown` highest
(`:318-322`) and `_least_quality` returns the higher rank, once a chunk is
`"unknown"` it stays `"unknown"` forever. The ledger-level cursor already has
the missing branch (`:256-260`); the per-chunk loop three lines below does not.
The consequence is not cosmetic: no live turn has ever produced a heard prefix,
so the whole downstream heard-state chain is dead on real hardware.

## Current behavior

- A chunk is born with the sentinel `cursor_quality="unknown"`
  (`jarvis/surface/voice_ledger.py:164`, in `PlaybackLedger.begin_segment`).
- `record_audible` merges each closed chunk unconditionally:
  `chunk.cursor_quality = _least_quality(chunk.cursor_quality, cursor_quality)`
  (`:262-264`). With the sentinel on the left this returns `"unknown"` for every
  input, permanently.
- `snapshot()` stops the heard-prefix scan at `chunk.cursor_quality == "unknown"`
  (`:287-293`), so `heard_through_sequence` stays `None` and `heard_text` stays
  `""`.
- `_checkpoint_if_advanced` returns immediately when the sequence is `None`
  (`jarvis/surface/voice_media.py:2784-2786`), so no `surface.playback_checkpoint`
  row is ever appended on a live run.
- `PlaybackHistory.fold` (`jarvis/state/conversation_playback.py:56`) therefore
  never reaches its quality gate (`:212-213`) or its `self.heard = HeardPrefix(...)`
  assignment (`:224-231`).
- `PresentationRecord.spoken_heard` is consequently always `None`
  (`jarvis/state/conversation.py:92`), and the L3 prompt builder always renders
  `"spoken_heard": None` (`jarvis/decision/conversation.py:23-33`, in
  `_response_context`).
- Observed live, not inferred: `docs/goals/crash-recovery-live-verification.md`
  Progress slice 3 and 5, finding 3 — 0 `surface.playback_checkpoint` rows and
  `heard_text=""` on fully played warm-up turns.
- The only writer of a non-sentinel chunk quality today is `finish_segment`'s
  escape hatch (`jarvis/surface/voice_ledger.py:206-210`), which fires only when
  the segment's end is *already* at or below the estimated audible cursor at
  close time. Live, `record_audible` is deferred by `presentation_delay_ns`
  (`jarvis/surface/voice_tts.py:1327-1339`, built from
  `estimated_output_latency_s`, default `0.12` at `:628`, applied at `:691`), and
  network-paced generation closes a segment long before playback reaches it — so
  the hatch never fires on real hardware.
- Sole caller of `record_audible`: `jarvis/surface/voice_tts.py:1335-1338`,
  hardcoded `cursor_quality="estimated"`. Sole caller of `finish_segment`:
  `jarvis/surface/voice_tts.py:1189`. No test calls either directly.

## Target behavior

- The per-chunk merge in `record_audible` distinguishes "never observed" from
  "observed as `unknown`" explicitly, mirroring the ledger-level
  `_cursor_quality_observed` idiom at `:256-260`. First observation assigns;
  every later observation merges through `_least_quality` unchanged.
- The distinction is carried by a real observation flag, not by testing whether
  the current value happens to be `"unknown"`.
- A segment closed *before* the audible horizon reaches its end, then covered by
  a later `record_audible(..., cursor_quality="estimated")`, ends with
  `cursor_quality == "estimated"`; `snapshot()` then advances
  `heard_through_sequence` and returns a non-empty `heard_text`.
- A segment whose quality is genuinely observed as `"unknown"` (the
  `record_submitted` gap path at `:225-234`, which sets the ledger cursor to
  `"unknown"` and marks it observed) still degrades every later merge to
  `"unknown"`. Conservatism is not weakened anywhere.
- `finish_segment`'s escape hatch stays and stays *reachable*: it covers the
  opposite ordering, where the horizon crosses while the chunk is still open and
  `output_end_cursor is None`, which the `record_audible` loop skips by its own
  `output_end_cursor is not None` guard (`:263`). The two existing tests named
  under Acceptance are that path's pin; the new hermetic test is the fixed
  path's pin.
- On a real daemon turn, `surface.playback_checkpoint` rows carry a non-null
  `heard_through_sequence` and a non-empty `heard_text`, and a following turn's
  L3 prompt context renders `spoken_heard` non-`None`.

## Affected contracts and files

- L5 `jarvis/surface/voice_ledger.py:262-264` (`PlaybackLedger.record_audible`) —
  the per-chunk merge gains the explicit first-observation branch. This is the
  whole production change.
- L5 `jarvis/surface/voice_ledger.py:69-85` (`SpeechChunk`) — may gain an
  observation flag *only* if that does not change a shape crossing the module
  boundary. Verified fact for that judgement: `SpeechChunk` is exported in
  `__all__` (`:343`) but no module outside `jarvis/surface/voice_ledger.py`
  imports it (repo-wide grep for `SpeechChunk` hits only that file). An internal
  map keyed by `segment_sequence` on `PlaybackLedger` is the alternative and is
  unambiguously inside the boundary. No payload, no `OutputTimelineSnapshot`
  field, and no event schema changes either way.
- Tests `tests/integration/test_wave2_streaming_media.py` — one new hermetic test
  reproducing the live ordering. `tests/unit` is retired
  (`.claude/rules/python-testing.md`), so this is an integration test, and this
  file is the only place in the suite that exercises the ledger.
- Verify-only, no edits expected: `jarvis/surface/voice_media.py:2784-2818`
  (checkpoint emission), `jarvis/state/conversation_playback.py:212-231`,
  `jarvis/state/conversation.py:92`, `jarvis/decision/conversation.py:23-33`.

## Boundaries and non-goals

- Layers that may change: L5 only (`jarvis/surface/voice_ledger.py`), plus tests
  and docs.
- Must not change: the ledger-level `_cursor_quality` logic (`:256-261`);
  `record_submitted` (`:212-242`); the `snapshot()` gate
  `chunk.cursor_quality == "unknown"` (`:291`); `_least_quality` and
  `_QUALITY_RANK` (`:318-332`); `finish_segment`'s escape hatch (`:206-210`),
  which stays; the two existing tests named under Acceptance, which must pass
  unedited; `tests/integration/test_wave2_streaming_media.py:819-820`, which
  asserts an *empty* heard prefix for a mid-segment interrupt — that is correct
  behavior, not the bug.
- No new config key and no feature flag. This is a defect fix restoring
  documented behavior, not a new surface.
- Non-goals: changing crash-recovery behavior; producing `measured_dac` (no code
  path produces it and none should start); broadening the `CursorQuality`
  vocabulary; any change to `voice_media` beyond what the fix strictly requires;
  removing the escape hatch (a separate concern).
- Downstream is verify-only, not build: show that the crash-recovery heard-prefix
  branch is now reachable; do not modify it.
- If the lane finds an ADR sentence the fix contradicts, it stops and reports
  rather than editing the ADR.

## Rejected approaches

- Fix at the `snapshot()` gate (`:291`) — treats the symptom. The gate correctly
  refuses to call an unmeasured chunk heard; the wrong value it reads is
  produced upstream by the merge. Every other reader of
  `chunk.cursor_quality` would still see the poisoned value.
- Loosen `_least_quality` — its contract is "the less certain of two already
  observed qualities" (`:331`), which is correct and is relied on by
  `record_submitted`'s gap path and by the ledger-level cursor. The bug is the
  call site violating the precondition, not the function.
- Special-case the caller (`jarvis/surface/voice_tts.py:1335-1338`) — pushes L5
  ledger accounting into the player and leaves the ledger still wrong for every
  other caller.
- Infer "first observation" from the value being `"unknown"` — reintroduces
  exactly the sentinel-versus-observation ambiguity this card exists to remove.
  It happens to work only because the sole caller passes `"estimated"` today; it
  would silently upgrade a genuinely observed `"unknown"` into whatever arrives
  next.

## Acceptance evidence

`PYTHONPATH=.` is mandatory on every command below. Without it the editable
install resolves to the main checkout and a provenance test fails spuriously.

- New hermetic test, before: at the parent commit (fix reverted or stashed), the
  raw output of the new test alone ends in a fail line, and the failing
  assertion is visible in the output (`heard_through_sequence` is `None` /
  `heard_text` is `""`). The reproduction must use the live ordering:
  `finish_segment(n)` runs *before* the audible horizon reaches segment n's end
  — i.e. a non-zero presentation delay, unlike the existing fixture's
  `estimated_output_latency_s=0.0` (`tests/integration/test_wave2_streaming_media.py:237-244`).
- New hermetic test, after: the raw output of the same test with the fix in place
  ends in a pass line, asserting `heard_through_sequence` advanced and
  `heard_text` is non-empty. Both raw outputs must appear in the transcript.
- Positive suite: `PYTHONPATH=. .venv/bin/python -m pytest -q -m "not live_llm and not live_codex"`
  prints the branch baseline at launch plus the new tests, and `64 deselected`.
  The pre-change count is recorded by running the suite BEFORE the first edit
  (it is 990 passed / 64 deselected on f914d20, but a sibling card may merge
  first); the delta is stated and is exactly the new tests.
- Regression, unchanged tests: `tests/integration/test_wave2_streaming_media.py`
  `test_checkpoint_persists_during_later_provider_feed_and_retries` (`:1158`,
  asserts at `:1222-1223` and `:1237-1238`) and
  `test_structured_chunks_preserve_heard_prefix_on_mid_second_interrupt`
  (`:1515`, asserts at `:1599`) still pass with their bodies unedited; `git diff`
  shows no edit inside either. These pass today only because their
  zero-output-latency, tiny-ring fixture (`:200-218`, `:237-244`) lets the
  horizon overtake the segment end before `finish_segment`, hitting the escape
  hatch instead of the broken merge — they are the escape hatch's pin.
- Gates: `PYTHONPATH=. .venv/bin/lint-imports`,
  `PYTHONPATH=. .venv/bin/ruff check .`, and
  `PYTHONPATH=. .venv/bin/mypy --strict jarvis tests scripts tools` each shown raw
  with their printed counts and exit 0.
- Swift: `bash scripts/test_inherent_swift.sh` (baseline 174) is required only if
  anything under `desktop/` is touched. No `desktop/` change is expected here; if
  the diff has none, state that instead of running it.
- Live run: **required** — see below.

### Live run

A real `python -m jarvis serve` daemon turn on a runtime root owned by this lane.
Do not signal, stop, or reuse the long-running daemon on port 8006 with root
`~/.jarvis-realtime-test`; state explicitly that it was left untouched. A live
scenario test under `tests/scenarios/` needs both the `live_llm` marker and the
`--live-llm` flag or `tests/conftest.py` skips the item.

Canary values that must be quoted verbatim in the transcript:

- The raw `surface.playback_checkpoint` payload row from the run's event
  database, showing a non-null `heard_through_sequence`, a non-empty
  `heard_text`, and `cursor_quality` of `"estimated"` (fields built at
  `jarvis/surface/voice_media.py:2810-2819`).
- The `provider` value on that response's terminal row
  (`jarvis/surface/voice_media.py:2959`) is `minimax_ws_streaming`, **not**
  `macos_say`. The `macos_say` fallback (`jarvis/surface/voice_media.py:2457`)
  bypasses the ledger entirely, so a run that falls back proves nothing. A known
  cause of that fallback is a quoted `MINIMAX_API_KEY` in the runtime root's
  `env` file (`docs/goals/crash-recovery-live-verification.md` Progress slice 2,
  follow-up (a)).
- A following turn in the same session whose L3 prompt context renders
  `spoken_heard` non-`None`: quote the `spoken_heard.text` and its
  `cursor_quality`, and show that the text equals the `heard_text` of the
  checkpoint row above.
- The heard prefix reaching L3 is the evidence that the crash-recovery
  heard-prefix branch is now reachable (verify only; change nothing there).

Audio rule, applies to this run verbatim: if the run switches the SYSTEM default
output device, capture the pre-run route first, restore it in a finally/trap on
every exit path, and if the captured route is ALREADY the loopback
("BlackHole 16ch") restore "MacBook Pro Speakers" instead; skip the switch
entirely when the run needs no audio. Two overlapping lanes once left the owner
with no speaker output. Quote the captured pre-run route and the post-run route
in the transcript.

## Docs to sync

Judge each entry explicitly; none is expected to need an edit.

- `docs/adr/0006-full-duplex-voice-session.md:347` (`cursor_quality` is
  `measured_dac`, `estimated`, or `unknown`, all boundary calculations round
  backward) and `:349` (V1 advances heard state only through a complete segment
  whose final post-gain frames crossed the conservative audible horizon with
  `audibility_class=normal`) — the fix restores this behavior; expected
  unchanged. No ADR sentence dictates the extra `!= "unknown"` gate at
  `voice_ledger.py:291`; that is an implementation decision.
- `docs/adr/0008-real-time-response-streaming.md:926` §4.4 — "`spoken_heard`
  advances only from ADR-0006 playback checkpoints" becomes true in practice
  again; expected unchanged.
- `docs/spec.html` — verified to contain no occurrence of "heard" (case
  insensitive) at `f914d20`; expected unchanged.
- `docs/live-burn-2026-09-05-crash-recovery.md` finding 3 and
  `docs/goals/crash-recovery-live-verification.md` Progress slice 3 and 5 —
  these record the defect as open. They are historical run records; state the
  disposition chosen (a pointer to this card's evidence, or left as history) and
  do not restate the mechanism in two documents.
- If this lane writes its own live-burn document, it owns the run trail; the
  mechanism stays in the code and in this card, not duplicated there.

## Open questions

(none)

## /goal condition

The goal is met when all of the following appear in the transcript.
(1) `git diff --stat` is shown; the only production file changed is
`jarvis/surface/voice_ledger.py`; the diff of that file is shown and adds an
explicit first-observation branch to the per-chunk merge in `record_audible`,
using a real observation flag and not a test for the value `"unknown"`, while
`_least_quality`, `_QUALITY_RANK`, the `snapshot()` `cursor_quality == "unknown"`
gate, `record_submitted`, the ledger-level `_cursor_quality` logic, and
`finish_segment`'s escape hatch are unedited, and no caller in `voice_tts.py` or
`voice_media.py` is special-cased; no new config key or feature flag appears.
(2) The card's statement that `finish_segment`'s escape hatch stays reachable is
restated with the test that pins it named.
(3) The new hermetic test is named and its raw pytest output appears TWICE: once
at the parent commit (fix reverted or stashed) ending in a fail line with the
failing assertion visible, and once with the fix ending in a pass line. Both
outputs are raw, not summarized.
(4) Raw output of
`PYTHONPATH=. .venv/bin/python -m pytest -q -m "not live_llm and not live_codex"`
is shown, its passed count equals the pre-change baseline recorded earlier in
the transcript plus the new tests with the delta stated, and deselected is 64.
(5) `tests/integration/test_wave2_streaming_media.py`
`test_checkpoint_persists_during_later_provider_feed_and_retries` and
`test_structured_chunks_preserve_heard_prefix_on_mid_second_interrupt` still
pass and `git diff` shows no edit inside either body; the empty-heard-prefix
assertion for the mid-segment interrupt is untouched.
(6) Raw output with printed counts and exit 0 for
`PYTHONPATH=. .venv/bin/lint-imports`, `PYTHONPATH=. .venv/bin/ruff check .`, and
`PYTHONPATH=. .venv/bin/mypy --strict jarvis tests scripts tools`. `PYTHONPATH=.`
is present on every command.
(7) If the diff touches `desktop/`, raw `bash scripts/test_inherent_swift.sh`
output showing 174; otherwise an explicit statement that no `desktop/` file
changed.
(8) A real daemon turn was run and the transcript quotes: the raw
`surface.playback_checkpoint` payload row with a non-null
`heard_through_sequence`, a non-empty `heard_text`, and `cursor_quality`
`"estimated"`; the response terminal's `provider` value showing
`minimax_ws_streaming` and not `macos_say`; and a following turn's L3 prompt
context with `spoken_heard` non-`None`, its `text` quoted and equal to that
`heard_text`. It is stated that the crash-recovery heard-prefix branch is
therefore reachable, and that nothing downstream was changed.
(9) The live run's audio handling is reported: the captured pre-run default
output route and the post-run route are quoted and match, restoration ran on
every exit path, and if the captured route was already `BlackHole 16ch` then
`MacBook Pro Speakers` was restored instead; or an explicit statement that the
run needed no audio switch. It is stated that the port-8006 daemon with root
`~/.jarvis-realtime-test` was never signalled or stopped.
(10) Each entry under Docs to sync is updated or explicitly judged unchanged,
following the rule: update the canonical document that owns a changed contract,
invariant, ownership boundary, or externally relevant behavior; do not document
what the code already makes clear; do not duplicate a fact across documents. If
an ADR sentence is found that the fix contradicts, the run stops and reports it
instead of editing the ADR.
(11) Each slice is committed with the project commit skill, `git status` is
shown clean, and a Progress line per slice is appended to the card.
Or stop after 30 turns.

## Progress

- (none yet)
