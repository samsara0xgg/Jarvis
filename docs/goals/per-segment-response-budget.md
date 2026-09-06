# Goal: per-segment-response-budget

## Goal
`response_timeout_s` bounds one speech segment's provider I/O plus its playback
on the non-live path as it already claims to on the live one, so a
multi-segment answer is no longer truncated mid-sentence at exactly the budget
with no operator-visible trace.

## Why
Owner's symptom, in his words: "语音读到...就停了" / "有时就没声音了" — speech
stops partway through while the full text renders DONE on screen.

Measured against his real event ledger: two answers died at **45.000 s** and
**45.001 s** after `surface.playback_started` against `response_timeout_s = 45.0`,
both terminal `surface.playback_failed` with `reason: tts_response_timeout`,
both with **47.26 s / 47.30 s** of audio prepared. Across all 15 playbacks in
that ledger: 9 completed, longest 41.30 s elapsed, all under 45; 2 failed, both
over 47 s of audio, both terminated at exactly 45.00 s. No completed playback
exceeded 45 s and no playback over 45 s ever completed.

Two competing causes were ruled out with numbers, not narrative:

- **Not ring starvation.** `total_samples - submitted_samples` was 131072, which
  is *exactly* the ring capacity — `_GenerationRingBuffer.__init__`
  (`jarvis/surface/voice_tts.py:228-234`) rounds `size_samples` up to a power of
  two, and `ring_seconds=2.0` x 48000 Hz (`voice_tts.py:620`, `:636-638`) rounds
  96000 up to 2^17 = 131072. The ring was completely **full**, the opposite of
  starved.
- **Not a provider failure.** A provider transport error is swallowed per
  endpoint at `voice_media.py:2386-2400` and, if it escapes, logs a warning and
  yields `tts_provider_error:*` at `voice_media.py:2187-2193`; both terminals
  said `tts_response_timeout`, reachable only from `except TimeoutError` at
  `voice_media.py:2185-2186`.

The defect is a seam, not a number. The `asyncio.timeout` at
`voice_media.py:2165` wraps the entire `_stream_with_prefix_fallback` **plus**
`_drain_and_complete`, and the budget object is handed to the callee only on the
live path (`:2169` `budget=budget if live else None`). The callee re-derives
`live = budget is not None` (`:2249`), so with `budget=None` the two
`budget.reschedule(...)` calls (`:2260-2265`, `:2416-2420`) never execute. The
deadline is then anchored once at `playback_started` and bounds **wall clock**.
Playback is real time and `_write_all` (`:2470-2495`) is backpressured by the
2 s ring — `write_generation` returns `None` when the ring is full
(`voice_tts.py:1161-1162`, documented at `:1132-1136` as "never a drop") — so
wall clock is approximately audio duration. Any answer with more than about
44.5 s of speech is cut off mid-sentence, silently.

This contradicts the code's own documented intent. The comment at
`voice_media.py:2261-2262` states the bound "covers one segment's wait plus its
provider I/O, never the whole generation of a still-streaming response". That is
what makes this a defect rather than a design choice. The prior card
(`docs/goals/incremental-tts-from-permitted-segments.md:43`) scoped the
requirement to the streaming case only — "its scope ends at
`surface.response_emitted`" — so the non-live path was never considered.

**In production the reschedule is dead code today.** `live` requires
`response.incremental` (`voice_media.py:2129`), which requires
`speak_from_segments` (`:1889-1891`), and `config/jarvis.yaml:265` ships
`speak_from_segments: false`; the owner's live overlay
(`~/.jarvis-realtime-test/overlay/config/jarvis.yaml`) has no
`speak_from_segments` key at all, so it takes the same default. Every playback
he has ever heard ran with `budget=None`.

## Current behavior
Every line below was re-verified in the `realtime-integration` worktree at
`5759479`. Line numbers in this file drift; re-check before editing.

- `jarvis/surface/voice_media.py:2165` — `async with
  asyncio.timeout(self._config.response_timeout_s) as budget:` opens at
  `playback_started` and closes after `_drain_and_complete` (`:2182`). Both the
  segment loop and the final drain are inside it.
- `:2169` — `budget=budget if live else None`, where `live` at `:2129` is
  `response.incremental and not response.emitted`.
- `:2239` — the callee's parameter is `budget: asyncio.Timeout | None = None`,
  and `:2249` re-derives `live = budget is not None`. One caller only:
  `grep -rn "_stream_with_prefix_fallback" jarvis tests` returns exactly the
  definition (`:2234`) and the single call site (`:2166`).
- The three (in fact **four**) other `live` uses in the callee mean "the segment
  list may still grow", a different fact from "a budget exists":
  - `:2252-2255` — `if segment_index >= len(segments) and not (live and await
    self._await_segments(active, segments)): break`. Gates growing the list.
  - `:2256-2258` — `if live and active.response.tagged:` →
    `_fail_active(stream_chunk_tagged)`. Gates the mid-stream tag abort.
  - `:2413-2420` — `while live and await self._await_segments(...)` in the
    macOS-`say` fallback branch, draining the rest of a still-growing buffer
    before handing off, with the second `budget.reschedule` inside it.
  - `:2421-2427` — the same tagged abort in that branch.
- `:2260-2265` — the reschedule, guarded by `if budget is not None:`, with the
  intent comment at `:2261-2262`.
- `:2185-2186` — `except TimeoutError: await self._fail_active(active,
  reason="tts_response_timeout", retryable=True)`. `_fail_active`
  (`:2735-2766`) emits the ledger event and **makes no `LOGGER` call**, so an
  operator watching the daemon log sees a completely silent truncation. Every
  other terminal path in this function logs (`:2188` for provider errors).
- Segments are one per response chunk regardless of liveness:
  `_ResponseBuffer.speech_segments` (`:328-350`) walks `self.chunks`, and
  `append_voice_suffix` (`:312-326`) adds the emitted-time suffix as one more.
  `tests/integration/test_incremental_tts.py:566` pins the non-live shape:
  with `speak_from_segments=False` a stream response prepares
  `[_SEGMENTS[0], _SUFFIX]` — two segments. A live daemon run recorded in
  `docs/goals/speak-ordinary-answers.md` Progress shows four
  `surface.playback_segment_prepared` rows for one ordinary answer. So a real
  answer is multi-segment on the non-live path, which is what makes a per-segment
  bound meaningful there.
- Terminal payload fields exist and are what the acceptance asserts on:
  `_commit_terminal` (`:2988-3016`) writes `heard_through_sequence` (`:3003`),
  `submitted_samples` (`:3004`), `total_samples` = `snapshot.accepted_samples`
  (`:3005`), `provider` (`:3006`, default `"minimax_ws_streaming"` at `:368`,
  set to `"macos_say"` at `:2504`), and `reason` only on the non-completed
  branch (`:3014`).
- `_MAX_RESPONSE_TIMEOUT_S = 300.0` exists at `:63`; `response_timeout_s`
  defaults to 45.0 at `:235` and is validated at `:276`.

## Target behavior
- `_stream_with_prefix_fallback` receives the budget **unconditionally**, so the
  per-segment reschedule runs on every path. The parameter is no longer
  optional.
- Liveness is passed **explicitly** as its own parameter and is never inferred
  from the presence of a budget. `live = budget is not None` disappears. The
  four sites above keep their current meaning ("the segment list may still
  grow"), driven by the caller's `live` from `:2129`.
- After the change the effective bound is: **the start of the current segment,
  plus `response_timeout_s`, covering that segment's provider I/O, its
  backpressured playback, and — for the final segment — `_drain_and_complete`.**
  Total generation length is unbounded. State this bound in the code comment;
  it is the contract this card creates.
- The budget still bites: a single segment that stalls past `response_timeout_s`
  still fails the run with `tts_response_timeout`. Nothing about the number,
  the config key, or `_MAX_RESPONSE_TIMEOUT_S` changes.
- One `LOGGER.warning` at the `except TimeoutError` seam naming the response id
  and the expired `response_timeout_s`, so the truncation is visible in the
  daemon log. Exactly one line, at that seam, nowhere else.
- **Known residual, not fixed here and not a regression:** a response whose
  whole text is a single segment (a `kind="text"` open, or a stream that emitted
  one chunk) still has one deadline covering the entire answer, because the
  reschedule fires once before that segment's I/O. Real answers are
  multi-segment (see Current behavior), so this does not cover the owner's case.
  Do not widen the card to chase it.

## Affected contracts and files
- L5 `jarvis/surface/voice_media.py:2166-2170` — pass the budget unconditionally
  and pass liveness explicitly.
- L5 `jarvis/surface/voice_media.py:2234-2265`, `:2413-2427` — non-optional
  `budget` parameter, explicit liveness parameter, delete the `budget is not
  None` derivation, unconditional reschedules, corrected intent comment.
- L5 `jarvis/surface/voice_media.py:2185-2186` — one `LOGGER.warning`.
- `tests/integration/test_incremental_tts.py` — the new hermetic tests belong
  here; it already imports the wave2 fakes (`:21-28`) and owns the non-live
  stream shape (`:540-566`).
- `tests/canary/` — one new AST canary (see Acceptance evidence for why a
  canary and not a behavioral test).

## Boundaries and non-goals
- Layers that may change: L5 surface, tests. Nothing else.
- Must not change: `response_timeout_s` (45.0 in `config/jarvis.yaml:253` and in
  the owner's overlay), `_MAX_RESPONSE_TIMEOUT_S`, the ring size, the
  backpressure loop, `_await_segments`, the interrupt path, the terminal payload
  shape, or `desktop/` (Swift baseline 178 — do not touch it, do not run it).
- Non-goal — the missing ring-starvation counter: `read_into`
  (`voice_tts.py:283-314`) zero-pads silently and `_callback` returns on
  `actual <= 0` with nothing counted. Separate work.
- Non-goal — TTS clipping / loudness. Landed separately as
  `docs/goals/tts-volume-headroom.md`.
- Non-goal — the 68-138 ms inter-sentence provider gaps.
- Non-goal — acting on `retryable=True`. It is recorded (`:3015-3016`) and
  advisory; nothing retries. Do not build a retry.
- Non-goal — truncation from `foreground_superseded` or `daemon_restart`. Those
  are different terminal reasons on different paths.
- Non-goal — the three commentary responses that never got a `playback_started`
  at all. Separate chain, under diagnosis elsewhere.
- Non-goal — any change to `speak_from_segments` or to whether the live path is
  enabled in config.

## Rejected approaches
- **Raising `response_timeout_s`** (to 120, to `_MAX_RESPONSE_TIMEOUT_S`, or to
  anything). Rejected by the hub. It moves a silent cliff instead of removing
  one: a long enough answer still truncates, and the failure stays invisible.
  No config value changes in this card.
- Removing the timeout, or moving `_drain_and_complete` outside it. The budget
  is the only thing that bounds a wedged provider session; the drain loop
  (`:2677-2733`) polls with no deadline of its own.
- Adding a config flag, a compatibility shim, or an abstraction over "budget
  policy". One caller, one seam, two parameters.
- Making `live` mean both things but documenting it better. The hub ruled the
  conflation is the work.

## Acceptance evidence
- **Baseline first.** Before the first edit, run
  `PYTHONPATH=. .venv/bin/python -m pytest -q -m "not live_llm and not live_codex"`
  and record the actual passed/deselected counts under Progress. It was around
  **1044 passed / 64 deselected** at the merge landing now — confirm it yourself
  and use your own number, never this one.
- **Regression**: the same command after the change, at the recorded baseline
  plus this card's new tests, same deselected count. Explicitly among them,
  named and shown:
  `tests/integration/test_incremental_tts.py::test_tag_before_playback_started_falls_back_to_emitted_time_speaking`,
  which drives the non-live path with `speak_from_segments=True` and asserts
  both `"incremental" not in started[0][1]` (`:624`) and a
  `surface.playback_completed` whose `speech_text_hash` covers the whole text
  (`:626`), and
  `tests/integration/test_incremental_tts.py::test_tag_after_playback_started_fails_the_run_and_stops_segments`,
  which asserts `playback_failed` / `reason == "stream_chunk_tagged"` (`:656`)
  on the live path. Together they pin both meanings of `live` from the outside.
- **Positive, new hermetic test — the emitted terminal event.** A non-live
  multi-segment generation whose TOTAL elapsed exceeds `response_timeout_s`
  while every INDIVIDUAL segment stays well under it must emit
  `surface.playback_completed`. Build it in
  `tests/integration/test_incremental_tts.py` from the helpers already there:
  `_pipeline(..., speak_from_segments=False)` (`:319-334`) with
  `replace(_config(), response_timeout_s=<small>)`, `_open` / `_chunk` x4 /
  `_emitted` submitted together so playback starts after emitted (the non-live
  path), and a `_FakeProvider` whose per-segment `_Behavior(final_delay_s=...)`
  (`tests/integration/test_wave2_streaming_media.py:51`, awaited per segment at
  `:121`/`:132`) is under the budget while 4 x it is over. Assert on the
  ledger rows through `_rows`: exactly one `surface.playback_completed` for that
  response_id, **zero** `surface.playback_failed`, `submitted_samples ==
  total_samples` on that payload, and `heard_through_sequence` equal to the last
  prepared sequence. Note honestly in the test that `submitted_samples ==
  total_samples` restates completion rather than adding an independent fact; the
  discriminating assertions are the event type and the absent failure row.
- **Control, must be RUN and its raw output shown, not asserted in prose**:
  with the fix reverted (budget forced back to `None` at the call site), the
  same test emits `surface.playback_failed` with `reason:
  tts_response_timeout`. Paste the failure output. A positive test that has
  never been seen to fail proves nothing here.
- **Negative, new hermetic test — the budget still bites, and the log line.**
  One segment whose provider delay exceeds `response_timeout_s` must still emit
  `surface.playback_failed` with `reason: tts_response_timeout`, and the
  captured log (`caplog`) must contain the new warning carrying that
  response_id. This is the only assertion on R3's line and the only guard
  against "fix" by deleting the timeout. Nothing asserts
  `reason == "tts_response_timeout"` today: `grep -rn tts_response_timeout
  tests/` returns nothing, and the one existing timeout test
  (`test_wave2_streaming_media.py:1752`) asserts only `provider == "macos_say"`
  and the process ordering.
- **Canary for the R2 split — and read why it is a canary.** A leaked
  `live=True` on the non-live path has **no observable consequence today**: the
  non-live path always has `response.emitted` True (playback is scheduled at
  `_response_emitted`, `voice_media.py:1948-1958`), so `_await_segments`
  (`:2449-2468`) returns `False` immediately at `:2465-2466`, and
  `response.tagged` is only ever set on the active response
  (`:1963-1969`). So write ONE stdlib-`ast` canary in `tests/canary/`, house
  style (see `tests/canary/_helpers.py`): in
  `jarvis/surface/voice_media.py`, `_stream_with_prefix_fallback` has a
  `budget` parameter that is not `Optional`/defaulted to `None`, has a separate
  boolean liveness parameter, and its body contains no comparison of `budget`
  against `None`. Do not fabricate a behavioral test for a difference that does
  not exist.
- **Live run: REQUIRED, both directions.** The cliff only appears past a length
  threshold, so a one-directional run proves nothing.
  - Run the BEFORE direction first, on the unmodified checkout: one prompt
    asking for a long explanation (7+ full sentences; keep lengthening until the
    prepared audio exceeds 45 s) must produce `surface.playback_failed` with
    `reason: tts_response_timeout`, `submitted_samples < total_samples`, and a
    `playback_started`-to-terminal gap of ~45.0 s. Then apply the fix and run
    the same prompt: `surface.playback_completed`, `submitted_samples ==
    total_samples`, `total_samples / 48000 > 45`, and `heard_through_sequence`
    equal to the highest `sequence` among that response's
    `surface.playback_segment_prepared` rows.
  - **Anti-false-PASS guard, mandatory**: assert `provider ==
    "minimax_ws_streaming"` and `submitted_samples > 0` on both terminals. The
    macOS `say` fallback (`enable_macos_say_fallback: true` in the live overlay)
    still emits `playback_started` and a terminal, but `_run_macos_say`
    (`:2497-2553`) never calls `write_generation`, so its payload carries
    `provider: "macos_say"` with `submitted_samples` and `total_samples` both 0
    — which satisfies `submitted_samples == total_samples` trivially and would
    manufacture a PASS on the exact defect this card claims to fix.
  - The MiniMax key is what keeps the run off that fallback. Load it as
    `set -a; source ~/.jarvis/env; set +a` — the file quotes the value and the
    plain loader does not interpret quotes, so a daemon fed by the file alone
    speaks through macOS `say` and proves nothing.
  - **The lane runs on its OWN port and runtime root.** Precedent:
    `docs/goals/speak-ordinary-answers.md` Progress used `127.0.0.1:8007` with
    root `~/.jarvis-card-speak`. Pick an unused port (8006 is the shared rig's,
    8009 is the owner's — check with `lsof -nP -iTCP:<port> -sTCP:LISTEN`
    before binding) and a fresh root. Copy the overlay
    `~/.jarvis-realtime-test/overlay/` into that root and edit **your copy**:
    keep `realtime.streaming_output.response_timeout_s: 45.0`, keep
    `speak_from_segments` absent (that IS the defect path — do not enable it),
    set `realtime.single_audio_ingress.enabled: false` so you do not contend
    with the owner's microphone, and set `realtime.output_device: "BlackHole
    16ch"`.
  - **Audio rule, mandatory.** `realtime.output_device` opens that device
    directly and does **not** change the macOS system default
    (`config/jarvis.yaml:152-155`), and `BlackHole 16ch` is present on this
    machine (`sounddevice` device index 3), so this run needs **no** device
    switch: skip it entirely. If any step would switch the system default
    anyway, capture the pre-run route first and restore it in a `finally`/trap
    on every exit path; if the captured route is already `BlackHole 16ch`,
    restore `MacBook Pro Speakers` instead. If BlackHole cannot be opened, STOP
    and report — do not fall back to playing 47 s of speech through the owner's
    speakers while he is working.
  - **Do not touch the owner's environment.** His daemon on port 8009 with
    runtime root `/Users/alllllenshi/.jarvis-allen-test` and its running
    InherentCard, and the worktree `.claude/worktrees/realtime-live-test` which
    holds uncommitted Swift edits he needs, must not be stopped, restarted, or
    written into. Do not edit the scripts in `~/.jarvis-realtime-test` or
    `~/.jarvis-audio-test`; copy out of them instead.
- **Gates**: `PYTHONPATH=. .venv/bin/lint-imports`,
  `PYTHONPATH=. .venv/bin/ruff check .`,
  `PYTHONPATH=. .venv/bin/mypy --strict jarvis tests scripts tools`.
  `PYTHONPATH=.` is mandatory on every one.
- **Swift**: no `desktop/` change. Do not run the Swift suite.

## Docs to sync
- **none.** Nothing canonical owns the bound; the code comment at
  `voice_media.py:2261-2262` owns it, and this card changes what it must say.
  The greps that establish this, run at `5759479`:
  - `grep -rn "response_timeout" docs/` → 3 hits, none canonical:
    `docs/live-burn-2026-09-05-crash-recovery.md:270` (a historical burn
    record) and `docs/goals/incremental-tts-from-permitted-segments.md:43,89`
    (a prior goal card, i.e. a run record). Historical documents are not
    rewritten.
  - `grep -rn "tts_response_timeout" docs/` → 1 hit, the same burn record.
  - `grep -rnE -i "playback budget|tts budget|speech budget" docs/` → no hits.
  - `docs/adr/0006-full-duplex-voice-session.md:624-627` owns the
    `surface.playback_failed` **payload shape**, which this card does not
    change; it states no timeout bound and no `reason` vocabulary. Judge
    explicitly unchanged.
  - `docs/adr/0008-real-time-response-streaming.md:807` mentions a "commit/
    delivery budget" — that is the L4 permit-to-surface path, not TTS playback.
    Judge explicitly unchanged.
  - `docs/spec.html` has no playback-timeout contract
    (`grep -n "timeout" docs/spec.html` returns action-lifecycle and ASR hits
    only). Judge explicitly unchanged.
- If the implementation ends up altering a documented contract anyway, update
  the canonical document that owns that fact; do not duplicate a fact across
  documents and do not document what the code already makes clear.

## Open questions
(none)

## /goal condition

The transcript shows all of the following as raw command output, pasted in full
and not summarized.

1. Baseline: raw output of `PYTHONPATH=. .venv/bin/python -m pytest -q -m "not
live_llm and not live_codex"` run BEFORE any edit, its passed/deselected counts
stated as the branch baseline.
2. Regression: the same command after the change, raw, at that baseline plus
this card's new tests, same deselected count. The transcript names
`test_tag_before_playback_started_falls_back_to_emitted_time_speaking` and
`test_tag_after_playback_started_fails_the_run_and_stops_segments` as passing.
3. Gates, each raw and each with a visible `PYTHONPATH=.`:
`.venv/bin/lint-imports` printing the contract KEPT, `.venv/bin/ruff check .`
clean, `.venv/bin/mypy --strict jarvis tests scripts tools` printing Success.
4. Positive test: raw passing output, and the transcript states its assertions —
a non-live multi-segment generation whose total elapsed exceeds
`response_timeout_s` while each segment stays under it emits exactly one
`surface.playback_completed`, zero `surface.playback_failed`, `submitted_samples
== total_samples`, and `heard_through_sequence` equal to the last prepared
sequence.
5. Control, RUN not asserted in prose: raw output of that same test with the fix
reverted (budget forced back to `None`), showing it emits `surface.playback_failed`
with `reason: tts_response_timeout`. Without this the positive test is unproven.
6. Negative test: raw passing output showing one over-budget segment still emits
`surface.playback_failed` / `tts_response_timeout`, and that the captured log
contains the new warning naming that response id — the only log line added.
7. Canary: raw passing output of the new AST canary, and what it pins:
`_stream_with_prefix_fallback` takes a non-optional `budget` plus a separate
boolean liveness parameter, and compares `budget` to `None` nowhere.
8. LIVE RUN, both directions, from the lane's own daemon and its own ledger:
   - BEFORE: raw rows for one >45 s answer showing `surface.playback_failed`,
     `reason: tts_response_timeout`, `submitted_samples < total_samples`, and a
     `playback_started`-to-terminal gap of about 45.0 s.
   - AFTER: raw rows for the same prompt showing `surface.playback_completed`,
     `submitted_samples == total_samples`, `total_samples / 48000 > 45`, and
     `heard_through_sequence` equal to the highest prepared `sequence`.
   - BOTH terminals show `provider: "minimax_ws_streaming"` and
     `submitted_samples > 0` — a `macos_say` terminal carries 0 == 0 and is a
     false pass — and the key was loaded with
     `set -a; source ~/.jarvis/env; set +a`.
9. Environment: the transcript names the lane's own port and runtime root (not
8006/8009, not `~/.jarvis-allen-test`), and states that the owner's daemon, his
InherentCard, the `realtime-live-test` worktree and the `~/.jarvis-realtime-test`
/ `~/.jarvis-audio-test` scripts were untouched, and that the macOS system
default output device never changed (`realtime.output_device: "BlackHole 16ch"`
opens the device directly) or was captured and restored.
10. Scope: raw `git diff --stat` with no `desktop/` file and no config change —
`response_timeout_s` still 45.0, `speak_from_segments` still false. No retry,
config flag, or abstraction added.
11. Docs: each entry under "Docs to sync" shown as updated or explicitly judged
unchanged with its grep output. Where the change altered a documented contract,
invariant, ownership boundary, or externally relevant behavior, the canonical
document that owns that fact was updated; no fact was duplicated across
documents, and nothing the code already makes clear was documented.
12. `git status` shows a clean tree and each committed slice is named under
Progress with its sha.

Stop and report rather than redesigning if the card contradicts the repository,
if the live run cannot produce a >45 s answer, or if BlackHole 16ch cannot be
opened. Or stop after 30 turns.

## Progress
- Baseline before any edit — `realtime-integration` at 81c9a61 —
  `pytest -q -m "not live_llm and not live_codex"` → **1044 passed, 64
  deselected** in 51.48s. That is this branch's baseline.
- Per-segment budget + acceptance — 0f81acd — `_stream_with_prefix_fallback`
  now takes a non-optional `budget` and an explicit `live: bool`;
  `live = budget is not None` is gone and both reschedules are unconditional,
  so the caller no longer withholds the budget on the non-live path. One
  `LOGGER.warning` at the `except TimeoutError` seam names the response id and
  the expired `response_timeout_s`. Regression: **1047 passed, 64 deselected**
  (baseline + 3), including
  `test_tag_before_playback_started_falls_back_to_emitted_time_speaking` and
  `test_tag_after_playback_started_fails_the_run_and_stops_segments`, which pin
  both meanings of `live` from the outside. Gates: lint-imports `6-layer
  architecture KEPT` (1 kept, 0 broken, 98 files / 309 dependencies), ruff `All
  checks passed!`, mypy `Success: no issues found in 245 source files`.
- Hermetic positive — 0f81acd —
  `test_a_multi_segment_answer_longer_than_the_budget_still_completes`: four
  0.4 s provider segments (1.6 s total) against `response_timeout_s=1.0` emit
  exactly one `surface.playback_completed`, zero `surface.playback_failed`,
  `submitted_samples == total_samples`, and `heard_through_sequence` equal to
  the last prepared sequence. Control RUN with the fix reverted: the same test
  fails with `surface.playback_failed`, `'reason': 'tts_response_timeout'`,
  `'heard_through_sequence': 1` of 3 — i.e. truncated mid-answer, the owner's
  defect reproduced hermetically.
- Hermetic negative — 0f81acd —
  `test_a_single_segment_over_the_budget_still_fails_the_run_and_is_logged`:
  one 4.0 s segment against a 1.0 s budget still emits
  `surface.playback_failed` / `reason: tts_response_timeout`, and `caplog`
  carries the new warning naming that response id. The budget still bites.
- Canary — 0f81acd — `tests/canary/test_canary_playback_budget_not_liveness.py`
  pins by AST that `budget` is keyword-only, non-optional and undefaulted, that
  `live: bool` is its own parameter, and that the body compares `budget` to
  `None` nowhere. Verified to FAIL against the pre-fix file
  (`assert 'live' in ['budget']`). A canary and not a behavioural test because
  a leaked `live=True` has no observable consequence on the non-live path.
- LIVE RUN, both directions — lane's own daemon on **127.0.0.1:8011**, runtime
  root **~/.jarvis-lane-b-budget**, own overlay copy (`response_timeout_s: 45.0`
  kept, `speak_from_segments` absent, `single_audio_ingress.enabled: false`,
  `realtime.output_device: "BlackHole 16ch"`), key loaded with
  `set -a; source ~/.jarvis/env; set +a`. Same prompt both directions (a 12+
  sentence Chinese explanation of virtual memory).
  - BEFORE (pre-fix file, turn `T61240e01`): `surface.playback_started`
    03:37:42.539Z → `surface.playback_failed` 03:38:27.540Z = **45.001 s**,
    `reason: tts_response_timeout`, `submitted_samples: 2101696 <
    total_samples: 2232640` (2.728 s of prepared speech never spoken),
    `heard_through_sequence: 3` while sequence 4 was already prepared,
    `provider: "minimax_ws_streaming"`. The owner's ledger figure reproduced to
    the millisecond.
  - AFTER (turn `T35427691`, response `RESP7b099053a6b1473cb95a00ba1eb0c1d3`):
    `surface.playback_completed`, zero `surface.playback_failed`,
    `submitted_samples == total_samples == 8423624`,
    `total_samples / 48000 = 175.49 s > 45`, `heard_through_sequence: 14`
    equal to the highest prepared `sequence` (14 of 14 prepared),
    `provider: "minimax_ws_streaming"`. Nearly four times the old cliff, spoken
    end to end.
  - Anti-false-PASS guard: both terminals carry `provider:
    "minimax_ws_streaming"` and `submitted_samples > 0`, so neither is a
    `macos_say` 0 == 0 pass.
  - Owner's environment untouched: his daemon stayed up on 8009 (pid 45429,
    root `~/.jarvis-allen-test`) across both runs, the `realtime-live-test`
    worktree and the `~/.jarvis-realtime-test` / `~/.jarvis-audio-test` scripts
    were only read, never written. No device switch was issued —
    `realtime.output_device` opens BlackHole directly — and the macOS system
    default output read back as `MacBook Pro Speakers`, unchanged. Lane daemon
    stopped afterwards; 8011 free.
- Docs to sync — **none**, as the card predicted, each re-verified: the only
  `response_timeout` / `tts_response_timeout` hits outside this card are
  `docs/live-burn-2026-09-05-crash-recovery.md:270` (a historical burn record)
  and `docs/goals/incremental-tts-from-permitted-segments.md:43,89` (a prior
  run record); `playback budget|tts budget|speech budget` has no hits outside
  this card; `docs/spec.html` timeout hits are action-lifecycle and ASR only;
  ADR-0006's `surface.playback_failed` payload shape and ADR-0008:807's L4
  commit/delivery budget are both unchanged by this diff. The bound is owned by
  the code comment at the reschedule, which this card rewrote.
- Owner follow-up, not a blocker: the live runs were text-submitted, so
  mic-in-the-loop was never exercised (`single_audio_ingress.enabled: false` by
  card instruction, to avoid contending with the owner's microphone).
- Verifier (fresh context, opus, `realtime-integration...HEAD`) — no confirmed
  defect. It independently re-ran the reverted-fix control and re-derived the
  live evidence from a read-only copy of the lane ledger, matching every field.
  Two of its five minor observations were fixed in 518f56f:
  - The comment claimed the window covered "this segment's wait". It does not:
    `_await_segments` for the current segment runs before the reschedule, so a
    window covers this segment's I/O and playback and runs into the *next*
    segment's wait. Restated accordingly.
  - The positive test pinned the non-live path only structurally. It now
    asserts the activation carries no `incremental` flag.
  Three observations were left alone as card-sanctioned or out of scope, and
  are reported to the hub rather than fixed here: the macOS `say` fallback puts
  the whole remaining answer in one window (a degraded path, and still strictly
  better than the pre-fix anchor); the `elapsed` assertion spans pipeline
  setup, with the discriminating power coming from the completed row and the
  absent failure row as the card itself says; and non-live foreground occupancy
  is now bounded only by `n_segments x response_timeout_s`, which is exactly
  the "Total generation length is unbounded" contract this card creates.

