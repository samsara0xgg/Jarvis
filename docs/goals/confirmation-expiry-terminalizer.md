# Goal: confirmation-expiry-terminalizer

## Goal
A live confirmation that passes its `expires_at_ms` reaches a durable
`confirmation.expired` terminal — appended by a fourth `lifecycle_terminal`
sibling, driven by a runtime sweep timer and re-driven once at boot, all behind a
default-false flag — so an idle connected panel is cleared by a committed row
instead of by its own clock, while today's lazy read-time expiry stays exactly as
it is.

## Launch gate — READ THIS FIRST

**This card launches only AFTER the sibling card `action-and-confirmation-projections`
(C6) has merged into `realtime-integration`.** Two parts of this card depend on what
C6 actually shipped, and both are marked **RE-PIN AT LAUNCH** below:

- **R11 re-pin** — whether C6's `ConfirmationCleared` view mutation carries a
  `reason` field on the Python side.
- **R12 re-pin** — the merged shape of `_fold_pending_confirmations`, which C6's own
  boundary said it would not change.

At each marker: verify the merged shape in the branch first. If C6 shipped something
incompatible with what the marker states, **STOP and report — do not redesign.**

If `action-and-confirmation-projections` is not in this branch's history, stop and
report before touching any code.

## Why
ADR-0014 D14 (`docs/adr/0014-inherent-realtime-ux.md:998-1019`) supersedes ADR-0012
D4's "**Lazy TTL** … No expiry event, no timer" clause
(`docs/adr/0012-confirmation-flow.md:96`) for confirmations that are live. C6 shipped
the projection but recorded the gap as an accepted consequence
(`docs/goals/action-and-confirmation-projections.md:250-256`): "with no further row
committed, an idle panel keeps showing an expired ask." C6 also names this card as its
own follow-up (`:334-338`). This card commits that row.

The reason a durable row is needed at all is that a projection cannot invent one: the
fold has no clock, and a client's own clock is not authority. Only a committed event
makes "this ask is over" true for every reader, including one that connects an hour
later.

## Current behavior
- Expiry is judged at read time, never stored: `PendingConfirmationSlot.is_live`
  returns `self.state == "pending" and now_ms < self.expires_at_ms`
  (`jarvis/state/projections.py:1450-1461`; the docstring at `:1451-1460` says it is
  "judged here, at read time" and never read from a hidden clock).
- Exactly four call sites read it: `jarvis/state/authorized_dispatch_outbox.py:130`
  (revalidates inside the atomic accept/dispatch transaction),
  `jarvis/decision/pre_route.py:120` (routes the turn to the confirmation-answer path),
  `jarvis/decision/__init__.py:1210` (gates `match_confirm_grammar`),
  `jarvis/decision/packet.py:461` (`format_pending_confirmation_note`).
- `_fold_pending_confirmations` (`jarvis/state/projections.py:1512-1572`) folds only
  `confirmation.requested` / `.accepted` / `.rejected` / `gate.evaluated`.
  `PendingConfirmationState` (`:1383-1389`) has five members and no `expired`.
- The TTL is config-only: `confirmation.ttl_ms: 600000` (`config/jarvis.yaml:138-139`),
  read by `_confirmation_ttl_ms` (`jarvis/runtime/__init__.py:548`) with the fallback
  constant `_FALLBACK_CONFIRMATION_TTL_MS` (`:228`), stamped once at mint time as
  `expires_at_ms = _now_epoch_ms() + ctx.confirmation_ttl_ms`
  (`jarvis/decision/__init__.py:3915`). Nothing ever revisits that deadline.
- The atomic terminal primitive already exists in the exact shape D14 describes:
  `_terminalize` (`jarvis/state/lifecycle_terminal.py:118-166`) — one
  `BEGIN IMMEDIATE`, an existing-terminal CAS via `_existing_terminal` (`:63-115`), one
  `append_event_in_transaction`, commit, publish. Its three siblings are
  `terminalize_playback` (`:169`), `terminalize_response` (`:207`),
  `terminalize_action` (`:236`); all three return
  `TerminalOutcome = TerminalCommitted | AlreadyTerminal`
  (`jarvis/shared/realtime.py:297-314`). **There is no `Stale` type anywhere in
  `jarvis/`** — the closest naming precedent is `StalePlaybackGeneration`
  (`jarvis/surface/voice_ledger.py:38`).
- The timer precedent is `_supervisor_sweep_task`
  (`jarvis/runtime/inherent_loop.py:2438-2456`): `while True` / `await asyncio.sleep`
  / call the plain function `_run_supervisor_sweep` (`:2283`), wrapped for
  `CancelledError`. It is started by `_start_sweep_control_plane` (`:2460`) from
  `serve_inherent` (`:3239-3241`), cancelled by the shared watcher teardown
  (`:3292-3294`), and tested hermetically by importing and calling
  `_run_supervisor_sweep` **directly** — no sleep, no task
  (`tests/integration/test_wave5_background_actions.py:61`, `:1084`).
- Boot reconciliation inside `serve_inherent` runs exactly two reconcilers today, both
  through `asyncio.to_thread` with their own `open_event_log` connection:
  `_reconcile_open_responses_in_thread` (helper `:467-495`, call site `:3082-3094`) and
  `_reconcile_action_quarantine_in_thread` (helper `:499-514`, call site `:3097-3115`).
- Registry: `EventTypeSchema` (`jarvis/state/event_log.py:204`), `_REGISTRY_ENTRIES`
  (`:243`), `_REGISTRY_MAP` (`:1176`). Siblings: `confirmation.requested` (`:1107`,
  `owner_layer="L3"`, `actor="jarvis_runtime"`), `confirmation.accepted` (`:1124`),
  `confirmation.rejected` (`:1135`). The comment at `:1120-1123` records the convention
  that `source_event_id` is an `emit_event` **column**, not a payload key.
- Layer contract: `.importlinter:20-30` — `cli / runtime / decision | execution |
  surface | deployment / state / constitution | shared`. Every existing timer and
  reconciler lives under `jarvis/runtime/`.
- Shipped Swift DTO drift, pre-existing and **not this card's**: `ConfirmationUpsert`
  (`desktop/inherent-swift/InherentRealtime/RealtimeViewDTOs.swift:284-303`) has no
  `requested_at_ms` and renames three D14 fields (`action_summary`→`summary`,
  `target_label`→`target`, `revision_cursor`→`revision`). `ConfirmationCleared` is at
  `:309-319` and `ConfirmationClearReason` at `:100-105` — the latter **already has an
  `expired` case**, so nothing on the Swift side needs to change for this card.

## Target behavior
- A new registered event type `confirmation.expired`: `owner_layer="L3"` (matching its
  three siblings), `actor="jarvis_runtime"`, `required_payload=("confirmation_id",
  "expired_at_ms")`, `optional_payload=()`, `schema_version=1`. `source_event_id` on the
  appended row is the **exact** `confirmation.requested` event's uid, carried in the
  column, never in the payload.
- A fourth sibling `terminalize_confirmation` in `jarvis/state/lifecycle_terminal.py`,
  reusing `_terminalize`'s single `BEGIN IMMEDIATE` and its existing-terminal CAS over
  the confirmation terminal set `{confirmation.accepted, confirmation.rejected,
  confirmation.expired}` keyed on `payload.confirmation_id`. It returns
  `TerminalCommitted | AlreadyTerminal | StaleConfirmation`.
- **Exactly one** terminal row can ever exist for one `confirmation_id`. A second call
  — from the timer, from a boot reconciler, from a concurrent accept — returns
  `AlreadyTerminal` carrying the row that won, and appends nothing.
- `StaleConfirmation` is returned when, inside that same transaction, the log's newest
  `confirmation.requested` row is no longer the one the caller folded (its `events.id`
  differs from the caller's `expected_revision`). This is the single-slot rule of
  `_fold_pending_confirmations` (`:1516-1521` — a new `requested` unconditionally
  replaces the slot) evaluated at CAS time: the slot the sweep meant to expire has been
  superseded, so nothing is appended.
- `PendingConfirmations` folds `confirmation.expired` whose `confirmation_id` matches
  the current slot into a new `PendingConfirmationState` member `"expired"`; a
  non-matching id changes nothing, exactly as the accepted/rejected branches already
  behave (`:1550-1566`). **RE-PIN AT LAUNCH (R12)**: confirm C6 left
  `_fold_pending_confirmations` unchanged as its boundary promised; if C6 reshaped the
  fold, verify the merged shape before adding the branch, and STOP and report if the
  branch cannot be added additively.
- `is_live`'s body is **not edited**. It returns False for the new state by
  construction, because it already tests `state == "pending"`. That is the whole reason
  R6 holds without touching any of the four readers.
- A runtime sweep: a plain function that folds the current slot and, when it is
  `pending` and the caller-supplied `now_ms` is at or past `expires_at_ms`, calls
  `terminalize_confirmation`. It logs and swallows every failure, exactly as
  `_run_supervisor_sweep` does (`inherent_loop.py:2283-2296`).
  **The sweep's write must not run on the event-loop thread.** `runtime.conn` is opened
  on the loop thread with `check_same_thread=True`, and the boot reconciler's own
  docstring records that a `BEGIN IMMEDIATE` "could not legally run on it"
  (`inherent_loop.py:470-479`). `_run_supervisor_sweep` is allowed on the loop thread
  only because `sweep_overdue_actions` is "a bounded typed fold over the log, not a
  blocking call" (`:2292-2294`) — that is a READ precedent and does not license a write
  sweep. So the expiry sweep performs its terminalize through the same
  `asyncio.to_thread` + `open_event_log` offload the boot reconciler uses; the fold that
  decides whether anything is due may stay a loop-thread read. If the session finds that
  `sweep_overdue_actions` does in fact hold a write transaction on the loop thread, that
  is a pre-existing condition to report as a follow-up, not a pattern to copy.
- A boot reconciler as a **third** sibling in `serve_inherent`'s boot block, ordered
  **after** both existing reconcilers, using the same `asyncio.to_thread` +
  `open_event_log` idiom. Two boots against the same expired-but-unterminalized
  confirmation leave exactly one terminal row.
- The panel is cleared by the durable row, not by its clock: a folded
  `confirmation.expired` produces one `confirmation.cleared` view mutation with reason
  `expired`, at that row's cursor as `revision`. **RE-PIN AT LAUNCH (R11)**: verify that
  C6's merged `ConfirmationCleared` projection carries a `reason` field. If it does,
  wire the new event through it with `reason="expired"`. If it does **not**, STOP and
  report — do not add a Swift-visible field; that belongs to the separate Swift DTO
  card.
- With the flag off, no sweep task is created, no boot reconciler runs, and the event
  log is byte-identical to today.

## Affected contracts and files
- **L2** `jarvis/state/event_log.py:_REGISTRY_ENTRIES` (`:243`) — one
  `EventTypeSchema` for `confirmation.expired`, placed immediately after
  `confirmation.rejected` (`:1135-1145`).
- **L2** `jarvis/state/lifecycle_terminal.py` — a `_CONFIRMATION_TERMINALS` frozenset
  beside the three existing ones (`:26-43`), a `"confirmation"` branch in
  `_existing_terminal` (`:63-115`) keyed on `json_extract(payload_json,
  '$.confirmation_id')` over the three confirmation terminals, and
  `terminalize_confirmation` as a fourth sibling exported from `__all__` (`:265-274`).
  D14's expected-revision precondition is the one thing the three existing siblings do
  not have; it must be evaluated **inside the same transaction** — the smallest way in
  is one optional keyword-only precondition on `_terminalize` that the three existing
  siblings do not pass and whose absence leaves their behavior identical. No second
  transaction, no new module, no `ConfirmationTerminalizer` class.
- **shared** `jarvis/shared/realtime.py` — `LifecycleOwner` (`:18`) widened with
  `"confirmation"`, and a new frozen `StaleConfirmation` dataclass beside
  `AlreadyTerminal` (`:306-312`). The `TerminalOutcome` union (`:314`) itself is
  **unchanged**; `terminalize_confirmation`'s return annotation is
  `TerminalOutcome | StaleConfirmation`.
- **L2** `jarvis/state/projections.py` — `PendingConfirmationState` (`:1383-1389`)
  gains `"expired"`; `_fold_pending_confirmations` (`:1512`) gains one
  `confirmation.expired` branch and its fold rule is recorded in the existing docstring
  rule list (`:1515-1543`).
- **L2 / view** the module C6 shipped its `ConfirmationCleared` mutation in (C6's card
  names `jarvis/state/inherent_view.py`) — one clear with reason `expired` driven by
  the durable row. **RE-PIN AT LAUNCH (R11).**
- **L6/runtime** `jarvis/runtime/inherent_loop.py` — the plain sweep function, its
  `asyncio` task copied from `_supervisor_sweep_task` (`:2438-2456`), the task appended
  to `watchers` so the existing teardown at `:3292-3294` cancels it with no new
  teardown path, and a third boot reconciler after `:3115`.
- **L6/runtime** `jarvis/runtime/__init__.py` — a sweep-interval reader beside
  `_confirmation_ttl_ms` (`:548`) with its fallback constant beside
  `_FALLBACK_CONFIRMATION_TTL_MS` (`:228`), and the flag carried to `serve_inherent`.
  One bool; do **not** add a new flags dataclass for a single key.
- **config** `config/jarvis.yaml` — `realtime.confirmation.durable_expiry.enabled:
  false`, a new block beside `realtime.response` / `realtime.actions` /
  `realtime.input` / `realtime.inherent`, additionally requiring `realtime.enabled:
  true` and downgrading once with one warning otherwise, in the wording already used at
  `:288-296`. Plus `confirmation.expiry_sweep_interval_s: 30` under the **existing
  top-level** `confirmation:` block (`:138-139`), clamped to a floor of 5 seconds with
  one warning. `confirmation.ttl_ms` is reused, never redefined.
- **tests** one new hermetic test file for the primitive/fold/races, plus sweep and
  boot-reconciler cases in the runtime integration suite.

## Boundaries and non-goals
- Layers that may change: `state`, `shared`, `runtime`, `config`. No `decision`, no
  `execution`, no `surface` behavior change beyond the R11 clear.
- **Must not change:** `PendingConfirmationSlot.is_live`'s body
  (`jarvis/state/projections.py:1450-1461`) or any of its four readers
  (`authorized_dispatch_outbox.py:130`, `decision/pre_route.py:120`,
  `decision/__init__.py:1210`, `decision/packet.py:461`). Lazy read-time expiry remains
  the truth in the window between the deadline and the timer firing. Durable expiry is
  purely additive.
- **Must not change:** any file under `desktop/inherent-swift/`. The shipped
  `ConfirmationUpsert` drift (`RealtimeViewDTOs.swift:284-303` — no `requested_at_ms`,
  three renamed D14 fields) is pre-existing and owned by the separate Swift DTO card.
  `ConfirmationClearReason` already carries `expired` (`:100-105`), so nothing is
  needed here. Swift is a pure regression check.
- **Must not change:** the `TerminalOutcome` union, the three existing terminalize
  siblings' behavior, `confirmation.requested/accepted/rejected` schemas, the v2
  envelope shapes, or the flow-control lanes.
- Non-goals: removing or weakening lazy expiry; making any of the four `is_live`
  readers depend on the new event; a multi-slot pending-confirmation model; a
  `confirmation.superseded` event (ADR-0012's no-extra-event supersede rule stands);
  client-side expiry rendering; any change to how `expires_at_ms` is minted.

## Rejected approaches
- **Deleting lazy expiry and making the durable row the only truth** — R6. Between the
  deadline and the next sweep tick the row does not exist yet; a reader that trusted
  only the row would dispatch an expired ask.
- **Widening the shared `TerminalOutcome` union with the stale variant** — it is
  consumed by L4 (`jarvis/execution/action_runner.py:1053`, `:1148`, `:1394`) and L3
  (`jarvis/decision/response_run.py:494`, `:543`, `:571`, `:598`), which would each be
  forced to handle a case they can never receive. A confirmation-local union costs
  nothing and ripples nowhere.
- **A `ConfirmationTerminalizer` class** — D14 names the *role*, not a class. The
  module already owns three function siblings over one `_terminalize`; a class for one
  function is scaffolding.
- **A new scheduling abstraction / generic expiry service** — R4. Copy
  `_supervisor_sweep_task` and stop.
- **Emitting `confirmation.expired` from the fold** — a fold has no clock and must not
  write. This is exactly the failure mode C6 recorded and deferred.
- **Putting the timer or reconciler in `state` or `decision`** — `runtime/` is the only
  cross-layer wiring point (`.importlinter:20-30`), and every existing timer and
  reconciler already lives there.
- **Defaulting the flag true** — this card writes durable rows, and a row cannot be
  unwritten.
- **Adding `requested_at_ms` or renaming the Swift DTO fields while here** — R10.

## Acceptance evidence

Every command below runs with `PYTHONPATH=.`. This is mandatory, not stylistic:
without it a provenance test fails spuriously because the editable install resolves to
the main checkout instead of this worktree.

- **Baseline, recorded BEFORE any edit:** run
  `PYTHONPATH=. .venv/bin/python -m pytest -q -m "not live_llm and not live_codex"` on
  the branch as launched and print the pre-change count. On `f914d20` (before C6 merged)
  this was `990 passed / 64 deselected`; the real baseline is whatever C6's merge
  leaves, so the recorded number — not 990 — is what the final run is compared against.
- **Regression:** the same pytest command after the change prints
  *the recorded branch baseline plus this card's new tests*, with zero failures.
- **Regression (Swift):** `bash scripts/test_inherent_swift.sh` prints 174 passed, 0
  failures, and `git diff --stat -- desktop/` prints empty. R10 forbids Swift changes,
  so any nonzero Swift diff is a failure of this card, not a Swift finding.
- **Gates:** `PYTHONPATH=. .venv/bin/lint-imports`,
  `PYTHONPATH=. .venv/bin/ruff check .`, and
  `PYTHONPATH=. .venv/bin/mypy --strict jarvis tests scripts tools` each exit 0 with
  their printed counts.
- **Positive — the durable row:** a confirmation past its deadline yields exactly one
  `confirmation.expired` row whose payload holds `confirmation_id` and `expired_at_ms`
  and nothing else, whose `source_event_id` column equals the `confirmation.requested`
  row's `event_uid`, and whose registered `owner_layer` is `L3`.
- **Positive — AlreadyTerminal race (R7):** the terminalizer is called for a
  confirmation that was just accepted; it returns `AlreadyTerminal` carrying the
  `confirmation.accepted` event, and the printed value of
  `SELECT COUNT(*) FROM events WHERE type IN ('confirmation.accepted',
  'confirmation.rejected','confirmation.expired') AND
  json_extract(payload_json,'$.confirmation_id') = ?` is **1**.
- **Positive — Stale race (R7):** a newer `confirmation.requested` lands between the
  sweep's fold and its CAS; the call returns `StaleConfirmation` and the row count for
  the original id is unchanged.
- **Positive — fold (R12):** folding a `confirmation.expired` whose id matches moves
  the slot to `"expired"` and `is_live(now_ms)` is False for it; a `confirmation.expired`
  naming a superseded id leaves the current slot untouched.
- **Positive — R6 pin:** the printed diff shows no edit to `is_live`'s body and no edit
  to `authorized_dispatch_outbox.py`, `decision/pre_route.py`, `decision/__init__.py`
  or `decision/packet.py`.
- **Positive — sweep body is directly callable (R4):** the test imports the sweep
  function and calls it with an injected `now_ms`, with **no `asyncio.sleep` and no
  task**, exactly as `test_wave5_background_actions.py:61,:1084` calls
  `_run_supervisor_sweep`; one call writes one row, a second call writes none.
- **Positive — boot idempotency (R5):** the boot reconciler is run **twice** against
  the same expired-but-unterminalized confirmation; the terminal-row count for that
  `confirmation_id` is printed after each run and is **1** both times.
- **Positive — R11 clear:** a folded `confirmation.expired` produces one
  `confirmation.cleared` mutation with reason `expired` at that row's cursor as
  `revision`. **RE-PIN AT LAUNCH.**
- **Flag-off byte-identity (R9), proven the way A6 (`lifecycle-commentary`) and C3
  (`sequencer-and-snapshot`) proved theirs:** drive the *same* confirmation scenario
  twice — once against a `git archive` export of the pre-change commit, once against
  this worktree with `realtime.confirmation.durable_expiry.enabled` absent from config
  — dump both event logs as
  `SELECT id, type, payload_json, source_event_id, correlation_json FROM events ORDER BY id`,
  and show `diff` between the two dumps printed **empty**. The transcript must
  additionally show that the flag-off run's watcher list contains no expiry task and its
  boot log contains no third reconciler line.

- **Live run: REQUIRED.** The behavior is a durable row appearing with no user action
  and no LLM involvement, driven by a real wall clock and a real daemon lifecycle —
  exactly the thing a hermetic test with an injected `now_ms` cannot demonstrate. A
  short-TTL live daemon run is the canary.

  Start a daemon from this worktree on its own runtime root and a port other than 8006,
  with an overlay carrying `realtime.enabled: true`,
  `realtime.confirmation.durable_expiry.enabled: true`, `confirmation.ttl_ms: 15000`,
  and `confirmation.expiry_sweep_interval_s: 5`. Ask one question that routes to a
  `confirm_required` tool (`write_file`, ADR-0012's L3 tool). Then send **nothing** for
  at least 30 seconds.

  Quote as canary values, in one chain:
  1. the `confirmation.requested` row — its `events.id`, its `event_uid`, its
     `payload.confirmation_id`, its `payload.expires_at_ms`;
  2. the `confirmation.expired` row that appeared with no further input — its
     `events.id` (strictly greater than the requested row's), its
     `payload.confirmation_id` byte-equal to the requested row's, its
     `payload.expired_at_ms` at or past `expires_at_ms`, its `source_event_id` column
     byte-equal to the requested row's `event_uid`, and its `actor` column
     `jarvis_runtime`;
  3. the printed terminal-row count for that `confirmation_id` — **1**;
  4. the daemon log line the expiry sweep emitted;
  5. a restart of the daemon against the **same** runtime root, after which that count
     is printed again and is **still 1** — the boot reconciler's live idempotency;
  6. an explicit statement that no input of any kind was sent between rows 1 and 2.

  **Audio rule (verbatim, applies to this run):** if the run switches the SYSTEM
  default output device, capture the pre-run route first, restore it in a finally/trap
  on every exit path, and if the captured route is ALREADY the loopback
  ("BlackHole 16ch") restore "MacBook Pro Speakers" instead; skip the switch entirely
  when the run needs no audio. This run submits text over HTTP and reads the event log,
  so it needs no audio: skip the switch entirely and state that the system default
  output device was never touched.

## Docs to sync
- `docs/adr/0012-confirmation-flow.md` §10 (Implementation errata and reconciliations,
  `:214`; existing subsections run `10.1`-`10.7`, `:220`-`:353`) — add **one sentence**
  as an erratum entry recording that ADR-0014 D14 supersedes D4's "No expiry event, no
  timer" clause (`:96`) for live confirmations, while fold-time judgement remains the
  read-time truth. Do **not** edit the D4 bullet itself — §10 is this ADR's amendment
  channel and the approved text stays unedited, matching ADR-0011's stated convention
  (`docs/adr/0011-tool-surface-v1.md:307`). Do **not** restate D14's rule text here; a
  fact lives in one document.
- `docs/adr/0014-inherent-realtime-ux.md` D14 (`:998-1019`) — C6 added a sentence there
  saying the `confirmation.expired` event, `ConfirmationTerminalizer`, the runtime timer
  and boot reconciliation are **not yet built** and belong to
  `confirmation-expiry-terminalizer` (C6's own Docs-to-sync entry,
  `docs/goals/action-and-confirmation-projections.md:441-445`). This card builds them,
  so that sentence must be replaced by one recording that they **are** built and naming
  where — the event in the registry, `terminalize_confirmation` in
  `jarvis/state/lifecycle_terminal.py`, the sweep and boot reconciler in
  `jarvis/runtime/inherent_loop.py`, behind
  `realtime.confirmation.durable_expiry.enabled`. D14's rule text itself is not edited.
  **RE-PIN AT LAUNCH**: locate C6's actual sentence in the merged file before editing.
- `docs/spec.html` — none. The spec owns what the system must satisfy; D14 already owns
  this rule and the code makes the rest clear. Judge it explicitly unchanged rather than
  duplicating D14 into it.

These are the only two doc edits this card expects.

## Open questions
(none)

## /goal condition

Implement docs/goals/confirmation-expiry-terminalizer.md on the current branch. Read it fully first. If `action-and-confirmation-projections` is not already merged into this branch, stop and report. The goal is met when the transcript shows: (1) a diff adding a `confirmation.expired` registry entry (owner_layer L3, actor jarvis_runtime, payload exactly confirmation_id and expired_at_ms), a fourth `terminalize_confirmation` sibling in jarvis/state/lifecycle_terminal.py returning TerminalCommitted, AlreadyTerminal or a new StaleConfirmation, one `confirmation.expired` branch in `_fold_pending_confirmations`, and a plain sweep function plus its asyncio task and a third boot reconciler in jarvis/runtime/inherent_loop.py behind `realtime.confirmation.durable_expiry.enabled` (default false) — changing no file under desktop/inherent-swift/ and not editing `PendingConfirmationSlot.is_live` or its four readers; (2) raw pytest output ending in a pass line covering: one `confirmation.expired` row carrying only confirmation_id and expired_at_ms, its source_event_id the `confirmation.requested` event_uid; the AlreadyTerminal race, where the terminalizer runs after an accept and the printed count of accepted/rejected/expired rows for that confirmation_id is 1; the Stale race, where a newer `confirmation.requested` makes the call return StaleConfirmation and append nothing; the fold moving the slot to `expired` with `is_live` false while a non-matching id changes nothing; the sweep body called directly with an injected now_ms, no sleep and no task, writing one row and none on a second call; and boot idempotency, two reconciler runs against the same expired confirmation each printing exactly one terminal row; (3) the flag-off byte-identity proof: the same scenario driven against a `git archive` export of the pre-change commit and against this worktree with the flag absent, both event logs dumped as id, type, payload_json, source_event_id ordered by id, `diff` printed empty, and no expiry task or third reconciler line in the flag-off run; (4) the pre-change branch baseline count recorded before any edit, then raw output of `PYTHONPATH=. .venv/bin/python -m pytest -q -m "not live_llm and not live_codex"` showing that recorded baseline plus this card's new tests, zero failures; (5) raw output of `bash scripts/test_inherent_swift.sh` showing 174 passed, 0 failures, and `git diff --stat -- desktop/` empty; (6) raw output of `PYTHONPATH=. .venv/bin/lint-imports`, `PYTHONPATH=. .venv/bin/ruff check .` and `PYTHONPATH=. .venv/bin/mypy --strict jarvis tests scripts tools`, each exiting 0 with printed counts; (7) a live run — daemon from this worktree, own runtime root, port other than 8006, flag on, `confirmation.ttl_ms: 15000`, `confirmation.expiry_sweep_interval_s: 5`, one `write_file` question then no further input — quoting the `confirmation.requested` row's events.id, event_uid, confirmation_id, expires_at_ms, then the `confirmation.expired` row that appeared unprompted with a strictly greater events.id, byte-equal confirmation_id, expired_at_ms at or past expires_at_ms, source_event_id equal to that event_uid, actor jarvis_runtime, a printed terminal-row count of 1, a restart against the same runtime root leaving it 1, plus statements that no input was sent between the two rows and the system default output device was never switched; (8) both doc edits made — the ADR-0012 §10 erratum sentence and the replaced ADR-0014 D14 "not yet built" sentence — with docs/spec.html judged unchanged, following the rule: update the canonical document that owns a changed contract, invariant, ownership boundary or externally relevant behavior; do not document what the code makes clear; do not duplicate a fact across documents; (9) each slice committed with the project commit skill and `git status` clean; (10) one Progress line per slice. If the card contradicts the repository, stop and report instead of redesigning. Or stop after 70 turns.

## Progress
- launch re-pins — merge of `realtime-integration` into `lane/c` was a
  fast-forward to `8b63a05`; branch baseline recorded there before any edit:
  `1041 passed / 64 deselected` in 49.02s. **R11 RESOLVED in favour**:
  `ConfirmationCleared` carries `reason: ConfirmationClearReason`
  (`jarvis/state/inherent_view.py:332`) and the presenter serializes it
  (`jarvis/surface/inherent_presenter.py:150`); `ConfirmationClearReason`
  already lists `expired` (`:137`). No STOP branch taken. **R12 RESOLVED in
  favour**: C6 left `_fold_pending_confirmations` unchanged — single-slot
  replace at `projections.py:1549-1550`, id-matching accepted/rejected at
  `:1551-1564` — so the new branch is purely additive.
- slice 1 the L2 primitive — b1772eb — `StaleConfirmation` +
  `LifecycleOwner` widened (shared/realtime.py, `TerminalOutcome` union
  unchanged); `confirmation.expired` registered L3 / `jarvis_runtime` /
  `(confirmation_id, expired_at_ms)`; `_CONFIRMATION_TERMINALS`, a
  confirmation branch in `_existing_terminal`, and `terminalize_confirmation`
  as a fourth sibling. `_terminalize` gained one optional keyword-only
  `precondition` hook evaluated inside its `BEGIN IMMEDIATE`; unpassed, the
  PEP-695 type parameter is unsolved and the three existing siblings' return
  type collapses to `TerminalOutcome`, so their signature and behavior are
  identical. 4/4 new checks, lint-imports KEPT (1/1), ruff clean, mypy strict
  241 files.
- slice 2 the two folds — 737d142 — `PendingConfirmationState` gains
  `"expired"` and `_fold_pending_confirmations` one branch under the same
  id-matching rule; `confirmation.expired` joined `CONFIRMATION_EVENT_TYPES`
  and a new `_CLEAR_REASON_OF_TYPE` table, so a folded row yields one
  `confirmation.cleared(reason="expired")` at its own cursor. The sequencer's
  acceptance pin
  (`test_inherent_action_view.py::test_the_row_query_selects_exactly_the_types_the_fold_reads`)
  caught a real gap: `_SELECT_RESPONSE_ROWS_SQL` is a static list and never
  selected the new type, so a v2 panel would have kept showing the expired
  ask. Fixed in `jarvis/runtime/inherent_view_sequencer.py`. 7/7 new checks.
- slice 3 the runtime — 25b56eb — `realtime.confirmation.durable_expiry.enabled`
  (default false, requires `realtime.enabled`, downgrades once with one
  warning) and `confirmation.expiry_sweep_interval_s: 30` under the existing
  top-level `confirmation:` block, clamped to a 5s floor with one warning;
  `_run_confirmation_expiry_sweep` as a plain function taking `now_ms`,
  `_reconcile_confirmation_expiry_in_thread` as the `asyncio.to_thread` +
  `open_event_log` offload both the periodic task and the boot reconciler go
  through, `_confirmation_expiry_sweep_task` joined to `watchers` so the
  existing teardown cancels it, and a third boot reconciler after the two
  existing ones. All four config paths verified live (off / on / parent-off
  downgrade / interval clamp). 10/10 new checks.
- slice 4 docs — 2b60522 — ADR-0014 D14's "Not built yet" sentence replaced
  by one naming where each piece landed and its flag; ADR-0012 §10.8 erratum
  recording that D14 supersedes D4's "no expiry event, no timer" clause for a
  live confirmation, with the D4 bullet unedited. `docs/spec.html` judged
  explicitly unchanged: D14 already owns this rule.
- flag-off byte-identity (R9) — the same seeded confirmation scenario driven
  against a `git archive 8b63a05` export (root-pre, port 8041) and against
  this worktree (root-post, port 8042), both with an overlay whose
  `realtime.confirmation` key is deleted outright — absent, not false — and a
  20s wait (4 sweep intervals). Both logs dumped as
  `SELECT id, type, payload_json, source_event_id, correlation_json ... ORDER BY id`;
  `diff` printed empty. The flag-off watcher list is user_intent_watcher,
  response_watcher, tts_watcher, system_trigger_watcher, supervisor_sweep —
  no `confirmation_expiry_sweep` — and the boot log has no third reconciler
  line (`grep -c` for the sweep = 0).
- live run — daemon from this worktree, runtime root
  `$CLAUDE_JOB_DIR/tmp/root-live`, port 8043, overlay `realtime.enabled: true`
  + `durable_expiry.enabled: true` + `confirmation.ttl_ms: 15000` +
  `expiry_sweep_interval_s: 5`; log line
  `confirmation_expiry_sweep started (interval=5.0s)`. Two earlier phrasings
  routed around the machine ask (ADR-0012 §10.6 N: the first took `open_path`,
  the second proposed `write_file` but the pre_action gate refused on
  `entity_trusted: entity_required` because the target did not resolve); the
  third, against an existing `~/Desktop/jarvis-lane-c.md` (created for the run
  and deleted after), reached `confirm_required`. Chain:
  `confirmation.requested` id 75, `event_uid` 4b567d4fb9164f06a02d313a8f8ec95f,
  `confirmation_id` C3928a77e, `expires_at_ms` 1788649480696 →
  `confirmation.expired` id 83 (strictly greater), payload exactly
  `{"confirmation_id":"C3928a77e","expired_at_ms":1788649483986}` (at/past the
  deadline), `source_event_id` column 4b567d4fb9164f06a02d313a8f8ec95f
  (byte-equal to the requested uid), `actor` `jarvis_runtime`; terminal-row
  count for C3928a77e = 1; sweep log line
  `confirmation expiry sweep expired C3928a77e (deadline 1788649480696, observed 1788649483986)`.
  No input of any kind was sent between rows 75 and 83 — rows 77-82 are the
  tail of the already-submitted turn. Restart against the SAME runtime root:
  count still 1, `MAX(events.id)` still 83, so the boot reconciler appended
  nothing. The run needed no audio: the SYSTEM DEFAULT OUTPUT DEVICE was never
  switched (`SwitchAudioSource -c -t output` read "MacBook Pro Speakers" before
  and after, read-only) and `JARVIS_VOICE_DISABLE_WAKE=1` kept the wake
  listener shut. The other worktree's daemon on 8006 was left running.
- final gates — lint-imports KEPT (1/1, 97 files / 303 dependencies) exit 0 ·
  ruff `All checks passed!` exit 0 · mypy strict 241 files exit 0 · full
  hermetic `1051 passed / 64 deselected` in 50.28s = the 1041 branch baseline
  plus this card's 10 new tests, zero failures · Swift
  `Executed 174 tests, with 0 failures` and `git diff --stat -- desktop/`
  empty (R10) · R6 pin held: `git diff 8b63a05..HEAD` touches neither
  `is_live`'s body nor any of its four readers.
- NOTE for the hub — `realtime-integration` advanced after this lane's merge
  (lanes A and B landed), so `realtime-integration..HEAD` no longer isolates
  lane C's diff; the range for this card's work is `8b63a05..HEAD`.
