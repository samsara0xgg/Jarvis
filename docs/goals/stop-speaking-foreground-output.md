# Goal: stop-speaking-foreground-output

## Goal

`ResponseCancelRequest(scope="foreground_output")` stops the speech that is
audible right now and appends `surface.playback_interrupted` with
`reason="user_stop"`, while the ResponseRun that owns it stays open and
finishes on its own — including when that run already ended and only its
audio tail is still playing.

## Why

ADR-0006 D8 (`docs/adr/0006-full-duplex-voice-session.md:445-449`) names the
gap in its own shipped-boundary list: a confirmed barge-in resolves its target
from `ResponseRunRegistry.open_runs()`, a run is unregistered the moment
generation ends (`jarvis/runtime/inherent_loop.py:1528`, `:1534`;
`jarvis/runtime/__init__.py:2409`, `:2651`), and the audio for that run is
still in the TTS queue at that instant — so "别说了" over the tail returns
`no_open_run` and the speech continues. The tail is the majority of every
answer: streaming TTS lags generation by seconds.

The mechanism to stop it is entirely built and exercised in production. What is
missing is one scope that reaches it without terminalizing the run.

## Current behavior

All `path:line` pinned at `realtime-integration` `36170ec`.

- `request_response_cancel` rejects the scope before any lookup:
  `if request.scope != "generation": return CancelRejected(reason="unsupported_scope")`
  (`jarvis/decision/response_run.py:780-781`). Its docstring at `:770-772`
  states the reason as "until a playback lease exists to make its
  `expected_playback_generation_id` meaningful".
- `make_response_cancel_callable` already admits the string
  (`scope not in ("generation", "foreground_output")` at
  `jarvis/runtime/__init__.py:1750`) and already forwards it into the request
  (`:1770`), so the rejection today comes only from L3.
- `/inherent/cancel-response` forwards `req.scope` verbatim
  (`jarvis/surface/inherent_server.py:896-914`). The `CancelResponseRequest`
  docstring at `:189-196` says `foreground_output` "needs a playback lease that
  does not exist yet" — imprecise: the lease type, the mint site and the three
  release sites all exist.
- The lease exists: `GenerationLease` / `ForegroundBusy` / `PlaybackLedger`
  (`jarvis/surface/voice_ledger.py:26-93`), minted in
  `AudioStreamPlayer.activate_generation`
  (`jarvis/surface/voice_tts.py:1072-1104`), released at exactly three sites
  (`voice_tts.py:974`, `:1208`, `:1255`).
- The stop mechanism is live and reached from **five** production call sites,
  not none: `_interrupt_active` (`jarvis/surface/voice_media.py:2774`) from
  `_suspend_for_sleep_owned` (`:1405`), from `_response_terminal` (`:1937`, on
  every `response.cancelled` / `response.failed`), from the
  `foreground_superseded` grant (`:2078`, whose `foreground_decision_callable`
  is wired unconditionally at `jarvis/runtime/inherent_loop.py:1926`) and from
  `media_owner_shutdown` (`:3112`); plus `_fail_active` (`:2741`, calling at
  `:2751`), which reaches the same `_interrupt_snapshot` (`:2935`) from every
  provider/timeout/stream failure. `reconcile_open_playback` is the
  startup-only one (single call site,
  `jarvis/runtime/inherent_loop.py:3860-3864`, inside the boot barrier).
- `_interrupt_active` already does the whole job: CAS tombstone via
  `interrupt_generation` (`voice_tts.py:1193-1216`), abort of the TTS session
  and fallback (`_cancel_active_io`, `voice_media.py:2807`), one durable
  terminal through the shared builder `_commit_terminal` (`:2994-3030`), then
  `_release_active(start_successor=False)` (`:2799-2804`) which pops the
  response from `_responses` so later chunks of that same response are dropped
  as `unregistered` (`:1894`).
- Nothing calls `_interrupt_active` with a caller-chosen `reason`, and no public
  method on `StreamingTTSPipeline` reaches it: the public surface is
  `submit_event`, `suspend_for_sleep`, `resume_after_wake`, `is_output_active`,
  `wait_until_idle`, `admit_wake_start`, `abort_wake_start`,
  `revoke_wake_starts_for_shutdown`, `request_close`, `close`.
- Two hermetic checks pin today's rejection:
  `tests/integration/test_wave4a_response_run.py:1270-1276` (HTTP body
  `{"outcome": "unsupported_scope"}` for `foreground_output` and for
  `not-a-scope`) and `:1278-1296`
  (`test_unsupported_scope_is_rejected_before_any_write`).

## Target behavior

Names below are the observable strings; the run's own vocabulary comes from
ADR-0014 D20 step 4 (`docs/adr/0014-inherent-realtime-ux.md:1219`).

- **L3 authorizes, never terminalizes.** `request_response_cancel` with
  `scope="foreground_output"` returns a new `CancelPlaybackAuthorized` outcome
  and touches neither `ResponseTerminalizer` nor `run.cancellation_token`.
  After such a call the Event Log contains zero rows of every response terminal
  type. When the run is still registered it first honours its policy: a
  `policy_hash` mismatch keeps returning `CancelRejected("policy_hash_mismatch")`,
  and `run.interrupt_policy.confirmed_playback == "ignore"` returns
  `CancelRejected("policy_ignore")` (a new member of that Literal).
- **A run that is no longer registered is authorized, not rejected.** This is
  the D8 gap and the point of the card: `registry.get(response_id) is None`
  means the run has no live policy left to forbid anything, and stopping its
  tail writes only a playback terminal. Target resolution then happens where
  D8 says it must — against the playback lease, in L5.
- **L5 stops exactly the named target.** A new public
  `StreamingTTSPipeline.stop_foreground_output(response_id) -> str`, callable
  from any non-actor thread, hops to the actor loop the way `submit_event`
  does (`asyncio.run_coroutine_threadsafe`, `voice_media.py:737`) and, on the
  actor, matches `self._active.response.response_id` against the argument
  before doing anything. Outcomes:
  - `applied` — `_interrupt_active(reason="user_stop")` returned True, so a
    durable `surface.playback_interrupted` with `reason="user_stop"` is
    committed for that `(response_id, playback_generation_id)`.
  - `stale` — no active response, an id that does not match the active one, or
    an active response already draining (`terminal_commit_pending`). Nothing
    is appended.
  - `uncertain` — `_interrupt_active` returned False (callback publication did
    not settle, or the terminal commit failed and the debt was isolated at
    `voice_media.py:2783-2789`). The audio is already stopped; the durable row
    is owed. This is the one word added to D20's four.
- **The queued lane is dropped with the active one.** After a successful stop
  the actor calls the existing `_purge_after_drain` (`voice_media.py:1974`).
  Without it the lane leaks: `_release_active(start_successor=False)` clears
  `_active` and `_output_active` and never calls `_advance_after_drain`
  (`:3055-3059`), so a queued successor would sit unstartable. Every other
  `_interrupt_active` caller already drains or purges (`:1938-1942`, `:2033`,
  `:1406-1412`). ADR-0006 D4 (`docs/adr/0006-full-duplex-voice-session.md:261`)
  is explicit that a user stop applies to the active *and queued* speech
  responses in that group.
- **Disclosed limitation: the purge leaves no durable row explaining itself.**
  A queued response silenced by someone else's stop is observable only by
  absence — no `surface.playback_started` row ever appears for it. It gets no
  playback terminal, because `_purge_after_drain` (`voice_media.py:1974`) calls
  only `ActivePlaybackRegistry.terminalize` (`:512-522`), which is in-process
  set/deque state, and a durable playback terminal is impossible for it anyway:
  `terminalize_playback` requires a non-negative `playback_generation_id`
  (`jarvis/state/lifecycle_terminal.py:230-232`) and a queued response never
  held a lease. So a user who stops A and then never hears B will find nothing
  in the ledger accounting for B's silence. Producing such a row needs a new
  event type or a lease-less terminal shape, which is outside this card; it is
  stated here so it is not lost. This card must not make it worse: the purge
  reuses the existing helper on the existing code path and adds no new silent
  drop.
- **The response does not start speaking again.** `_release_active` popped the
  response from `_responses`, so its later `surface.response_chunk` rows are
  dropped and no second `surface.playback_started` row appears for it.
- **The runtime is the only wiring.** `make_response_cancel_callable` gains one
  keyword-only `stop_foreground_output: Callable[[str], str] | None = None`,
  bound at `jarvis/runtime/inherent_loop.py:3883-3887` to the live
  `tts_pipe.stop_foreground_output` when `tts_pipe` is a
  `StreamingTTSPipeline` (`tts_pipe` is assigned at `:3782`, inside the same
  `serve_inherent` body). With it unset the callable answers
  `unsupported_scope` — the honest answer for a deployment with no playback
  actor, and what keeps every existing runtime-level fixture unchanged.
- **The HTTP surface changes by zero lines of behavior.** The handler already
  forwards `scope`; only the `CancelResponseRequest` docstring is corrected.
  `not-a-scope` still answers `unsupported_scope`.

## Affected contracts and files

- L3 `jarvis/decision/response_run.py:780-781` — the scope gate becomes a
  `foreground_output` branch; `:465-470` `CancelRejected.reason` Literal gains
  `policy_ignore`; a frozen `CancelPlaybackAuthorized` joins the
  `CancelOutcome` union at `:485`; the docstring at `:770-772` is corrected.
- L6/wiring `jarvis/runtime/__init__.py:1721-1785` — new keyword-only
  parameter, new outcome branch, docstring's returned-string list extended.
- L6/wiring `jarvis/runtime/inherent_loop.py:3883-3887` — bind the L5 callable.
- L5 `jarvis/surface/voice_media.py` — one new public method plus its actor-owned
  coroutine; reuses `_interrupt_active` (`:2774`) and `_purge_after_drain`
  (`:1974`) unchanged.
- L5 `jarvis/surface/inherent_server.py:189-196` — docstring only.
- `tests/integration/test_wave4a_response_run.py:1270-1276` and `:1278-1296` —
  both pin the old rejection and must flip.
- `docs/adr/0006-full-duplex-voice-session.md:448-449`,
  `docs/adr/0008-real-time-response-streaming.md:623-624`,
  `docs/adr/0014-inherent-realtime-ux.md:1219` — see Docs to sync.

## Boundaries and non-goals

- Layers that may change: L3 (`jarvis/decision/response_run.py`), L5
  (`jarvis/surface/voice_media.py`, `jarvis/surface/inherent_server.py`
  docstring), and the runtime wiring. `lint-imports` must stay green; L5 gains
  no import of `jarvis.decision` — the injected callable is what keeps that
  true.
- **Must not change** `jarvis/surface/voice_session.py` or
  `jarvis/surface/voice_interrupt.py`. The canary at
  `tests/canary/test_canary_audio_ingress_realtime_safety.py:83-107`
  source-scans both and fails on `interrupt_generation(`,
  `request_response_cancel`, `.duck(`, `.flush(`, `_interrupt_active`,
  `ResponseCancelRequest`, `interrupt_playback(`, `SystemAudioDucker`,
  `ActionRunner`. The input side may still stop speech only by emitting
  `BargeInSignal` into its injected callable. Do not route this card's path
  through those two modules, and do not "fix" the canary.
- **Must not change** `ResponseTerminalizer`, the `response.cancelled` reason
  vocabulary in `jarvis/shared/realtime.py`, `_interrupt_active` itself,
  `_commit_terminal`, `_release_active`, the generation CAS in
  `voice_tts.py:1193-1216`, or the `_after_drain` lane policy.
- No new config key and no new feature flag. The existing
  `realtime.response.independent_response_cancel` already gates the route.
- **Non-goal: the client wire surface.** The reason is delivery, not absence.
  `control.stop_speaking` and the `StopSpeaking` envelope are indeed zero-hit
  repo-wide, but `playback.progress(state="silenced")` is NOT unbuilt: its
  Swift half is complete and tested —
  `desktop/inherent-swift/InherentRealtime/RealtimeViewDTOs.swift:138`, `:463`,
  `RealtimeState.swift:183`, `:201-202`, with a decode test at
  `InherentCardTests/Realtime/RealtimeViewDTOTests.swift:179-184`. What is
  missing is the Python emitter (no `playback.progress` anywhere in `jarvis/`).
  It stays missing here because the transport that would deliver such a frame
  is **disabled**:
  `desktop/inherent-swift/InherentCard/RealtimeTransportV2.swift:25` is
  `static let enabled = false` and the live app uses BridgeBackend, so emitting
  the frame now would produce something nothing can receive. Nothing under
  `desktop/` may change. D20 step 1's "authenticate" is likewise out of scope;
  v1 HTTP has no auth today and this card adds none.
- **Non-goal, and a separate queued card on this lane: the interrupt fade /
  declick.** Not deferred for lack of value — deferred because it is not this
  card's bug. Interruption already hard-zeros on **five** live production call
  sites of `_interrupt_active` / `_fail_active`, which the next drafter should
  inherit rather than re-derive:
  1. `jarvis/surface/voice_media.py:1405` — `_suspend_for_sleep_owned`,
     `reason="system_sleep"`;
  2. `:1937` — `_response_terminal`, on every `response.cancelled` /
     `response.failed` that lands while its response is speaking;
  3. `:2078` — the `foreground_superseded` grant, whose
     `foreground_decision_callable` is wired **unconditionally** at
     `jarvis/runtime/inherent_loop.py:1926` (no flag);
  4. `:3112` — `media_owner_shutdown`;
  5. `:2751` — `_fail_active`, reaching the same `_interrupt_snapshot`
     (`:2935`) from every provider/timeout/stream failure.
  All five converge on the same three callback zeroing sites
  (`jarvis/surface/voice_tts.py:1555`, `:1565`, `:1580`), so the step
  discontinuity is a pre-existing, reason-independent defect there. A fix
  scoped to `reason="user_stop"` would leave four callers still popping. That
  card also inherits two constraints this one established: the fade cannot
  delay the tombstone (ADR-0006 D4,
  `docs/adr/0006-full-duplex-voice-session.md:259`), so faded samples fall
  outside `PlaybackLedger`'s submitted/accepted accounting; and the ledger
  already owns the value `"attenuated"`
  (`jarvis/surface/voice_ledger.py:22`) rather than needing new bookkeeping.
- **Non-goal: the queued siblings on this lane** — `measured_dac` cursor quality
  from `voice_tts` `time_info`, the ring-starvation counter, and the
  `_run_macos_say` single-window timeout residual. Separate cards.
- Non-goal: action cancellation, `CancelActionRequest`, and anything that
  appends `response.cancelled`.

## Rejected approaches

- **Let `foreground_output` fall through to the terminalizer with a different
  reason.** Directly violates ADR-0014 D20 steps 6-7
  (`docs/adr/0014-inherent-realtime-ux.md:1221-1223`) and would make "stop
  speaking" kill the document sibling and the answer's own completion.
- **Require `registry.get(response_id)` to be non-None for
  `foreground_output`.** Reintroduces the exact D8 gap this card exists to
  close: a run is unregistered while its audio still plays, so the tail case —
  the common case — would answer `unknown_response`.
- **Require a caller-supplied `expected_playback_generation_id` in the HTTP
  body.** No HTTP client can learn it: `surface.playback_started`
  (`voice_media.py:2148-2160`) carries it but is not on the v1 wire, and the v2
  transport is disabled. The exact-target CAS is preserved by matching
  `response_id` against the actor's own `self._active` lease *on the actor
  thread*, which is stronger than a client-supplied integer because it cannot
  be stale by the time it is compared.
- **Drive the stop through a new durable "stop requested" event row so the
  media actor picks it up in `submit_event`.** Adds an event type to L2 for a
  control action that writes its own terminal anyway, and puts SQLite commit
  latency in front of a stop the user expects to be instant.
- **Give the input side (`voice_session` / `voice_interrupt`) a direct stop
  path** — banned by the canary and by ADR-0006 D8's two-phase contract.
- **A new `PlaybackActor.interrupt_playback` public API mirroring ADR-0006 D4's
  sketch** (`docs/adr/0006-full-duplex-voice-session.md:255`). The concrete
  equivalent already exists as `_interrupt_active`; a second entry point into
  the same CAS is a parallel implementation of a live one.

## Acceptance evidence

Every criterion below names the artifact it reads. No criterion may be
satisfied by a value the implementation returns about itself.

**Hermetic H1 — L3 writes nothing (`tests/integration/test_wave4a_response_run.py`).**
`request_response_cancel(scope="foreground_output")` against a *registered,
open* run returns `CancelPlaybackAuthorized`, and
`SELECT count(*) FROM events WHERE type IN (<the response terminal types
`_RESPONSE_TERMINALS` already lists in that file>)` is `0`. The same call
against an unregistered id also returns `CancelPlaybackAuthorized` and still
counts `0`. A run whose `interrupt_policy.confirmed_playback == "ignore"`
returns `CancelRejected` with `reason == "policy_ignore"` and counts `0`.
This replaces `test_unsupported_scope_is_rejected_before_any_write` (`:1278`);
quote its before and after.

**Hermetic H2 — L5 commits the right row and only that row
(`tests/integration/test_wave2_streaming_media.py`, using that file's existing
real-pipeline-plus-real-event-log harness).** Drive one response to active
playback through the real `StreamingTTSPipeline`, call
`stop_foreground_output(response_id)` from the test thread, then assert on
committed rows via `json_extract`, not on the pipeline object:
- exactly one row with `type = 'surface.playback_interrupted'`,
  `json_extract(payload_json,'$.response_id') = <that id>` and
  `json_extract(payload_json,'$.reason') = 'user_stop'`;
- zero rows with `type = 'surface.playback_completed'` for that id;
- feed one further `surface.response_chunk` for the same response afterwards
  and assert the count of `surface.playback_started` rows for that id is
  still `1`.

**Hermetic H3 — the degenerate path is named.** In the same file, calling
`stop_foreground_output` with an id that is not the active response returns
`stale` and the count of rows whose `type LIKE 'surface.playback_%'` is
unchanged across the call. Quote the count before and after.

**Hermetic H5 — the queued lane is provably dropped, and its accounting gap is
pinned.** With response A active and response B queued behind it in the same
`response_group_id`, stop A, then let the pipeline run to quiescence and assert
on committed rows:
- zero rows with `type = 'surface.playback_started'` and
  `json_extract(payload_json,'$.response_id') = B` — B never speaks;
- zero rows of any `type LIKE 'surface.playback_%'` for B at all. This second
  assertion is the disclosed limitation stated as a pin: B is silenced with
  nothing in the ledger explaining why. If a future card gives the purge a
  durable row, this assertion is the one that must be changed, deliberately.
Both counts quoted. The check reads committed rows only; the in-process
`ActivePlaybackRegistry` state is not an acceptable substitute.

**Hermetic H4 — the HTTP body field
(`tests/integration/test_wave4a_response_run.py:1270-1276`).** The existing
loop splits: `POST /inherent/cancel-response` with
`{"scope": "not-a-scope"}` still returns body `{"outcome":
"unsupported_scope"}`, while `{"scope": "foreground_output"}` is forwarded to
the injected callable with `scope == "foreground_output"` and returns that
callable's own outcome in the body's `outcome` field.

**Live run: REQUIRED.** Daemon started from this worktree on its own runtime
root and its own port. Do not touch the owner's daemon (pid 45429, port 8009)
or InherentCard (pid 96300), and do not build, launch or delete anything under
`.claude/worktrees/realtime-live-test`. The runtime root's config must have
`realtime.streaming_output` and `realtime.response.independent_response_cancel`
on (a 404 from the route means the flag is off — fix the config, not the code).
Provider keys via `set -a; source ~/.jarvis/env; set +a`; mechanism per
`~/.jarvis-realtime-test/ask.sh` and `show-turn.sh`.

*Audio rule:* this run needs the speaker but no routing change, so do not
switch the macOS system default output device at all. If any step ends up
switching it, capture the pre-run route first, restore it in a `finally`/`trap`
on every exit path, and if the captured route is already the loopback
(`BlackHole 16ch`) restore `MacBook Pro Speakers` instead.

- **Live A — the central case, both halves of D20 in one trail.** Ask a
  question whose answer is long enough to still be generating when the first
  sentence is audible. Poll the log until `surface.playback_started` exists for
  `RESP` and neither `response.completed` nor `response.cancelled` does, then
  `POST /inherent/cancel-response {"response_id": RESP, "scope":
  "foreground_output", "reason": "user_stop"}`. Show:
  1. body `{"outcome": "applied"}`;
  2. the `surface.playback_interrupted` row for `RESP` with
     `reason == "user_stop"`, `provider == "minimax_ws_streaming"` (a
     `provider` of `macos_say` INVALIDATES the run — `_run_macos_say`
     (`voice_media.py:2503-2551`) never calls `write_generation`, so its
     terminal reports `submitted_samples = 0` and `total_samples = 0` and any
     sample comparison passes trivially as `0 == 0`), and
     `submitted_samples > 0`. Quote `submitted_samples` and `total_samples`
     as printed;
  3. `SELECT count(*) FROM events WHERE type='response.cancelled' AND
     json_extract(payload_json,'$.response_id')=RESP` is `0`;
  4. the `response.completed` row for `RESP` with an `events.id` **greater**
     than the `surface.playback_interrupted` row's `events.id`. This ordering
     is what proves the run was still open when the stop landed and finished on
     its own afterwards — half 2 of D20. Both ids quoted. (`events.id` is a
     sound column to compare on: it is `INTEGER PRIMARY KEY AUTOINCREMENT`
     (`jarvis/state/event_log.py:95`), so values strictly increase in append
     order and are never reused, and the log is append-only by trigger —
     `events_no_update` and `events_no_delete` (`:149-162`) abort any UPDATE or
     DELETE, so no row can be removed or reordered. `ts_epoch_ms` is wall clock
     and is not a substitute.);
  5. exactly one `surface.playback_started` row for `RESP`;
  6. the operator's own statement that the speaker went quiet on the POST and
     stayed quiet.
  Canary value: `RESP`, the `playback_generation_id`, and the two `events.id`
  values from (4).
- **Live B — the D8 gap, and the contrast that proves the scope is not a
  no-op.** Same shape, but wait until `response.completed` for `RESP` exists
  while audio is still playing. First `POST` the *old* scope
  (`"scope": "generation"`) and show that its body is **not** `applied`
  (`unknown_response`, since the run is unregistered) and that the speech
  continues. Then `POST` `"scope": "foreground_output"` and show body
  `{"outcome": "applied"}`, the `surface.playback_interrupted` row with
  `reason == "user_stop"` and `provider == "minimax_ws_streaming"`, and zero
  `response.cancelled` rows for `RESP`. This is the case barge-in cannot reach
  today.
- If Live A cannot be timed (generation always finishes before the first audio
  in this configuration), say so, run Live B, and **stop and report** rather
  than claiming half 2 from Live B — in Live B "no `response.cancelled`" is
  trivially true because the run was already terminal.

**Regression.** `env -u MINIMAX_API_KEY PYTHONPATH=. .venv/bin/python -m pytest
-q -m "not live_llm and not live_codex"` — the key must be unset; there is a
known flake at `tests/integration/test_wave2_streaming_media.py:2497` when it is
set. Baseline reported at launch was `1055 passed, 64 deselected` at `36170ec`,
but the branch moves under this card: **measure and quote the baseline yourself
before the first edit**, then show the final count equals that baseline plus
exactly the tests this card adds, with any other delta explained. Also show
`tests/canary/test_canary_audio_ingress_realtime_safety.py` passing.

**Gates.** `PYTHONPATH=. .venv/bin/lint-imports`, `PYTHONPATH=. .venv/bin/ruff
check .`, `PYTHONPATH=. .venv/bin/mypy --strict jarvis tests scripts tools` —
each exit 0, raw output with printed counts.

**Diff shape.** `git diff --stat` shows no file under `desktop/`, no file under
`config/`, and neither `jarvis/surface/voice_session.py` nor
`jarvis/surface/voice_interrupt.py`. The Swift suite (baseline 191) is not run
and not needed.

## Docs to sync

- `docs/adr/0006-full-duplex-voice-session.md:448-449` — the sentence
  "Stopping the tail needs the `foreground_output` scope and its playback
  lease, which is the stop-speech card's work" becomes a statement of what now
  exists. One sentence. The rest of D8's boundary list stays: barge-in itself
  still resolves its target from `open_runs()` and this card does not rewire it.
- `docs/adr/0008-real-time-response-streaming.md:623-624` — "`foreground_output`
  and its playback lease are still absent, and that remains the stop-speech
  card's work" is now false. One sentence. The scope table at `:636-638` is
  unchanged and must stay unchanged.
- `docs/adr/0014-inherent-realtime-ux.md:1219` — D20 step 4's return vocabulary
  gains `uncertain` (audio stopped, durable terminal owed) alongside `applied`
  / `stale` / `rejected`. One clause. Steps 1-3 and 5-7 are unchanged and are
  what the implementation must satisfy.
- `docs/spec.html` — judged unchanged; state that explicitly. It carries no
  `foreground_output`, `cancel-response` or stop-speaking contract (verified by
  grep at `36170ec`).
- `jarvis/decision/response_run.py:770-772` and
  `jarvis/surface/inherent_server.py:189-196` — code docstrings carrying the
  now-false "does not exist yet" claim. Corrected as part of the change, not
  listed as documentation.

## Open questions

(none)

## /goal condition

Implement `docs/goals/stop-speaking-foreground-output.md` on the current
branch. The goal is met when all of the following appear in the transcript.

(1) `git diff --stat` is shown. No file under `desktop/` or `config/` appears,
and neither `jarvis/surface/voice_session.py` nor
`jarvis/surface/voice_interrupt.py` appears.

(2) The diff of `jarvis/decision/response_run.py` is shown and it is visible
that the `foreground_output` branch returns without calling `terminalizer` and
without touching `run.cancellation_token`, and that an unregistered
`response_id` is authorized rather than rejected.

(3) Raw pytest output for the hermetic checks H1-H5 described in the card,
ending in a pass line. Each check's assertion is quoted and reads on a
committed Event Log row (`type` plus a `json_extract` payload field) or on the
HTTP response body's `outcome` field — never on the state of an object the test
constructed. `test_unsupported_scope_is_rejected_before_any_write` is quoted
before and after. H5's two counts for the queued response B are quoted, and the
transcript restates the disclosed limitation: B is silenced with no durable row
explaining why.

(4) A live run from this worktree on its own runtime root and port, never
touching the daemon on port 8009, InherentCard, or
`.claude/worktrees/realtime-live-test`, and never switching the macOS system
default output device. For Live A the transcript shows: the POST body
`{"outcome": "applied"}`; the `surface.playback_interrupted` row for the quoted
`RESP` with `reason` `user_stop`, `provider` `minimax_ws_streaming` and
`submitted_samples` greater than 0 (a `provider` of `macos_say` invalidates the
run); a count of `0` for `response.cancelled` rows on that `RESP`; the
`response.completed` row for that `RESP` with an `events.id` greater than the
interrupted row's, both ids quoted; a count of exactly `1` for
`surface.playback_started` on that `RESP`; and the operator's statement that the
speaker went quiet. Live B additionally shows that `scope="generation"` at the
same point does NOT return `applied` and does not stop the speech, and that
`scope="foreground_output"` then does. If Live A could not be timed, the run
stopped and reported instead of claiming half 2 from Live B.

(5) Raw output of `env -u MINIMAX_API_KEY PYTHONPATH=. .venv/bin/python -m
pytest -q -m "not live_llm and not live_codex"`, with the branch baseline the
session measured itself before its first edit quoted alongside, and the final
count equal to that baseline plus exactly the tests this card adds; any other
delta explained. Plus raw passing output for
`tests/canary/test_canary_audio_ingress_realtime_safety.py`.

(6) Raw output with printed counts and exit 0 for `PYTHONPATH=.
.venv/bin/lint-imports`, `PYTHONPATH=. .venv/bin/ruff check .`, and
`PYTHONPATH=. .venv/bin/mypy --strict jarvis tests scripts tools`.

(7) Each entry under Docs to sync is updated or explicitly judged unchanged,
following the rule: update the canonical document that owns a changed contract,
invariant, ownership boundary, or externally relevant behavior; do not document
what the code already makes clear; do not duplicate a fact across documents.
The three ADR edits are one sentence or clause each and the ADR-0008 scope
table is shown unchanged. If any other ADR sentence is found that the change
contradicts, the run stops and reports it instead of editing.

(8) No new config key and no new feature flag appears in the diff.

(9) Each slice is committed with the project commit skill, raw `git status`
shows a clean tree, and a Progress line per slice with its commit sha and
evidence is appended to the card.

Or stop after 45 turns.

## Progress

- Baseline measured before the first edit at `b42ef7b` (this branch, after
  merging `realtime-integration`): `1055 passed, 64 deselected`. One
  intermittent flake seen once on
  `test_typed_llm_stream.py::test_actual_sdk_commits_permitted_prefix_before_provider_completion[openai]`;
  it passes alone and on re-run. Every `path:line` in the card was re-pinned
  against this tree; all matched within a line or two, none contradicted.
- Slice 1 `3c89488` `feat(decision,runtime): authorize the foreground_output
  cancel scope`. H1
  (`test_foreground_output_scope_authorizes_without_any_write`) and H4 green;
  full suite `1055 passed, 64 deselected` (H1 replaces the old rejection test
  1:1, H4 edits an existing one, so the count is unchanged). Gates:
  lint-imports KEPT 1/1, ruff all checks passed, mypy strict 248 files clean.
  One deviation from the card's letter, deliberate: L3's
  `unsupported_scope` branch was deleted rather than kept behind the new
  `foreground_output` branch, because `mypy --strict` proves it unreachable —
  `ResponseCancelScope` is `Literal["foreground_output", "generation"]` and
  the runtime seam normalizes every other string before constructing the
  request. `CancelRejected.reason` keeps the member, which the runtime seam
  still returns.
- Slice 2 `201ce87` `feat(surface,runtime): stop one response's audible
  output on request`. H2/H3 and H5 green, 5/5 repeat runs; full suite
  `1057 passed, 64 deselected` = baseline 1055 + exactly the 2 tests this
  card adds. Gates: lint-imports KEPT 1/1, ruff all checks passed, mypy
  strict 248 files clean, canary
  `test_canary_audio_ingress_realtime_safety.py` 3 passed.
- Live run done from this worktree, daemon on port 8011, runtime root
  `~/.jarvis-lane-a-liveA`; the owner's daemon (pid 45429, port 8009) and
  InherentCard (pid 96300) were never touched, nothing under
  `.claude/worktrees/realtime-live-test` was read, built or launched, and the
  macOS default output device was never switched. Two live-only config
  choices in MY overlay (no repo `config/` change):
  `realtime.response.routine_streaming.enabled` and
  `realtime.streaming_output.speak_from_segments` on — without both,
  `response.completed` always precedes the first audible sample and Live A is
  structurally untimeable.
  - Live A `RESP287934b56f8a432687421556102067f3`: POST body
    `{"outcome": "applied"}` with the run still open (`response.completed`
    count 0 immediately before the POST); `surface.playback_interrupted`
    `events.id=1879` `reason=user_stop` `provider=minimax_ws_streaming`
    `submitted_samples=100288` `total_samples=231296`
    `playback_generation_id=13`; `response.cancelled` count `0`;
    `response.completed` `events.id=1989` > `1879`, so the run finished on
    its own after the stop; `surface.playback_started` count `1`.
    Took 3 attempts: the streaming suffix gate answers `suffix_rejected`
    (`response.failed`) on roughly a third of runs on this branch, which is
    pre-existing and independent of the stop (L3 wrote nothing).
  - Live B `RESP0def351d3abe40d9b1d1e225cdbf6e35`: with `response.completed`
    `events.id=2030` already written and the tail still playing,
    `scope="generation"` returned `{"outcome": "unknown_response"}` and 1.5s
    later the playback terminal count for that response was still `0` — the
    speech continued. `scope="foreground_output"` then returned
    `{"outcome": "applied"}` with `surface.playback_interrupted`
    `events.id=2249` `reason=user_stop` `provider=minimax_ws_streaming`
    `submitted_samples=56512`, and `response.cancelled` count `0`.
  - Owner follow-up, not a blocker: the speaker-audibility half of Live A
    item 6 ("the speaker went quiet on the POST and stayed quiet") needs the
    operator's own ear. `submitted_samples=100288` at 48 kHz is ~2.1 s of
    audio actually written to the device before the stop, which is the
    machine-checkable half.
- Docs to sync: ADR-0006 D8 and ADR-0008 sentences rewritten to state what
  now exists (one sentence each); ADR-0014 D20 step 4's vocabulary gains
  `uncertain` (one clause); the ADR-0008 scope table is untouched;
  `docs/spec.html` judged unchanged and verified by grep (0 hits for
  `foreground_output` / `cancel-response` / `stop_speaking`).
- STOPPED AND REPORTED, not edited: `docs/adr/0008-real-time-response-streaming.md`
  ~`:629-631` says "`expected_playback_generation_id` is required for
  `foreground_output`". The shipped implementation does not require it, by the
  card's own explicit rejection of that approach (no HTTP client can learn the
  value; the exact-target CAS is preserved by matching `response_id` against
  the actor's own `self._active` lease on the actor thread). That sentence is
  not in Docs to sync, so per /goal condition (7) it is reported rather than
  edited. It needs an owner decision.

