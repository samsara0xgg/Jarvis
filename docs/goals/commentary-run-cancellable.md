# Goal: commentary-run-cancellable

## Goal
An open `phase="commentary"` ResponseRun lives in the runtime's own
`ResponseRunRegistry`, so `/inherent/cancel-response` can close it by id, while a
confirmed barge-in keeps resolving to exactly the open `final` run it resolves to
today.

## Why
An unheard commentary has no cancel path at all. Its only three close paths are
playback (`_commentary_heard`, jarvis/runtime/inherent_loop.py:1697-1698, :1734-1748),
supersession by a newer lifecycle row (`_retire_superseded_commentary`, :1475-1484),
and the watcher's shutdown sweep (`_cancel_unheard_commentary` under
`asyncio.CancelledError`, :1720-1731, reason `"shutdown"`). A commentary that is
never played and never superseded therefore stays open until process teardown, and
the operator seam that exists precisely to close a run cannot see it.

## Current behavior
- `_render_commentary` builds a throwaway registry per run —
  `registry = ResponseRunRegistry()` then `registry.register(run)`
  (jarvis/runtime/inherent_loop.py:1566-1567) — and stores it on
  `_OpenCommentary.registry` (:1319, field at :1331). The runtime's registry
  (`jarvis/runtime/__init__.py:1592-1593`) is populated only by the normal response
  path (:1692-1693), which unregisters at :2405-2406 and :2647-2648.
- `request_response_cancel` looks the target up with
  `registry.get(request.response_id)` (jarvis/decision/response_run.py:782) and
  returns `CancelRejected(reason="unknown_response")` on a miss (:784). Since the
  runtime registry never holds a commentary run, every `/inherent/cancel-response`
  for a commentary id lands on that branch; the handler returns it verbatim as
  `{"outcome": "unknown_response"}` (jarvis/surface/inherent_server.py:897-914,
  outcome mapping at jarvis/runtime/__init__.py:1774-1781).
- `ResponseRunRegistry.open_runs()` filters on `run.is_open` only
  (jarvis/decision/response_run.py:442-446; `is_open` at :367-369 over
  `_OPEN_STATES = {"idle","generating","waiting_action","finalizing"}` at :82-84).
  It does not look at `phase`.
- `make_barge_in_interrupt_callable._interrupt` is the only caller of `open_runs()`
  (jarvis/runtime/__init__.py:1817, :1822): 0 open runs → `"no_open_run"`
  (:1823-1824), 2+ → `"ambiguous_open_runs"` (:1825-1826) with no cancel attempted,
  exactly 1 → policy check and cancel (:1827-1832).
- The two windows genuinely overlap. A turn that dispatches an async action marks
  its final run `waiting_action` — an open state — at jarvis/runtime/__init__.py:2462,
  and the commentary watcher opens a commentary run off that same
  `action.dispatched` / `action.running` row (`_COMMENTARY_ACTION_TYPES`,
  jarvis/runtime/inherent_loop.py:1286-1291; dispatch at :1704-1711). Both carry the
  same `response_group_id = stable_response_group_id(turn_id)`
  (jarvis/decision/response_run.py:679).
- A commentary run's `ResponseInterruptPolicy` is the default one
  (`confirmed_playback="interrupt_expected_playback_generation"`,
  `generation_action="cancel"`, jarvis/shared/realtime.py:220-225), so nothing in the
  policy layer would stop a barge-in from cancelling a commentary if it ever became
  the resolved target.
- `lifecycle_commentary` and `independent_response_cancel` are independent switches;
  both only require `response_run_lifecycle` (jarvis/runtime/__init__.py:699-704). So
  `runtime.response_runs` is legitimately `None` while commentary is on — the shipped
  commentary test harness runs exactly that way
  (tests/integration/test_lifecycle_commentary.py:213-227, :243-263).
- Every other registry reader is keyed by exact `response_id`; nothing enumerates it
  except `_interrupt`. The class is a lock-guarded `dict[str, ResponseRun]`
  (jarvis/decision/response_run.py:419-446) — no projection, snapshot, wire frame or
  fold reads it, and `register` has no side effect beyond the dict write.

## Target behavior
- `_render_commentary` registers the run in `runtime.response_runs` under the *same*
  condition the normal response path already uses —
  `runtime.response_flags.independent_response_cancel and runtime.response_runs is not None`
  (jarvis/runtime/__init__.py:1692-1693) — and keeps today's private
  `ResponseRunRegistry()` as the fallback when that condition is false. No new flag
  dependency, no downgrade warning. Without `independent_response_cancel` no run of
  any phase is registered and `/inherent/cancel-response` answers `unknown_response`
  for everything, final runs included; the fallback is what makes a commentary run
  behave exactly like a final run under every flag combination, which is the
  invariant worth holding — a special case for commentary would not be.
- `/inherent/cancel-response` with an open, unheard commentary's `response_id` and
  `scope="generation"` answers `{"outcome": "cancelled"}` and commits one
  `response.cancelled` row for that `response_id` carrying the requested `reason` and
  `cancel_scope="generation"`.
- Barge-in resolves its target from the open runs whose `phase == "final"`. The
  filter is applied at the call site in `_interrupt`, before the zero / one / many
  branches, so a concurrently-open commentary neither becomes the target nor turns a
  single open final run into `"ambiguous_open_runs"`. With only commentary runs open,
  the outcome is `"no_open_run"` — the existing vocabulary, unchanged.
- Registration is symmetric. Every path by which the commentary watcher lets go of an
  entry unregisters that `response_id`: `_complete_commentary` (:1507-1521, including
  its early return when a cancel already won the CAS), and `_cancel_unheard_commentary`
  (:1487-1505) unconditionally after the call, whatever `CancelOutcome` came back —
  so the supersede path, the shutdown sweep, and a re-cancel of an already-terminal
  run all leave the registry clean. No entry the watcher tracked outlives the watcher.
- A commentary closed by playback or by supersession is no longer a cancel target:
  a later `/inherent/cancel-response` for that id answers `unknown_response`.
- Known and accepted, not sloppiness: a commentary closed by the operator seam
  itself stays registered (terminal, therefore not open, therefore invisible to
  barge-in) until the watcher next releases the entry — heard, superseded, or
  shutdown. Unregistering at cancel time instead would have to happen in the shared
  `make_response_cancel_callable`, which would regress a *final* run's second-cancel
  answer from `already_terminal` to `unknown_response`; the bounded delay is the
  cheaper of the two.
- Cancelling a commentary stops generation and closes the run. It does **not** stop
  the sound: PCM already committed to the ring buffer plays to the end, so Allen can
  hear a phrase he just cancelled. That is a known limit owned by the separate
  `foreground_output` playback-lease card, not a defect of this one — do not chase it
  here, and do not read it as a regression this card introduced.

## Affected contracts and files
- L6/runtime `jarvis/runtime/inherent_loop.py:1566-1567` — `_render_commentary` uses
  the runtime registry when present.
- L6/runtime `jarvis/runtime/inherent_loop.py:1487-1521` — `_cancel_unheard_commentary`
  and `_complete_commentary` unregister on every close path.
- L6/runtime `jarvis/runtime/__init__.py:1822` — `_interrupt` filters `open_runs()`
  to `phase == "final"` before counting.
- tests — the three cases under Acceptance evidence. Which file holds them is the
  implementation session's call; the card fixes only the observable each must assert
  on.
- docs `docs/adr/0006-full-duplex-voice-session.md:450-453` — the D8 bullet that names
  the barge-in target.

## Boundaries and non-goals
- Layers that may change: `runtime/` only (plus tests and one ADR). No file under
  `jarvis/decision/`, `jarvis/state/`, `jarvis/surface/`, `jarvis/execution/` changes.
- Must not change: `ResponseRunRegistry.open_runs()` — its signature, its docstring,
  and its meaning stay "every registered run whose FSM has not terminated"
  (jarvis/decision/response_run.py:442-446).
- Must not change: `_OPEN_STATES`, `ResponseRun.is_open`, or the FSM transition table.
- Must not change: the barge-in outcome vocabulary — `no_open_run`,
  `ambiguous_open_runs`, `policy_ignore`, `policy_generation_continue`, plus whatever
  `make_response_cancel_callable` already returns. No new outcome string.
- Must not change: `RESPONSE_CANCEL_REASONS` (jarvis/shared/realtime.py:23-25) or the
  reasons the three existing commentary close paths use (`superseded`, `shutdown`).
- Must not change: the flag-off event log. With `realtime.commentary.enabled: false`
  the log stays byte-identical
  (tests/integration/test_lifecycle_commentary.py:845).
- Must not change: `scope="foreground_output"` stays rejected
  (jarvis/decision/response_run.py:780-781).
- Non-goal: stopping the audible tail. Cancelling a commentary closes the run and
  stops generation; PCM already committed to the ring buffer keeps playing to the
  speaker. Silencing it needs the `foreground_output` scope and the `GenerationLease`
  playback lease (jarvis/surface/voice_ledger.py:26-33), which is a separate card.
  This is a documented limit of this change, not a regression introduced by it.
- Non-goal: a Swift/wire control for cancelling a commentary. The HTTP seam is the
  whole surface here.
- Non-goal: teaching the barge-in path to prefer the run that owns the current
  playback generation. That is still the `foreground_output` lease's job.

## Rejected approaches
- **Narrow `open_runs()` to final runs.** It has exactly one caller, so narrowing the
  accessor buys nothing and makes the registry lie to every future caller. The filter
  belongs at the call site that has the requirement (R2).
- **Register the commentary run and stop there.** That is the trap: the final run is
  `waiting_action` (open) at the exact moment the commentary opens off the same action
  row, so `_interrupt` would see two open runs and return `"ambiguous_open_runs"` —
  silently disabling barge-in for every action-dispatching turn, with no new outcome
  string and no failing test to notice it.
- **Stop the audio too, in this card.** Generation cancel and playback silence are
  separate protocols (ADR-0008 D10 scope table; ADR-0014 D20 explicitly does *not*
  append `response.cancelled`). Bundling them would require the playback lease and
  would change what `scope="generation"` means (R3).
- **Unregister inside `make_response_cancel_callable` on `CancelAccepted`.** It is the
  shared seam: doing so would flip a *final* run's second-cancel answer from
  `already_terminal` to `unknown_response`, a contract change this card does not own.
- **Give `_OpenCommentary` a nullable registry and branch at every use site.**
  Choosing the registry once at construction — the runtime's when
  `independent_response_cancel` is on and `response_runs` is not None, today's private
  one otherwise — keeps the field non-optional and every existing use site untouched.

## Acceptance evidence
Establish the hermetic baseline BEFORE the first edit:
`PYTHONPATH=. .venv/bin/python -m pytest -q -m "not live_llm and not live_codex"`,
and report that number. Every count below is expressed against it, never as an
absolute — the integration count is moving as merges land.

- Positive (operator cancel reaches a commentary):
  `PYTHONPATH=. .venv/bin/python -m pytest -q tests/integration -k cancel_response`
  reports `1 passed`, the remainder deselected. The case drives a real
  `action.dispatched` row through the real commentary observer, on a runtime whose
  config has both
  `realtime.response.independent_response_cancel: true` and
  `realtime.commentary.enabled: true`, then POSTs
  `{"response_id": <the commentary's id>, "scope": "generation", "reason": "user_stop"}`
  to `/inherent/cancel-response` through `fastapi.testclient.TestClient` over
  `create_app(InherentDeps(..., cancel_response_callable=make_response_cancel_callable(runtime)))`
  (the precedent is tests/integration/test_wave4a_response_run.py:1229-1278).
  Observables asserted: the HTTP body field `outcome == "cancelled"`, and exactly one
  `response.cancelled` row in the event log whose payload `response_id` is that
  commentary's id, `reason` is `"user_stop"` and `cancel_scope` is `"generation"`.
  The same POST on the pre-change code returns `{"outcome": "unknown_response"}` and
  writes no row — state that in the transcript.

- Regression, and the point of the card (R1 — barge-in is unchanged):
  `PYTHONPATH=. .venv/bin/python -m pytest -q tests/integration -k barge_in_while_a_commentary_is_open`
  reports `1 passed`, the remainder deselected. The case has a real commentary run
  open through that same observer path while a `final` run for another turn is
  registered and marked
  `waiting_action`, then calls `make_barge_in_interrupt_callable(runtime)("keyword")`.
  Observable asserted: the event log gains exactly one `response.cancelled` row, its
  payload `response_id` is the **final** run's id, `reason` is `"barge_in"`,
  `cancel_scope` is `"generation"`, and no `response.cancelled` row names the
  commentary's id. The before/after pair IS the test: with the registration in place
  but the `phase == "final"` filter removed, `_interrupt` returns
  `"ambiguous_open_runs"` and that row is ABSENT. The transcript must show both runs
  of this one command — once with the filter temporarily removed (fails, quote the
  assertion showing the missing row) and once with it restored (passes). That control
  is not busywork: a case that only ever runs against the finished code passes
  identically whether the filter is there or not, so without the failing run there is
  no evidence the filter is load-bearing and the trap it exists to prevent stays
  untested.

- Regression (R4 — a closed commentary is not a cancel target):
  `PYTHONPATH=. .venv/bin/python -m pytest -q tests/integration -k no_longer_a_cancel_target`
  reports `1 passed`, the remainder deselected. One case, two close paths in sequence: a commentary closed by
  `surface.playback_started`, and a commentary closed by supersession. Observable
  asserted in both: a `/inherent/cancel-response` POST for that id afterwards returns
  the body `{"outcome": "unknown_response"}` — `already_terminal` would mean the entry
  is still registered, so this string is exactly the proof of unregistration.

- Regression (the shipped commentary and barge-in suites):
  `PYTHONPATH=. .venv/bin/python -m pytest -q tests/integration/test_lifecycle_commentary.py tests/integration/test_barge_in_two_stage.py tests/integration/test_wave4a_response_run.py`
  passes with no case removed or weakened, including
  `test_flag_off_event_log_is_what_the_observer_never_touched`,
  `test_zero_or_ambiguous_open_runs_cancel_nothing`, and
  `test_final_shares_the_group_keeps_phase_final_and_plays_after_the_commentary`.

- Regression (full hermetic + Tier 1):
  `PYTHONPATH=. .venv/bin/python -m pytest -q -m "not live_llm and not live_codex"`
  prints baseline + 3 passed with the deselected count unchanged; report baseline,
  final, and k explicitly. `lint-imports`, `ruff check .`, and
  `mypy --strict jarvis tests scripts tools` each exit 0.

- Live run: **not required.** Nothing here touches an LLM, a TTS provider, or an
  external runtime — the commentary phrase is deterministic and `_render_commentary`
  issues no provider request, and every fact this card adds is a SQLite event-log row
  or an HTTP body field, fully observable hermetically. The one behavior a live run
  would expose — whether the speaker actually goes quiet — is R3's explicit non-goal
  and belongs to the `foreground_output` card. No audio is needed, so no output-route
  switch is performed and the audio rule does not apply; do not change the system
  default output device during this card.

## Docs to sync
- `docs/adr/0006-full-duplex-voice-session.md:450-453` — D8's second boundary bullet
  currently reads "The target is the single open run, not the run that owns the
  current playback generation. With more than one open run the runtime cancels
  nothing (`ambiguous_open_runs`)". After this change the target is the single open
  **final** run, and a concurrently open commentary is not counted. Amend that bullet;
  do not restate the rest of D8.
- `docs/adr/0008-real-time-response-streaming.md:370` (D6) — **judge this one against
  the code you wrote, and record the disposition.** The sentence reads "the run
  instead stays open until `surface.playback_started` names it". If D6 is enumerating
  a commentary run's close paths, this card adds one and the enumeration needs the
  amendment. If it is stating the run's terminal condition, that condition is
  unchanged and the entry is explicitly judged unchanged. Do not amend it reflexively
  and do not skip it silently.
- `docs/adr/0008-real-time-response-streaming.md` D10 (:578-748) — **none.** The range
  owns the cancel scope table, the closed reason vocabulary, and "runtime applies the
  L3-issued policy mechanically"; all three stay true verbatim. Proof:
  `grep -n "open_runs\|ambiguous\|registry" docs/adr/0008-real-time-response-streaming.md`
  returns no hit inside 578-748.
- `docs/adr/0014-inherent-realtime-ux.md` D20-D24 (:1199-1578) — **none.** D20 is the
  `foreground_output` stop-speaking protocol and already states it does not append
  `response.cancelled` (:1218-1220); this card adds nothing to it and R3 keeps the
  audible tail out of scope. Proof:
  `sed -n '1199,1578p' docs/adr/0014-inherent-realtime-ux.md | grep -n "open_runs\|ambiguous\|commentary"`
  returns nothing.
- `docs/spec.html` — **none.** Proof:
  `grep -c "ResponseRun\|open_runs\|commentary" docs/spec.html` returns 0; §3.6.3
  (:1029-1050) owns `PresentationIntent`'s field set only, which this card does not
  touch.

## Open questions
(none)

## /goal condition

The goal is met when all of the following appear in the transcript.

(1) The hermetic baseline, measured with
`PYTHONPATH=. .venv/bin/python -m pytest -q -m "not live_llm and not live_codex"`
BEFORE the first edit, is stated as a number.

(2) The diff makes `_render_commentary` in jarvis/runtime/inherent_loop.py register
its run in `runtime.response_runs` when that registry exists and in a private
registry when it is None; makes `_cancel_unheard_commentary` and
`_complete_commentary` unregister the response id on every close path including the
early return; and filters `open_runs()` to `phase == "final"` inside
`make_barge_in_interrupt_callable._interrupt` in jarvis/runtime/__init__.py — with
`ResponseRunRegistry.open_runs()` itself unchanged in signature, docstring and
meaning, no new barge-in outcome string, no change under jarvis/decision/,
jarvis/state/, jarvis/surface/ or jarvis/execution/, and no attempt to stop playback
audio or support `scope="foreground_output"`.

(3) Raw pytest output showing a case in which `/inherent/cancel-response`, called
over a real TestClient for a commentary run opened by the real commentary observer,
returns the body `{"outcome": "cancelled"}` and the event log then holds exactly one
`response.cancelled` row whose `response_id` is that commentary's, `reason` is
`user_stop` and `cancel_scope` is `generation`; plus a statement that the same POST
answered `unknown_response` and wrote no row before the change.

(4) Raw pytest output of the barge-in regression case run TWICE from the same
command: once with the `phase == "final"` filter temporarily removed, showing it
FAILS because no `response.cancelled` row for the final run exists (`_interrupt`
returned `ambiguous_open_runs`), and once with the filter restored, showing it PASSES
with exactly one `response.cancelled` row naming the final run's `response_id` with
reason `barge_in` and no row naming the commentary's. Both outputs are shown.

(5) Raw pytest output showing that after a commentary is closed by
`surface.playback_started`, and after one is closed by supersession, a later
`/inherent/cancel-response` for that id returns `{"outcome": "unknown_response"}`.

(6) Raw output of
`PYTHONPATH=. .venv/bin/python -m pytest -q tests/integration/test_lifecycle_commentary.py tests/integration/test_barge_in_two_stage.py tests/integration/test_wave4a_response_run.py`
passing with no existing case deleted or weakened, and raw output of the full hermetic
suite showing baseline + 3 passed with the deselected count unchanged and k stated;
plus `lint-imports`, `ruff check .`, and `mypy --strict jarvis tests scripts tools`
each ending in a pass line or exit 0.

(7) A statement that no live run was performed and none was required, that the system
default audio output device was not changed, and that a cancelled commentary still
plays its already-committed audio to the end — a known limit owned by the separate
`foreground_output` playback-lease card, not a defect of this change.

(8) The D8 bullet at docs/adr/0006-full-duplex-voice-session.md:450-453 is amended to
say the barge-in target is the single open FINAL run and that an open commentary is
not counted; the D6 sentence at docs/adr/0008-real-time-response-streaming.md:370 is
either amended or explicitly judged unchanged, with the reading that decided it stated
in one line; and ADR-0008 D10, ADR-0014 D20-D24, and docs/spec.html are each
explicitly judged unchanged with the grep or sed output that proves it. Follow the
rule: update the canonical document that owns a changed contract, do not document what
the code already makes clear, do not duplicate a fact across documents.

(9) Each slice committed with the project commit skill, `git status` clean, and a
Progress line per slice appended to the card.

Or stop after 30 turns.

## Progress
- Hermetic baseline before the first edit — `1040 passed, 64 deselected` on
  realtime-integration (merged fast-forward into lane/a at 5ae0f98).
- Slice 1, runtime + tests — 7e9f98b — `_render_commentary` registers in
  `runtime.response_runs` under the normal response path's own condition with
  the private registry as fallback; `_cancel_unheard_commentary` and
  `_complete_commentary` unregister on every close path; `_interrupt` filters
  `open_runs()` to `phase == "final"` at the call site. Evidence:
  `-k cancel_response` 1 passed / 829 deselected;
  `-k no_longer_a_cancel_target` 1 passed / 829 deselected;
  `-k barge_in_while_a_commentary_is_open` run TWICE from one command — filter
  removed it FAILS `AssertionError: ambiguous_open_runs` /
  `assert [] == ['RESPf6d80afa972a4eda9b1d26a8bd5667c0']`, filter restored it
  passes 1 / 829. The pre-change registration was probed directly: the same
  POST returned `{'outcome': 'unknown_response'}` with `open_runs() == ()` and
  zero `response.cancelled` rows. Shipped suites
  (test_lifecycle_commentary, test_barge_in_two_stage, test_wave4a_response_run)
  80 passed, no case deleted or weakened. Full hermetic `1043 passed,
  64 deselected` = baseline + 3, k = 3, deselected unchanged. Tier 1:
  lint-imports KEPT (1/1) · ruff all checks passed · mypy strict clean
  (244 files).
- Slice 2, docs — 70420e9 — ADR-0006 D8's second boundary bullet now names the
  single open **final** run and says an open commentary is not counted.
  ADR-0008 D6 (:370) judged UNCHANGED: it states the commentary run's terminal
  condition against D1's, not an enumeration of close paths, so an added
  operator cancel leaves it true. ADR-0008 D10, ADR-0014 D20-D24 and
  docs/spec.html judged unchanged — `grep -c "ResponseRun\|open_runs\|
  commentary" docs/spec.html` returns 0, and the only `ambiguous` hits inside
  the two ADR ranges are ActionRun cancellation targets, none naming
  `open_runs` or the response-run registry. (The card predicted zero hits in
  ADR-0008 578-748; there are two, both about actions, so the conclusion holds
  and only the proof grep was imprecise.)
- No live run performed and none required — no LLM, TTS provider or external
  runtime is touched; every added fact is a SQLite event-log row or an HTTP
  body field. The system default audio output device was NOT changed. A
  cancelled commentary still plays its already-committed PCM to the end: a
  known limit owned by the separate `foreground_output` playback-lease card,
  not a defect of this change.
- Slice 3, verifier pass — 084c69d — `verifier` (opus, fresh context) over
  the card and `realtime-integration..HEAD` re-ran every acceptance and gate
  command and matched all reported numbers. It confirmed one real defect,
  D1: `_render_commentary` registered above three fallible calls
  (`pre_emit_gate`, `_emit_pre_emit_verdict`, `render_response`) with no
  `finally`, so a raising render left an entry the watcher never receives
  and no close path can unregister — harmless into the old throwaway
  registry, a permanently open `generating` run in `runtime.response_runs`
  after slice 1. Fixed by registering after the last fallible call. The
  barge-in two-run control was re-run against the fixed code: filter removed
  `AssertionError: ambiguous_open_runs` /
  `assert [] == ['RESP84091df91d6445a1ae628a6c62ce3cc4']`, filter restored
  1 passed / 829 deselected. Final: 1043 passed / 64 deselected, 80 passed
  across the three shipped suites, lint-imports / ruff / mypy --strict each
  exit 0.
- Verifier findings accepted without a code change, recorded here:
  (D2) `_cancel_unheard_commentary` unregisters after `request_response_cancel`
  returns, leaving a microsecond window in which a concurrent POST answers
  `already_terminal` rather than `unknown_response`, and skipping the
  unregister if that call raises. Both follow the card's explicit
  "unconditionally after the call" wording and the entry stays in
  `open_by_action` for the shutdown sweep, so this is a card-design nit, not
  an implementation deviation — changing it would be redesign.
  (D3) "only commentary runs open → `no_open_run`" has no dedicated case.
  Deliberately not added: /goal condition (6) fixes the suite at baseline + 3,
  and the branch follows mechanically from the filter plus the existing
  `if not open_runs`.
  (D4) `git status` clean is satisfied by this commit.
  The verifier also confirmed the harness change is behaviour-neutral
  (`independent_response_cancel: False` is identical to absent under the
  fail-closed `is True` rule, and `cancel_timeout_ms` is not read by
  `Wave4ResponseFlags.from_mapping`), and independently agreed with the
  ADR-0008 D6 unchanged judgement.
- Hub ruling on verifier finding D2: ACCEPTED as implemented, no change.
  `_cancel_unheard_commentary` unregisters after the cancel returns, so a
  microsecond window answers `already_terminal` instead of
  `unknown_response`, and the unregister is skipped if that call raises.
  Both are safe here for a reason worth writing down: the `phase == "final"`
  filter at the barge-in call site means a commentary entry left in
  `runtime.response_runs` — open or terminal — can never make barge-in
  ambiguous. What remains is a bounded dict entry on an exception path, and
  the entry stays in `open_by_action` for the shutdown sweep. Tightening it
  would be redesign against the card's own explicit wording, not a fix.
