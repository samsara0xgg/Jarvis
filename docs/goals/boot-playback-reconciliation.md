# Goal: boot-playback-reconciliation

## Goal
At boot, the daemon closes every playback generation a previous process
abandoned with exactly one `surface.playback_interrupted(reason="daemon_restart")`,
satisfying ADR-0008 §4.4's third daemon-restart bullet.

## Why
ADR-0008:946 mandates "runtime asks L5 to close any active playback state; L5
PlaybackTerminalizer owns its event". Nothing implements it: `terminalize_playback`
has no caller outside the live in-process playback actor, so a generation killed
mid-playback never reaches a terminal and the log keeps a permanently open
playback identity.

Honest scope: no read path degrades today. `PlaybackHistory.fold`
(`jarvis/state/conversation_playback.py:56-94`) never requires a terminal —
`self.terminal` (:87-88, :93) only stops further mutation and never touches
`consistent`; `_Response.freeze` (`jarvis/state/conversation.py:78-96`) computes
`valid` (:84) from `playback.consistent`, never from `playback.terminal`. This
card closes a durable-truth gap the ADR mandates. It does not fix a crash, a
hang, or a wrong answer.

## Current behavior
- `terminalize_playback` (`jarvis/state/lifecycle_terminal.py:169-204`) has exactly
  one production caller: `_commit_terminal` (`jarvis/surface/voice_media.py:2941-2977`,
  called at `:2850`) inside the live in-process playback actor. None in
  `jarvis/runtime/inherent_loop.py` or `jarvis/cli/`. It is a passive primitive;
  nothing schedules it.
- The one-shot startup barrier runs two reconcilers and no third: response
  (`jarvis/runtime/inherent_loop.py:3084-3094`, via `_reconcile_open_responses_in_thread`
  at `:467-496`) then action quarantine (`:3103-3115`, via
  `_reconcile_action_quarantine_in_thread` at `:499-514`). Line numbers as of
  f914d20 — re-pin, see Boundaries.
- Live-verified orphan: `RESP2b75e178e097471487044c6f158c27bb` generation 2,
  `surface.playback_started` id 25, no terminal
  (`docs/live-burn-2026-09-05-crash-recovery.md:204-212`, recorded as "Not fixed
  under this card").
- `surface.playback_interrupted` (`jarvis/state/event_log.py:866-889`) requires
  `session_id, response_id, turn_id, playback_generation_id, heard_through_sequence,
  submitted_samples, heard_text_hash, reason`. Registry validation is key-presence
  only (`jarvis/state/event_log.py:1544-1547`); `reason` is a free-form string
  (`jarvis/surface/voice_media.py:2967` writes `reason or "unknown"`). There is no
  closed reason vocabulary to extend.

## Target behavior
- A new L5 function in `jarvis/surface/playback_recovery.py` mirrors
  `reconcile_open_responses` (`jarvis/decision/response_run.py:765-785`): it takes a
  connection, folds for orphans, terminalizes each once, and returns the closed
  identities.
- Orphan definition, queried not inferred: a `surface.playback_started` row whose
  exact `(response_id, playback_generation_id)` pair has no row of type
  `surface.playback_completed | surface.playback_interrupted | surface.playback_failed`,
  mirroring the `_existing_terminal` query at
  `jarvis/state/lifecycle_terminal.py:70-83`. `session_id` and `turn_id` ride on
  payloads but are not part of the CAS identity (`:192`).
- Each orphan gets exactly one `surface.playback_interrupted` with
  `reason="daemon_restart"`, written through `terminalize_playback`.
- Payload is reconstructed from the log alone, no live actor:
  - `session_id`, `response_id`, `turn_id`, `playback_generation_id` come from the
    orphan's own `surface.playback_started` row — all four are in its required
    payload (`jarvis/state/event_log.py:810-813`).
  - `source_event_id` MUST be that started row's own `event_uid`.
    `PlaybackHistory._start` sets `activation_uid = event.event_uid`
    (`jarvis/state/conversation_playback.py:133`) and `fold` flips `consistent = False`
    for any later row whose `source_event_id` differs (`:84-86`).
  - Heard cursor, per ADR-0008:947 ("reconstructs the last heard checkpoint"):
    carry the last `surface.playback_checkpoint` for that pair forward verbatim
    (`heard_through_sequence`, `submitted_samples`, `heard_text_hash`, `heard_text`).
    If there is none, write `heard_through_sequence=None`, `submitted_samples=0`,
    `heard_text=""`, `heard_text_hash=sha256(b"").hexdigest()`.
  - Do not put `speech_text_hash` in the payload: `_cursor`
    (`jarvis/state/conversation_playback.py:186`) compares it against the folded
    speech hash and would flip `consistent = False`.
- The replay fold stays `consistent` after reconciliation in both branches. The
  no-checkpoint payload lands on `_cursor`'s `cursor_sequence = -1` branch
  (`jarvis/state/conversation_playback.py:190-191`); the carry-forward payload keeps
  `cursor_sequence == last_sequence` and `text == last_text`, satisfying `:198-204`.
- Registered third in the one-shot startup barrier, immediately after
  `_reconcile_action_quarantine_in_thread`, matching ADR-0008 §4.4's own bullet
  order (responses, actions, playback). Same `asyncio.to_thread` own-connection
  idiom as its two siblings — `serve_inherent` runs on the event-loop thread and
  `open_event_log` uses `check_same_thread=True`, so `BEGIN IMMEDIATE` cannot
  legally run on `runtime.conn`.
- Gated exactly as the sibling response reconciler is:
  `runtime.response_flags.response_run_lifecycle`. No new flag.
- Idempotent: a second boot over the same orphan appends nothing and returns an
  empty result.

## Affected contracts and files
- L5 `jarvis/surface/playback_recovery.py` (new) — orphan query + terminalize loop.
- L2 `jarvis/state/lifecycle_terminal.py:169` `terminalize_playback` — gains a caller;
  unchanged.
- runtime `jarvis/runtime/inherent_loop.py` — a new `_reconcile_..._in_thread` helper
  beside `:467-514`, called in the startup barrier after `:3115`. RE-PIN both regions.
- tests — one hermetic integration test (see Acceptance evidence).
- `tests/scenarios/test_live_crash_recovery.py` — extend the existing rig with the
  playback-terminal assertions and the second restart.

## Boundaries and non-goals
- Layers that may change: L5 (surface), runtime, tests.
- Must not change: `_commit_terminal` or any live playback actor path in
  `jarvis/surface/voice_media.py`; the folds in
  `jarvis/state/conversation_playback.py` and `jarvis/state/conversation.py`;
  `jarvis/surface/voice_ledger.py`; the L2 primitive itself.
- RE-PIN AT LAUNCH: the A6 lifecycle-commentary card merges into `serve_inherent`
  first, appending a watcher task in the `watchers` build-out region (currently
  `jarvis/runtime/inherent_loop.py:3186-3234`). This card writes in the one-shot
  startup barrier (currently `:3081-3116`). Different regions, both additive, but
  every `inherent_loop.py` line number in this card must be re-pinned against the
  merged tree before editing.
- Non-goals: the heard-cursor / checkpoint defect (lane B's separate
  live-heard-cursor card); barge-in; sleep/wake; action-run or intent-pump
  recovery; the unbuilt `authorized_dispatch_outbox` boot reconciliation
  (ADR-0014:1769 — no reconcile function exists).
- This is a deliberately small card between two large ones. If the work needs to
  touch `voice_media.py`, the folds, or any ADR, the scope grew: stop and report.

## Rejected approaches
- Putting the reconciler in `jarvis/surface/voice_media.py` — it must run at boot on
  its own connection with no live actor; the file is 2900+ lines and is lane A's
  hotspot for the queued foreground-arbitration card; L5 already has the boot-fold
  shape at `jarvis/surface/repo_observer.py:374` (`recover_baselines`).
- Using `surface.playback_failed` as the terminal — `failed` implies a playback
  error that did not occur; the audio simply stopped mid-way, which is an
  interruption.
- Using `surface.playback_completed` — false; the generation did not finish.
- Gating it behind a new feature flag — the event type already exists and is already
  written on every normal path, the CAS already makes a duplicate impossible, and
  this only adds a caller for a bullet the ADR already mandates. A flag would
  institutionalize two divergent durable-truth behaviors.
- Inferring the orphan identity from a single event's payload convenience fields
  instead of the CAS-identity query — the 3030de1 lesson: correlate across event
  types by the actual identity, never assume a field is present on an earlier row.
  `session_id`/`turn_id` are not part of the CAS identity.

## Acceptance evidence
- Pre-change baseline: run
  `PYTHONPATH=. .venv/bin/python -m pytest -q -m "not live_llm and not live_codex"`
  BEFORE any edit and record the actual passed/deselected counts in Progress. On
  f914d20 it was 990 passed / 64 deselected; this card launches after the A6 card
  merges, so the number will differ.
- Regression: the same command after the change prints the recorded branch baseline
  at launch plus this card's new tests, with the same deselected count.
- Idempotency (R5), named: `test_boot_reconciler_closes_open_playback_exactly_once`,
  mirroring `test_restart_reconciler_closes_open_response_exactly_once`
  (`tests/integration/test_wave4a_response_run.py:1078`). Two reconciler runs against
  the same orphan leave exactly one `surface.playback_interrupted` row with
  `reason == "daemon_restart"`; the second run returns an empty result.
- Fold safety: the same test replays the log after reconciliation and asserts
  `consistent` stays true, in both the checkpoint-present and no-checkpoint branches.
- Own-connection shape: copy what
  `tests/integration/test_wave4b_action_runner.py:1316`
  (`test_boot_helper_requarantines_on_its_own_connection`) pins.
- Gates: `PYTHONPATH=. .venv/bin/lint-imports`, `PYTHONPATH=. .venv/bin/ruff check .`,
  `PYTHONPATH=. .venv/bin/mypy --strict jarvis tests scripts tools`. `PYTHONPATH=.` is
  mandatory on every one — without it a provenance test fails spuriously because the
  editable install resolves to the main checkout.
- Swift: baseline 174. No `desktop/` change is expected; run it only if `desktop/`
  is touched, as a regression check.
- Live run: REQUIRED. Reuse lane B's existing crash-recovery rig
  (`tests/scenarios/test_live_crash_recovery.py`,
  `PYTHONPATH=. .venv/bin/python -m pytest -q -s --live-llm -m live_llm
  tests/scenarios/test_live_crash_recovery.py`; `--live-llm` is required or
  `tests/conftest.py:105` skips the item). Do not build a new rig.
  Canary: SIGKILL a real daemon mid-playback, restart, then restart a SECOND time.
  The transcript must quote, as raw rows and values:
  - the killed pid and the orphan `(response_id, playback_generation_id)` pair;
  - the `surface.playback_started` row id and `event_uid` for that pair;
  - after restart 1: the full raw payload JSON of the single
    `surface.playback_interrupted` row for that pair, showing
    `"reason":"daemon_restart"`, and its `source_event_id` equal to the started
    row's `event_uid`; plus the `SELECT COUNT(*)` for that pair = 1;
  - after restart 2: the same count still = 1 and zero new rows appended for that
    pair;
  - the audio lines: captured pre-run default output device, the switch, and the
    restored device with `restored=True`.

  Audio rule, verbatim: if the run switches the SYSTEM default output device,
  capture the pre-run route first, restore it in a finally/trap on every exit path,
  and if the captured route is ALREADY the loopback ("BlackHole 16ch") restore
  "MacBook Pro Speakers" instead; skip the switch entirely when the run needs no
  audio. Two overlapping lanes once left the owner with no speaker output.
  The existing fixture `silent_output_device`
  (`tests/scenarios/test_live_crash_recovery.py:336-386`) captures `before` at
  `:354-359` and restores it verbatim at `:371-376`, so it does not yet satisfy the
  already-loopback clause; it was observed capturing "BlackHole 16ch" during run 8.

## Docs to sync
- `docs/adr/0008-real-time-response-streaming.md:946` — no change expected; the bullet
  already mandates this and the code now satisfies it. Judge explicitly unchanged.
- `docs/live-burn-2026-09-05-crash-recovery.md:204-212` — finding 2 records this as an
  open defect ("Not fixed under this card"). Record that it is now closed and which
  live run proves it.
- `docs/spec.html` — only if a sentence there states the playback terminal is written
  solely by the live actor and is now wrong. Otherwise judge explicitly unchanged.

## Open questions
(none)

## /goal condition

The transcript shows all of the following as raw command output, pasted in full and
not summarized:

1. Pre-change baseline: the raw output of
`PYTHONPATH=. .venv/bin/python -m pytest -q -m "not live_llm and not live_codex"`
run BEFORE any edit, with its passed/deselected counts stated as the branch baseline.
2. Regression: the same command after the change, raw output shown, passing with the
recorded baseline count plus this card's new tests and the same deselected count.
3. Gates, each raw: `PYTHONPATH=. .venv/bin/lint-imports` printing the contract KEPT;
`PYTHONPATH=. .venv/bin/ruff check .` clean; `PYTHONPATH=. .venv/bin/mypy --strict
jarvis tests scripts tools` printing Success. `PYTHONPATH=.` is visible in every
command line.
4. Idempotency proof: raw passing output of the new hermetic test
`test_boot_reconciler_closes_open_playback_exactly_once`, and the transcript states
its assertions: after the first reconciler run exactly one
`surface.playback_interrupted` row exists for the orphan
`(response_id, playback_generation_id)` pair with `reason == "daemon_restart"`, and
the second run returns an empty result and adds no row.
5. Fold safety: raw test output showing a replay of the log after reconciliation still
reports `consistent` true, in both the checkpoint-present and no-checkpoint branches.
6. Live canary: raw output of the live crash-recovery run, ending in a pass line,
quoting the killed daemon pid and the orphan `(response_id, playback_generation_id)`
pair; the `surface.playback_started` row id and `event_uid` for that pair; after the
FIRST restart, the full raw payload JSON of exactly one `surface.playback_interrupted`
row for that pair showing `"reason":"daemon_restart"` with `source_event_id` equal to
the started row's `event_uid`, plus the row count for that pair = 1; after a SECOND
restart, that count still = 1 and zero new rows for that pair. The audio lines are
also quoted: the captured pre-run default output device, the switch, and the restored
device with `restored=True`.
7. Swift: stated explicitly — either `desktop/` was not touched, so no Swift run was
needed, or raw Swift output shows 174 passing.
8. Docs: each entry under "Docs to sync" is shown as updated or explicitly judged
unchanged with a reason. Where the change altered a documented contract, invariant,
ownership boundary, or externally relevant behavior, the canonical document that owns
that fact was updated; no fact was duplicated across documents, and nothing the code
already makes clear was documented.
9. `git status` shows a clean tree, and each committed slice is named under Progress
with its sha.

Stop and report rather than redesigning if the card contradicts the repository, or if
the change turns out to need edits to `jarvis/surface/voice_media.py`, the folds in
`jarvis/state/conversation_playback.py` or `jarvis/state/conversation.py`, or any ADR.

Or stop after 40 turns.

## Progress
- (empty)
