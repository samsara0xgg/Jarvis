# Goal: action-and-confirmation-projections

## Goal
The v2 Inherent wire carries two more sections of durable truth — `actions` and
`pending_confirmation` — folded in L2 from the registered action lifecycle, the
A5 cancel trail and the ADR-0012 confirmation events, serialized by the L5
presenter into the `action.upsert` / `confirmation.upsert` /
`confirmation.cleared` mutations and snapshot pages the shipped Swift DTOs
already decode, with the response lifecycle finally reported from committed
events instead of the hardcoded `generating`.

## Why
The `sequencer-and-snapshot` card deliberately shipped one section and named
this one as the successor: `snapshot.begin.section_order` and `counts` are the
literal `[RESPONSE_GROUPS_SECTION]` / `{RESPONSE_GROUPS_SECTION: len(pages)}`
(jarvis/surface/inherent_presenter.py:246-247), and every response's lifecycle
is the constant `_LIFECYCLE_PENDING = "generating"` behind a comment that names
this card (:40-42, used at :56 and :116). The Swift side already shipped the
whole contract — `ActionUpsert`, `ConfirmationUpsert`, `ConfirmationCleared`,
the five-section `SnapshotSection` enum and the reducer cases that apply them —
so the panel decodes a work cockpit it is never sent.

## Current behavior
- **The fold models response groups only.** `RESPONSE_EVENT_TYPES` is the three
  `surface.response_{open,chunk,emitted}` types (jarvis/state/inherent_view.py:41-45),
  `ChangeKind` is `response.opened | response.segment | response.delivery`
  (:50), and `InherentViewCheckpoint` holds `through_cursor` plus
  `groups: tuple[ResponseGroupView, ...]` and nothing else (:118-123). The
  module is stdlib-only by its own docstring (:27-29).
- **The sequencer's SQL filters to those same three types.**
  `_SELECT_RESPONSE_ROWS_SQL` selects `id, event_uid, type, ts_epoch_ms,
  payload_json` `WHERE ... type IN ('surface.response_open',
  'surface.response_chunk', 'surface.response_emitted')`
  (jarvis/runtime/inherent_view_sequencer.py:64-71); `_rows` returns exactly
  those five columns and `fold()` is called with them at :176-182 (live) and
  :239-245 (post-ACK catch-up). No action, confirmation, `response.*`,
  `correlation_json` or `source_event_id` value ever reaches the fold.
- **The presenter hardcodes one section and one lifecycle.**
  `RESPONSE_GROUPS_SECTION` at :39 is the only section constant; `_page_frame`
  stamps it at :169-172; `build_snapshot_plan` writes `section_order` and
  `counts` from it at :246-247. `counts[section]` is the **page** count, not the
  item count. The module docstring states the presenter "never derives canonical
  action state, confirmation validity or cancellability" (:14-16).
- **`action:` provenance exists but carries no UI fields.** `_ActionFoldState.fold`
  registers an `action:<id>` `EntityRegistryEntry` at `action.dispatched` and
  pops it at any of `_STATUS_BOARD_TERMINAL_ACTION_TYPES` = `action.result_observed`,
  `action.failed`, `action.timeout_assumed`, `action.cancelled`
  (jarvis/state/projections.py:770-777, :1274-1296); the same route records
  `ActionAdmission(action_id, dispatched_event_uid, admission_gate_uid, lease_id,
  run_id)` (:1343-1365). Nothing there holds a label, target, state or cursor.
- **The confirmation slot is single-slot with lazy TTL.**
  `PendingConfirmationSlot` holds `confirmation_id, snapshot, template_line,
  expires_at_ms, state, accepted_event_uid` (jarvis/state/projections.py:1407-1445)
  and `is_live(now_ms)` returns `state == "pending" and now_ms < expires_at_ms`
  (:1450-1462). `_fold_pending_confirmations` (:1512-1573) replaces the slot
  unconditionally on `confirmation.requested`, moves it on an id-matching
  `confirmation.accepted` / `confirmation.rejected`, and moves
  `accepted_unconsumed -> consumed` on a passing `gate.evaluated` carrying a
  `lease_id`. No `confirmation.expired` type is registered or emitted anywhere
  in `jarvis/` or `tests/`.
- **Confirmation events.** `confirmation.requested` requires
  `confirmation_id, action_snapshot, template_line, expires_at_ms`
  (jarvis/state/event_log.py:1107-1118); the snapshot's six keys are
  `tool_name, caller, canonical_target, target_entity_ref, risk_level, args_meta`
  (jarvis/decision/__init__.py:3826-3844). It is emitted with
  `source_event_id` = the `gate.evaluated` verdict and
  `correlation=_action_correlation(action_request)` = `{action_id, run_id?,
  turn_id?}` (:3855-3867, correlation builder at :3565-3573).
  `confirmation.accepted` / `.rejected` require
  `confirmation_id, utterance_raw, grammar_rule_id` and point their
  `source_event_id` column at the request (event_log.py:1120-1142); they are
  emitted at jarvis/decision/__init__.py:3945.
- **The A5 cancel trail is four durable rows and one ack.** The cancel branch
  resolves its target from `packet.action_admissions` and writes nothing when it
  cannot (jarvis/decision/__init__.py:1802-1822), then emits
  `action.proposed` with `tool_name="cancel_action"`,
  `payload.action_id` = the *request* id, `payload.arguments.target_action_id` =
  the target and `target_entity_ref = "action:<target>"` (:1949-1990); one
  `gate.evaluated(gate="pre_action", action_id=<request id>)` (:2000-2015); on
  pass, `action.authorized` + dispatch (:2080-2100). The L4 handler "writes no
  event about the target" — it emits only its own
  `action.result_observed(semantics="ack")` whose `tool_output` is
  `json.dumps({"target_action_id": ..., "status": ...})` with status in
  `accepted | already_terminal | unconfirmed | unsupported`
  (jarvis/execution/tools.py:5438-5453, :5455-5510). `action.cancelled` on the
  target stays the runner's, written only after confirmed quiescence
  (jarvis/execution/action_runner.py:1090-1103).
- **Registered action lifecycle types** (jarvis/state/event_log.py):
  `action.proposed` :308-314 (required `action_id, tool_name, caller_principal,
  risk_level`; optional `target_entity_ref, run_id, turn_id, arguments`),
  `action.authorized` :355-361, `action.dispatched` :363-374,
  `action.running` :376-388, `action.result_observed` :390-400,
  `action.failed` :402-410 (optional `error, reason, stash_ref`),
  `action.timeout_assumed` :412-426 (optional adds `run_id, task_id`),
  `action.cancelled` :428-450, plus `worker.quiesced` :456-462,
  `action.cleanup_completed` :466-472, `action.cleanup_failed` :474-480.
- **Response lifecycle types exist and are emitted**: `response.started` :747-771
  (required `response_id, response_group_id, turn_id, phase, channel, ...`),
  `response.completed` :773-779, `response.cancelled` :781-793 (required
  `reason`), `response.failed` :796-808 (required `reason`); the terminals are
  CAS-owned by jarvis/state/lifecycle_terminal.py:34 and the opener by
  jarvis/state/response_runs.py:33-38. When L3 opened a run, `cli_render`
  binds the L5 `surface.response_*` rows to that same `response_id`
  (jarvis/surface/cli_render.py:456-470); with no run it falls back to the
  uuid5 `stable_legacy_presentation_binding`, and no `response.*` row exists.
- **Swift is the pinned contract, and it already has all of it**
  (desktop/inherent-swift/InherentRealtime/RealtimeViewDTOs.swift):
  - `ActionCanonicalState` :81-95 = `proposed | authorized | dispatched |
    running | result_observed | timeout_assumed | failed | cancelled`, terminal
    = the last four.
  - `ConfirmationClearReason` :100-105 = `accepted | rejected | expired |
    superseded`.
  - `ActionUpsert` :255-278 — wire keys `action_id`, `response_group_id?`,
    `task_id?`, `state`, `label`, `target?`, `revision`, `cancellable`,
    `freshness_ms?`. **There is no `cancel_request` key** and no
    `progress_label` / `user_action_required` key.
  - `ConfirmationUpsert` :280-303 — `confirmation_id`, `response_group_id?`,
    `action_id?`, `summary`, `target?`, `risk`, `options`, `expires_at_ms`,
    `revision`. **There is no `requested_at_ms` key.**
  - `ConfirmationCleared` :305-317 — `confirmation_id`, `reason`, `revision`.
  - `ViewMutation` :354-389 dispatches on `"kind"`: `action.upsert`,
    `confirmation.upsert`, `confirmation.cleared` (plus `response.lifecycle`
    :364-365 → `ResponseLifecycleChange` :221-234 with `response_id`,
    `lifecycle`, `terminal_reason?`, `revision`). An unknown durable kind
    throws — a durable mutation the client cannot apply is a protocol
    violation (:12-15).
  - `SnapshotSection` :545-551 = `response_groups | actions |
    pending_confirmation | capabilities | surface_notices`; `SnapshotPage`
    :647-681 decodes `items` as `[ActionUpsert]` / `[ConfirmationUpsert]` by
    section; `VerifiedSnapshot` :697-735 holds `actions: [ActionUpsert]` and
    `pendingConfirmation: ConfirmationUpsert?` (last page wins for the slot).
  - Reducer: the durable switch is RealtimeReducer.swift:298-309;
    `applyAction` :581-606 inserts on first sight, **ignores** a lower
    `revision` (rule 5), preserves the local `commandOverlay` / `progressHint`
    across an upsert, ignores an equal-revision identical upsert, and resyncs
    with `.terminalConflict` on an equal-revision differing upsert touching a
    terminal or on any state change away from an existing terminal.
    `applyConfirmation` :608-622 requires **strictly greater** `revision` to
    replace an occupied slot; `applyConfirmationCleared` :624-633 clears only
    when the id matches and `revision >= pending.revision`.
  - `ActionViewState` (RealtimeState.swift:324-362) carries `commandOverlay` /
    `progressHint` as local-only truth; `ConfirmationViewState` :364-397
    mirrors the upsert exactly.
- **The Swift decoder tolerates an omitted empty section.**
  `SnapshotStagingState.verify` iterates `begin.sectionOrder` and reads
  `begin.counts[section.rawValue] ?? 0` (RealtimeTransport.swift:119-128), so a
  section absent from `section_order` is simply never looked for; a section
  listed with no `counts` entry expects zero pages. A page whose section is not
  in `sectionOrder` throws `.unexpectedSection` (:97-99).
- **Test doubles**: `_Socket` records every frame and the close
  (tests/integration/test_inherent_sequencer.py:381-405); `_Rig` runs a real
  Event Log with one sequencer and one hub (:407-497) and its `emit()` (:448-456)
  writes any registered type through `emit_event`, so action and confirmation
  rows need no new fixture; `_settle` :499 and `_ack` :503.
  `scripts/smoke_inherent_sequencer.py` seeds rows directly with `_emit_turn`
  (:85-115) and prints the snapshot trail at :299.
- Baselines at 16ef152: hermetic 922 passed / 64 deselected; Swift 165.

## Target behavior

### The L2 fold gains two projections
`jarvis/state/inherent_view.py` folds three more families beside the response
rows, in `events.id` order, still one `ViewTransition` per relevant row:
the eight `action.*` lifecycle types plus `worker.quiesced` and the two
`action.cleanup_*`; `confirmation.requested` / `.accepted` / `.rejected`; and
`response.started` / `.completed` / `.cancelled` / `.failed`.
`InherentViewCheckpoint` gains `actions: tuple[ActionView, ...]` and
`pending_confirmation: ConfirmationView | None`. `ChangeKind` (:50) gains
`response.lifecycle`, `action.upsert`, `confirmation.upsert` and
`confirmation.cleared` — spelled exactly as the Swift `ViewMutation`
discriminators (RealtimeViewDTOs.swift:369-379), because Swift shipped first.

### Every D13 ActionView field, and where its value comes from
| D13 field | Source |
| --- | --- |
| `action_id` | `payload.action_id` on every `action.*` row |
| `task_id?` | `action.timeout_assumed.payload.task_id` (event_log.py:412-426) — the only registered action payload key of that name; otherwise `None` (D13 marks it optional) |
| `response_group_id?` | `stable_response_group_id(turn_id)` (jarvis/shared/realtime.py:220-229) over `action.proposed.payload.turn_id`; `None` when no proposed row carried one |
| `action_type` | `action.proposed.payload.tool_name`; `"unknown"` when an action was dispatched with no proposed row (the fixture path `ActionAdmission` already documents, projections.py:1352-1356) |
| `canonical_state` | the row's own type, one-to-one with `ActionCanonicalState` (RealtimeViewDTOs.swift:81-92) |
| `state_revision_cursor` | `events.id` of the row that set the state; this is the wire `revision` |
| `started_at_ms?` | `ts_epoch_ms` of the `action.running` row; `None` before it |
| `updated_at_ms` | `ts_epoch_ms` of the latest row for the action |
| `safe_target_ref?` | `action.proposed.payload.target_entity_ref`; `None` when absent |
| `result_available` | true once an `action.result_observed` row landed |
| `failure_code?` | `payload.reason` of `action.failed` / `action.timeout_assumed` / `action.cancelled` only — never `payload.error` (free text; D13:938 bars unbounded stdout and unverified self-report) |
| `cleanup_state` | `quiesced` from `worker.quiesced`, `completed` from `action.cleanup_completed`, `quarantined` from `action.cleanup_failed`; **default `none`**. `pending` has no committed source and is never produced — no event marks cleanup as started |
| `cancel_request` | folded from the A5 trail below; **not serialized** (see the wire gap) |
| `freshness` | **D13's safe default `fresh`**: no clock reaches a pure fold, so the wire's `freshness_ms?` is omitted, which the decoder reads as fresh. `updated_at_ms` is what a later staleness rule would use |
| `display_label` → wire `label` | `action_type` verbatim |
| `safe_target_label` → wire `target?` | `safe_target_ref` verbatim (already a safe ref: `file:<abs>`, `action:<id>`, or a task id) |
| `cancellable_hint` → wire `cancellable` | computed **in the L2 fold**: an `action.dispatched` row seen and no row in `_STATUS_BOARD_TERMINAL_ACTION_TYPES` — the same open-set rule `_ActionFoldState.fold` applies (projections.py:1274-1296), which is exactly the set `resolve_cancellable_action` treats as cancellable |
| `progress_label` | no wire slot on `ActionUpsert`; the ephemeral `action.progress_hint` owns it and this card produces no ephemeral frame |
| `user_action_required` | no wire slot on `ActionUpsert`; `surface_notices` would carry it and stays deferred |

### CancelRequestView
Folded from the A5 trail, keyed by the cancel request's own `action_id`
(= `CancelActionRequest.request_id`, jarvis/decision/__init__.py:1953-1959):

- `received` — `action.proposed` with `tool_name == "cancel_action"`; the target
  is `payload.arguments.target_action_id`.
- `authorized` — `gate.evaluated(gate="pre_action", action_id=<request id>,
  outcome="pass")`.
- `rejected` — that verdict with any other outcome (it never reaches L4), or an
  ack whose `status` is `unsupported`; `reason_code` = the gate reason or
  `"unsupported"`.
- `quiescing` — `action.dispatched` for the request with no ack row yet; the
  handler blocks up to `_CANCEL_ACTION_QUIESCENCE_BUDGET_S` (tools.py:5428)
  between those two rows, so this is a real observable interval.
- `failed` — `action.failed` / `action.timeout_assumed` on the request itself,
  or an ack whose `status` is `unconfirmed` (the target did not quiesce in
  budget and keeps running, ADR-0008 F9).
- `resolved` — the **target** reached any of the four canonical terminals;
  `reason_code` = `cancelled` | `completed_before_cancel` | `failed` |
  `already_terminal`, the last from the ack's `status`.
- `revision_cursor` = `events.id` of the row that set the state.
- Only `action.cancelled` moves the target's own `canonical_state` to
  `cancelled`; "cancel requested" never enters `ActionLifecycle` (D13:955-957).
- The fold reads **only** the `status` key out of the ack's `tool_output` JSON,
  never its other fields and never any free text.

**Wire gap, stated deliberately**: `ActionUpsert` has no `cancel_request` key
(RealtimeViewDTOs.swift:255-278) and ruling 1 forbids touching Swift sources, so
`CancelRequestView` is folded, held in the checkpoint and pinned hermetically,
but does not cross the wire in this card. The cancel action itself is an action
and does travel, as its own `action.upsert` row with `label = "cancel_action"`
and `target = "action:<target id>"`.

### Confirmation section
One globally unique slot mirroring `PendingConfirmations`' single-slot rule:

- `confirmation.requested` → `confirmation.upsert` with
  `confirmation_id` = `payload.confirmation_id`;
  `summary` = `payload.template_line` (runtime-rendered fixed vocabulary,
  ADR-0012 D5 — never LLM text); `target` = `action_snapshot.canonical_target`;
  `risk` = `action_snapshot.risk_level`; `options` = `["accept", "reject"]`
  (D14:990); `expires_at_ms` = `payload.expires_at_ms`; `action_id` = the
  event's `correlation.action_id`; `response_group_id` =
  `stable_response_group_id(correlation.turn_id)`; `revision` = `events.id`.
  Nothing else from `action_snapshot` — no `args_meta`, no `content_artifact`,
  no lease (D14:994-995).
- A `confirmation.requested` arriving while a slot is live produces **one**
  `view.delta` whose `changes` are ordered `confirmation.cleared(A,
  reason="superseded")` then `confirmation.upsert(B)` (D14:1021-1027). No
  `confirmation.superseded` event exists and none is invented.
- `confirmation.accepted` / `.rejected` whose `payload.confirmation_id` matches
  the live slot → `confirmation.cleared` with reason `accepted` / `rejected`. A
  non-matching id changes nothing, exactly as `_fold_pending_confirmations`
  ignores it (projections.py:1549-1562).
- **Lazy expiry, no timer**: the fold has no clock, so the deadline is judged
  against the `ts_epoch_ms` of the rows it folds — the first row folded whose
  timestamp is at or past the slot's `expires_at_ms` emits
  `confirmation.cleared(reason="expired")` at that row's cursor. This is the
  existing `PendingConfirmationSlot.is_live` rule (:1450-1462) evaluated at fold
  time. Consequence, accepted here and fixed by the follow-up card: with no
  further row committed, an idle panel keeps showing an expired ask.
- The clear's `revision` is that row's cursor, which satisfies the reducer's
  `revision >= pending.revision` guard (RealtimeReducer.swift:624-633); a
  supersede's upsert revision is strictly greater than the cleared slot's,
  satisfying :608-622.

### Response lifecycle
`_LIFECYCLE_PENDING` (inherent_presenter.py:40-42) and its deferring comment are
deleted. The fold joins `response.*` rows to the response by `response_id`:
`response.started` → `generating`, `response.completed` → `completed`,
`response.cancelled` → `cancelled` (`terminal_reason` = `payload.reason`),
`response.failed` → `failed` (same). A lifecycle-only row emits a
`response.lifecycle` change (`ResponseLifecycleChange`, RealtimeViewDTOs.swift:221-234).
`waiting_action` and `finalizing` are **not reachable**: no registered event type
carries either, and inferring them from an in-flight action would invent state.
A response opened through the legacy uuid5 binding
(cli_render.py:465-470) has no `response.*` row at all and stays `generating`;
`surface.response_emitted` never stands in for a terminal (ADR-0014:1047, :2049).

### Snapshot
`section_order` and `counts` become data driven over the sections the plan
actually produced, in the fixed order `response_groups`, `actions`,
`pending_confirmation`. `counts[section]` keeps its shipped meaning — the number
of **pages** in that section, which is what
`SnapshotStagingState.verify` compares against (RealtimeTransport.swift:119-128).
Each section pages independently under the existing 64 KiB
`SNAPSHOT_PAGE_MAX_BYTES` rule, `page_index` restarts at 0 per section, and
`content_hash` stays SHA-256 over the exact sent page bytes in section-then-page
order. **An empty section is omitted from both `section_order` and `counts`** —
verified safe: the Swift verifier only iterates `begin.sectionOrder`, and a page
for an unlisted section would throw `.unexpectedSection` (:97-99).
`capabilities` and `surface_notices` stay absent.

### Delivery and flag
The new deltas travel on the durable lane exactly like response deltas — D11
rule 1 forbids dropping or reordering action and confirmation states
(docs/goals/per-client-flow-control.md:72). No new flag: everything stays under
`realtime.inherent.v2_sequencer.enabled`, and flag-off is byte-identical on the
v1 wire.

## Affected contracts and files
- L2 jarvis/state/inherent_view.py — the action and confirmation folds, the
  CancelRequestView state machine, the response-lifecycle join, the widened
  `ChangeKind`, and `InherentViewCheckpoint` gaining `actions` and
  `pending_confirmation`. Gains one `jarvis.shared` import for
  `stable_response_group_id`; `state -> shared` is permitted (.importlinter:20-30)
  and the module's stdlib-only docstring sentence (:27-29) is updated.
- L5 jarvis/surface/inherent_presenter.py — `action.upsert` /
  `confirmation.upsert` / `confirmation.cleared` / `response.lifecycle` change
  payloads, the two new snapshot item shapes, per-section paging, data-driven
  `section_order` / `counts`, and the deletion of `_LIFECYCLE_PENDING` (:40-42,
  :56, :116). Still no canonical derivation here (:14-16).
- runtime jarvis/runtime/inherent_view_sequencer.py — `_SELECT_RESPONSE_ROWS_SQL`
  (:64-71) must widen its type list and add the `source_event_id` and
  `correlation_json` columns the confirmation and cancel joins need; `_rows`
  (:296-304) and the two `fold(...)` call sites (:176-182, :239-245) carry them
  through. This is the one place ruling 8's "prefer none" cannot hold: the fold
  is fed by that query and sees nothing else.
- jarvis/state/projections.py — expected untouched; the `action:` registry fold
  and `ActionAdmissions` keep their exact shape and stay the provenance source.
- tests/integration/test_inherent_sequencer.py (or a new
  tests/integration/test_inherent_action_view.py) — `_Rig.emit` (:448-456)
  already writes any registered type.
- scripts/smoke_inherent_sequencer.py — one action driven dispatch-to-terminal
  and one confirmation request-to-accept, printing both sections' deltas with
  their cursors.
- docs/adr/0014-inherent-realtime-ux.md D13/D14 — the built sentence and the
  terminalizer note.

## Boundaries and non-goals
- Layers that may change: L2 (`inherent_view.py` only), L5
  (`inherent_presenter.py` only), runtime (`inherent_view_sequencer.py`'s row
  query only), tests, scripts, one ADR sentence.
- Must not change: `jarvis/surface/inherent_output.py` (v1), the v2 envelope
  shapes, the flow-control lanes and budgets, `jarvis/decision/action_cancel.py`,
  the confirmation event schema, the existing folds in
  `jarvis/state/projections.py`, and every file under `desktop/inherent-swift/`.
  No new feature flag.
- **Durable confirmation expiry is not in this card.** The
  `confirmation.expired` event, `ConfirmationTerminalizer`, the runtime expiry
  timer and boot reconciliation of ADR-0014 D14 (:998-1019) belong to
  `confirmation-expiry-terminalizer`. This card reports `expired` only through
  the existing lazy rule at fold time.
- Non-goals: the `capabilities` and `surface_notices` sections; any ephemeral
  frame, including `action.progress_hint`; the `cancel_request` wire field and
  any Swift source change; L3 computing `cancellable_hint` /
  `user_action_required` (no L3 producer exists, and the fold's open-set rule is
  the same one L3's cancel resolver reads); `waiting_action` and `finalizing`;
  the artifact fetch path behind a document reference.

## Rejected approaches
- Adding the action/confirmation fold to `jarvis/state/projections.py` — that
  module rebuilds over the whole log per `ProjectionSet` (:1579-1607); the
  Inherent view needs an incremental cursor fold, and the
  `sequencer-and-snapshot` card already rejected mixing them.
- Reusing `PendingConfirmations` directly as the wire section — it is rebuilt
  from the whole log and carries `args_meta`, `content_artifact` and
  `accepted_event_uid`, none of which may reach Swift (D14:994-995); the
  Inherent fold mirrors its rules instead of exporting its dataclass.
- Computing `cancellable` in the presenter — inherent_presenter.py:14-16 and
  D13:940-942 both forbid it.
- Reading completion off `surface.response_emitted` — ADR-0014:1047 and :2049
  say it is delivery production truth, not ResponseRun completion.
- Inventing a `confirmation.superseded` event — D14:1024-1027 says the supersede
  is two ordered mutations inside the request's own delta, with no new event.
- Emitting an empty `actions` or `pending_confirmation` section with zero pages
  — omitting it is what ruling 7 prefers and the Swift verifier tolerates it
  (RealtimeTransport.swift:119-128), so an idle daemon's snapshot stays exactly
  what it is today.
- Deriving `cleanup_state = pending` from a terminal with no cleanup row — no
  event marks cleanup as started; D13's `none` is the honest default.
- Giving each section its own `page_index` namespace shared with another —
  `SnapshotStagingState.PageKey` is `(section, pageIndex)`
  (RealtimeTransport.swift:68-76), so per-section indices starting at 0 are
  required, not optional.

## Acceptance evidence
- Positive: `PYTHONPATH=. .venv/bin/python -m pytest -q
  tests/integration/test_inherent_sequencer.py tests/integration/test_inherent_action_view.py`
  raw output ends in a pass line and covers:
  - one `action.upsert` per reachable `ActionCanonicalState` transition —
    `proposed -> authorized -> dispatched -> running -> result_observed`, and
    each of `failed`, `timeout_assumed`, `cancelled` as a terminal — each with
    `revision` equal to its row's `events.id`, `label` = the proposed
    `tool_name`, `target` = the proposed `target_entity_ref`;
  - `cancellable` true exactly between `action.dispatched` and the first
    terminal, and false before and after;
  - `cleanup_state` reaching `quiesced`, `completed` and `quarantined` from
    `worker.quiesced` / `action.cleanup_completed` / `action.cleanup_failed`,
    and defaulting to `none`;
  - the CancelRequestView walking `received -> authorized -> quiescing ->
    resolved(cancelled)` over a real A5 trail, plus `rejected` from a refusing
    `gate.evaluated`, `rejected(unsupported)` and `failed(unconfirmed)` from the
    two ack statuses, and `resolved(already_terminal)`;
  - a confirmation appearing on `confirmation.requested` with the exact nine
    wire keys and no `args_meta`, then cleared by `accepted`, by `rejected`, by
    supersede (one delta, `confirmation.cleared(superseded)` strictly before
    `confirmation.upsert`, the new revision strictly greater), and by lazy
    expiry on the first row timestamped past `expires_at_ms`;
  - the response lifecycle reported as `generating` / `completed` / `cancelled`
    / `failed` from the `response.*` rows, with `terminal_reason` carried, and a
    legacy-bound response with no `response.*` row staying `generating`;
  - a three-section snapshot whose `section_order` is `["response_groups",
    "actions", "pending_confirmation"]`, whose `counts` are per-section page
    counts, whose `page_index` restarts at 0 per section, and whose
    `content_hash` is SHA-256 over the sent page bytes in section-then-page
    order; and a snapshot with no action and no confirmation omitting both
    sections from `section_order` and `counts`;
  - a post-ACK catch-up replaying action and confirmation deltas in ascending
    cursor order alongside response deltas.
- Layer proof: the transcript shows
  `grep -nE "^from jarvis\.|^import jarvis\." jarvis/state/inherent_view.py jarvis/surface/inherent_presenter.py`
  with `inherent_view.py` naming only `jarvis.constitution` / `jarvis.state` /
  `jarvis.shared` and `inherent_presenter.py` only those.
- Swift: `desktop/inherent-swift` test output shows **165 unchanged**, or, if a
  reducer case turns out to be genuinely uncovered, 165 plus the added cases
  with the card's Progress naming which case and why.
- Regression: `PYTHONPATH=. .venv/bin/python -m pytest -q -m "not live_llm and
  not live_codex"` shows at least the 922 passed / 64 deselected baseline at
  16ef152 plus this card's new cases; `tests/integration/test_inherent_flow_control.py`
  and `tests/integration/test_gated_action_cancel.py` pass unchanged;
  `lint-imports`, `ruff check .` and `mypy --strict jarvis tests scripts tools`
  each exit 0 with printed counts.
- Flag-off proof: with the flag absent from config, the v1 smoke frames are
  byte-identical to a pre-change capture and no sequencer line appears.
- Live run: **not required** — no LLM and no external runtime; every event is
  written directly. One daemon-level smoke is required instead: start the daemon
  from this worktree on a port other than 8006 with `realtime.enabled` and the
  flag on, and run the extended `scripts/smoke_inherent_sequencer.py`, which
  drives one action from `action.proposed` through `action.dispatched` /
  `action.running` to `action.result_observed`, and one confirmation from
  `confirmation.requested` to `confirmation.accepted`. **Canary: the printed
  `snapshot.begin.section_order` naming all three sections, the final
  `action.upsert` carrying `state="result_observed"` with its `revision`, and
  the `confirmation.upsert` followed by `confirmation.cleared` with
  `reason="accepted"` at a strictly greater `event_cursor`.**

## Docs to sync
- docs/adr/0014-inherent-realtime-ux.md D13 (:909-975) — one built sentence
  naming `jarvis/state/inherent_view.py` as the `ActionViewProjection`'s home,
  plus the pinned facts this card settles: `cleanup_state` has no committed
  `pending` source, `freshness` is reported by omission of the wire's
  `freshness_ms`, and `cancel_request` / `progress_label` /
  `user_action_required` have no slot in the shipped `ActionUpsert` and are
  folded-but-unsent (or unmodelled) until a Swift card adds one.
- docs/adr/0014-inherent-realtime-ux.md D14 (:998-1019) — one sentence recording
  that the `confirmation.expired` event, `ConfirmationTerminalizer`, runtime
  timer and boot reconciliation are **not yet built** and belong to
  `confirmation-expiry-terminalizer`; the rule text is not edited and ADR-0012
  D4 is not touched.
- docs/adr/0014-inherent-realtime-ux.md :226-231 — record which ResponseRun
  lifecycle states have a committed source (`generating`, `completed`,
  `cancelled`, `failed`) and that `waiting_action` / `finalizing` have none, or
  judge unchanged.
- docs/spec.html §3.6.7 — judged unchanged unless the implementation finds an
  ownership boundary this card moves; ADR-0014 owns D13/D14 and the sentence
  §3.6.7 already gained from the `sequencer-and-snapshot` card covers durable
  projection transport.

## Open questions
(none)

## /goal condition
Implement docs/goals/action-and-confirmation-projections.md on the current branch. Read it fully before touching code. The goal is met when the transcript shows: (1) the diff adds an action projection and a single-slot confirmation projection to jarvis/state/inherent_view.py (ChangeKind widened with response.lifecycle, action.upsert, confirmation.upsert, confirmation.cleared; the checkpoint with actions and pending_confirmation), serializes them in jarvis/surface/inherent_presenter.py with data-driven section_order and counts over response_groups, actions, pending_confirmation, deletes _LIFECYCLE_PENDING and its deferring comment, and widens the sequencer row query in jarvis/runtime/inherent_view_sequencer.py to carry the new types plus source_event_id and correlation_json — no new flag, and no change to jarvis/surface/inherent_output.py, jarvis/decision/action_cancel.py, the existing folds in jarvis/state/projections.py, the confirmation event schema, the v2 envelope shapes, the flow-control lanes, or any file under desktop/inherent-swift; (2) raw pytest output ending in a pass line covering: one action.upsert per reachable state with revision = its events.id and label/target from action.proposed; cancellable true only between action.dispatched and the first terminal; cleanup_state reaching quiesced, completed and quarantined, else none; the CancelRequestView walking received, authorized, quiescing, resolved over a real cancel_action trail plus rejected, failed and already_terminal; a confirmation upserted with only its nine wire keys and cleared by accepted, by rejected, by supersede as one delta ordering confirmation.cleared(superseded) before confirmation.upsert at a strictly greater revision, and by lazy expiry judged against a folded row's ts_epoch_ms; response lifecycle generating/completed/cancelled/failed from response.* rows with terminal_reason, and a legacy-bound response staying generating; a three-section snapshot with per-section page counts, page_index restarting at 0 per section, and content_hash over the sent page bytes; an empty section omitted from section_order and counts; a post-ACK catch-up carrying action and confirmation deltas in ascending cursor order; (3) the layer grep showing inherent_view.py importing only jarvis.constitution, jarvis.state and jarvis.shared, and inherent_presenter.py only those; (4) Swift test output showing 165 unchanged, or 165 plus added cases with a stated uncovered reducer case; (5) the full hermetic suite run with -m "not live_llm and not live_codex" showing at least 922 passed / 64 deselected, and test_inherent_flow_control.py and test_gated_action_cancel.py passing unchanged; (6) lint-imports, ruff check . and mypy --strict jarvis tests scripts tools each exiting 0 with printed counts; (7) a flag-off check showing v1 frames byte-identical and no sequencer line; (8) a statement that no live LLM run is required, plus raw output of the extended scripts/smoke_inherent_sequencer.py against a daemon from this worktree on a port other than 8006 with the flag on, showing snapshot.begin.section_order naming all three sections, a final action.upsert with state result_observed and its revision, and a confirmation.upsert then confirmation.cleared with reason accepted at a strictly greater event_cursor; (9) every Docs to sync entry updated or explicitly judged unchanged, following the rule: when the change alters a documented contract, invariant, ownership boundary, or externally relevant behavior, update the canonical document that owns that fact, do not document what the code already makes clear, do not duplicate a fact across documents; (10) each slice committed with the project commit skill, git status clean, one Progress line per slice. Durable confirmation expiry (confirmation.expired, ConfirmationTerminalizer, runtime timer, boot reconciliation) is out of scope. If the card contradicts the repository, stop and report instead of redesigning. Or stop after 70 turns.
## Progress
- (none yet)
