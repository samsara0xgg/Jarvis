# Goal: lifecycle-commentary

## Goal
A real action lifecycle event on a user-originated turn produces one short spoken
`commentary` ResponseRun — acknowledge, progress, or error, mapped deterministically
from the durable event and never from a timer or an LLM — in the same
`response_group_id` as that turn's `final` answer, behind a default-off flag.

## Why
ADR-0008 §8 step 5 (docs/adr/0008-real-time-response-streaming.md:1030) and D6
(:344-369) are unbuilt: `PresentationIntent` "does not exist in `jarvis/` yet" (:367),
which §10 rule 2 (:1171) repeats as an open spec deviation. Today Jarvis is silent
between "Allen asked" and "the answer is verified", which for a Codex-shaped action is
minutes of nothing.

## Current behavior
- Base: `realtime-integration` at `88011b0`; A5 `docs/goals/gated-action-cancel.md`
  merged (that card's Progress section is empty as of `88011b0` — this card launches
  after it lands). Hermetic baseline observed at `88011b0`: 852 passed / 63 deselected.
- **No `PresentationIntent` or `AttentionRouting` type exists** anywhere under
  `jarvis/` (grep, zero hits). The L3→L5 contract objects that do exist live in
  `jarvis/shared/realtime.py`: `ResponseInterruptPolicy` (:189) and
  `LegacyPresentationBinding` (:208); `jarvis/decision/response_run.py:34-41` imports
  from there, and `.importlinter` puts `constitution | shared` at the bottom of the
  layer DAG so both L3 and L5 may import it. `jarvis/shared/stream_emission.py` holds
  `EmissionPermit`, split out because L2 also consumes it.
- `ResponseRun` already owns a `phase` of `Literal["commentary", "final"]`
  (jarvis/decision/response_run.py:54, ADR-0008 :120-130) and
  `start_response_run(conn, *, turn_id, trigger_event_uid, request_client, policy,
  response_id, phase="final", channel="both", committed_event_bus=None,
  corrects_response_id=None, route=None)` (jarvis/decision/response_run.py:622-635)
  already takes `phase`; it stamps it on `response.started` (:649) and on the run
  (:680). `response_group_id` is **derived, not passed**:
  `stable_response_group_id(turn_id)` (:643; jarvis/shared/realtime.py:220-229), so two
  runs of one turn share a group by construction.
- `request_client` is used only for `preset_snapshot` (provider/model on the payload,
  :644, :659-660). `_start_drive_turn_response` builds one in two lines from the
  session factory (jarvis/runtime/__init__.py:1574-1576).
- The only policy constructors are `legacy_full_text_policy`
  (jarvis/decision/response_run.py:126-140) and `routine_stream_policy`
  (jarvis/decision/stream_gate.py:31-51); **both pin `allowed_phases=("final",)`**
  (:140, :44). `"deterministic"` exists only as a member of the `ResponseEmissionMode`
  union (jarvis/decision/response_run.py:57) with no constructor and no caller.
- **A2's streaming emit path cannot carry a deterministic run.** L2's `_validate_permit`
  requires a committed `gate.evaluated(gate="stream_emit", outcome="permit",
  candidate_risk="routine")` row and matches the run's `emission_mode` against the
  literal `"routine_stream"` and `required_gate_mode` against `"sentence"`
  (jarvis/state/stream_emission.py:245-270); `append_permitted_segment` then refuses any
  run whose `response.started.source_event_id` is not `utterance.received` /
  `surface.user_intent` — "ordinary stream requires a direct user input source"
  (:296-298). The A2 seam wires exactly that (`emit_segment=lambda ... :
  emit_permitted_segment(...)`, jarvis/runtime/__init__.py:1629-1637).
- The path that opens, chunks, emits and terminalizes from a complete plan is
  `render_response` (jarvis/surface/cli_render.py:240-254): with
  `streaming_enabled=True` it writes one `surface.response_open` + N
  `surface.response_chunk` + one `surface.response_emitted` (:465-495), binding the
  run's ids when `response_id`/`response_group_id` are supplied (:459-470).
  `drive_turn` calls `terminalizer.complete(...)` and then `render_response(...)`
  (jarvis/runtime/__init__.py:2404-2460).
- **`_emit_response_open` hardcodes `"phase": "final"`** (jarvis/surface/cli_render.py:186).
  The Event Type Registry already allows an optional `phase` on
  `surface.response_open` / `_chunk` / `_emitted` (jarvis/state/event_log.py:929, :956,
  :969).
- The Pre-emit token is satisfiable locally: `pre_emit_gate(text, claim_evidence,
  active_subject_ref=None)` short-circuits to a routine pass-through `ResponsePlan`
  (jarvis/decision/gates.py:697-745), and `record_pre_emit_token` / `SurfaceState`
  (jarvis/surface/cli.py:122-136, :211-214) is the token carrier `render_response`
  checks.
- **`CommittedEventBus` exists but cannot see the D6 acknowledge row.** The class is at
  jarvis/state/committed_event_bus.py:16 and the runtime constructs one when
  `realtime.enabled` (jarvis/runtime/__init__.py:1507-1513), but **no production code
  calls `.subscribe()`** (grep across `jarvis/`, zero hits) and
  **`jarvis/execution/tools.py` never receives a bus** (grep for `committed_event_bus`
  in that file: zero hits). `action.dispatched` is emitted there
  (jarvis/execution/tools.py:4886-4892) and so is every inline sync-tool
  `action.result_observed`; only `action.running` and the runner-owned terminals publish
  (jarvis/execution/action_runner.py:866, :1008, :1038, :1063, :1198).
- The working committed-event mechanism is the durable-cursor watcher:
  `_tts_watcher` (jarvis/runtime/inherent_loop.py:1065) polls
  `_fetch_events_after(conn, after_id=..., event_types=(...))` (:337-361) from a boot
  high-water anchor (`_latest_id(conn)`, :1119-1123) and is registered as one
  `watchers.append(asyncio.create_task(...))` entry in `serve_inherent` (:3061-3078).
  `_response_watcher` (:994) is the same shape on one cursor.
- **Turn origin is already durable and unambiguous.** `turn.started` carries
  `{"turn_id", "trigger": <trigger event_uid>}` with `source_event_id` = that same uid,
  and is written in exactly two places, both on the user-intent branch:
  `claim_input_once` (jarvis/state/input_claim.py:188-194) and `_handle_utterance` when
  the pump is off (jarvis/decision/__init__.py:1148-1158). No reconciliation trigger
  (`_RUNTIME_TRIGGER_TYPES`, jarvis/runtime/__init__.py:185-196;
  `_RECONCILIATION_TRIGGER_TYPES`, jarvis/decision/gates.py:843-845) and no
  supervisor-sweep system turn writes one.
- **The pending-confirmation slot carries no `action_id`.** `PendingConfirmations` is a
  single global slot (jarvis/state/projections.py:1327-1349) whose `snapshot` is the
  six-key `action_snapshot` — `tool_name`, `caller`, `canonical_target`,
  `target_entity_ref`, `risk_level`, `args_meta` (jarvis/decision/__init__.py:3674-3679).
  Liveness is read at `PendingConfirmationSlot.is_live(now_ms)` (projections.py:1315-1325).
- `attention_policy` (jarvis/decision/gates.py:848-945) reads a `SituationPacket` and
  defaults to `voice_notify` for anything that is not `worker.reported` or a
  reconciliation terminal (:944-945). `ROUTINE_ATTENTION_CHANNEL = "voice_notify"`
  (jarvis/decision/pre_route.py:36).
- voice_media reads `phase` off the open header, defaulting to `"final"`
  (jarvis/surface/voice_media.py:1856), keys buffers by `response_id` (:1852) and stamps
  `phase` on `surface.playback_started` (:2029). Its lane handoff is keyed by
  `response_group_id`: a same-group successor is appended to `_after_drain`, never
  superseded (:1930-1945) — ADR-0006 :261's `enqueue_after_drain`.
- Flag precedent: `Wave4ResponseFlags.from_mapping` reads the nested
  `routine_streaming.enabled` mapping with a fail-closed `is True`
  (jarvis/shared/realtime.py:99-112); `_wave4_response_activation` resolves the whole
  graph once with one downgrade per boot (jarvis/runtime/__init__.py:585-638); the yaml
  block is config/jarvis.yaml:158-168.

## Target behavior
- **Contract.** `PresentationIntent` is added to **jarvis/shared/realtime.py**, beside
  `ResponseInterruptPolicy` (:189) and `LegacyPresentationBinding` (:208) — the module
  where the existing L3→L5 contract objects live and the only one both L3 and L5 may
  import under `.importlinter`. Frozen dataclass with exactly spec §3.6.3's five fields
  (docs/spec.html:1041-1047): `intent_type`, `surface_hint`, `subject_ref`,
  `content_hint`, `freshness_required`. `intent_type` carries all seven spec §3.6.3 values
  (`emphasize | acknowledge | progress | stale_warn | confirm_request | review_needed | error`);
  this card produces only `acknowledge | progress | error`. It is never written to the Event
  Log (spec §3.6.3's Contract-vs-Event note, docs/spec.html:1050).
- **Mapping (pure L3).** New `jarvis/decision/commentary.py` with one deterministic
  function from a single committed action `Event` to `PresentationIntent | None`,
  implementing exactly ADR-0008 D6's four action rows (:352-357):
  `action.dispatched` → acknowledge "我开始处理了。"; `action.running` → progress
  "任务已经在运行。"; `action.result_observed` → progress "结果回来了，我整理一下。";
  `action.failed` → error "这一步失败了，我告诉你具体原因。" `subject_ref` is the
  `action_id`; `content_hint` is the phrase; every other event type returns `None`. No
  LLM, no timer, no clock, no DB read.
- **Delivery.** Each intent becomes its own ResponseRun opened with
  `start_response_run(..., phase="commentary", channel="speech",
  trigger_event_uid=<the action event's own event_uid>, turn_id=<the action's
  correlation turn_id>)`, so `response_group_id` derives to the turn's group
  (jarvis/decision/response_run.py:643) and D6's "truth derives from the durable action
  event" (:367) is the run's `source_event_id`. `linked_action_ids=[action_id]`.
  `request_client` comes from `runtime.llm_session_factory.snapshot(None)` /
  `.create(...)` (the two lines at jarvis/runtime/__init__.py:1574-1576); **no request is
  ever issued, so no cost disposition is recorded**.
- A new `deterministic_commentary_policy(...)` in jarvis/decision/response_run.py:
  `emission_mode="deterministic"`, `output_risk_class="routine"`,
  `required_gate_mode="sentence"`, `allowed_phases=("commentary",)`,
  `allowed_channels=("speech",)`. It is the first constructor for the `"deterministic"`
  member that has sat unused at :57.
- The phrase reaches the surface through `render_response` with `streaming_enabled=True`
  and the run's `response_id`/`response_group_id`, on the channel constant exported by `jarvis/decision/commentary.py`
  (`COMMENTARY_ATTENTION_CHANNEL = "voice_notify"`; L3 owns the channel decision, runtime passes it through)
  — one `surface.response_open` + one `surface.response_chunk` + one
  `surface.response_emitted` (jarvis/surface/cli_render.py:465-495). The plan is
  `pre_emit_gate(phrase, claim_evidence, active_subject_ref=None)`'s routine
  pass-through (jarvis/decision/gates.py:738-745) and its `response_hash` is recorded on
  a locally built `SurfaceState` (jarvis/surface/cli.py:211-214). The run is then closed
  through the same `ResponseTerminalizer.complete(...)` `drive_turn` uses
  (jarvis/runtime/__init__.py:1612-1616, :2404-2410).
- `_emit_response_open` takes the run's phase instead of the hardcoded `"phase": "final"`
  (jarvis/surface/cli_render.py:186), defaulting to `"final"` so every existing caller is
  byte-identical. This is what makes voice_media buffer the run as `phase="commentary"`
  (jarvis/surface/voice_media.py:1856) and stamp it on `surface.playback_started` (:2029).
- **Wiring is runtime-only.** A new durable-cursor observer in `jarvis/runtime/`, built on
  the `_tts_watcher` pattern: `_fetch_events_after(conn, after_id=...,
  event_types=("action.dispatched", "action.running", "action.result_observed",
  "action.failed"))` (jarvis/runtime/inherent_loop.py:337-361) anchored at boot
  high-water (`_latest_id`, :1119-1123) so a historical action never speaks fake
  progress, registered as one more `watchers.append(asyncio.create_task(...))` in
  `serve_inherent` (:3061-3078) only when the flag is on. It calls the L3 mapping and
  opens the run; `_RUNTIME_TRIGGER_TYPES` (jarvis/runtime/__init__.py:185-196) is
  untouched and commentary never re-enters `decide()`.
- **Origin filter.** Commentary only for an action whose `correlation["turn_id"]` has a
  `turn.started` row whose `source_event_id` names an event of type
  `surface.user_intent` or `utterance.received`. This is exactly the predicate L2 already
  enforces for ordinary streaming (jarvis/state/stream_emission.py:296-298), and
  "no `turn.started` for this turn" is precisely "not user-originated" because the row is
  written only on the user-intent branch (jarvis/state/input_claim.py:188-194;
  jarvis/decision/__init__.py:1148-1158).
- **Confirmation guard.** While `PendingConfirmations.slot.is_live(now_ms)` is true
  (jarvis/state/projections.py:1315-1325) the observer produces no commentary at all.
  The slot is globally unique and its `action_snapshot` carries no `action_id`
  (jarvis/decision/__init__.py:3674-3679), so ADR-0014 :1132's "new commentary does not
  overwrite an unresolved confirmation" is enforced at the granularity the fold supports.
  `confirm_request` intents stay with the confirmation flow.
- **Coalescing.** At most one commentary per `(action_id, D6 row)`, tracked in the
  observer's own memory. If a newer intent for the same action arrives while an earlier
  commentary run for that action has not reached `surface.playback_started`, the earlier
  run is cancelled through `request_response_cancel` (jarvis/decision/response_run.py:693)
  with `reason="superseded"` (in the closed vocabulary at jarvis/shared/realtime.py:23-25);
  a commentary already playing finishes.
- **The final is never delayed, dropped, or superseded.** Both runs share
  `stable_response_group_id(turn_id)`, and voice_media's same-group branch appends the
  successor to `_after_drain` rather than interrupting (jarvis/surface/voice_media.py:1930-1945)
  — ADR-0006 :261's `enqueue_after_drain`, idempotent by `(response_id, phase)`.
- **Flag** `realtime.commentary.enabled`, default false, parsed with the fail-closed
  `is True` rule of `Wave4ResponseFlags.from_mapping` (jarvis/shared/realtime.py:99-112)
  and resolved once in the activation graph (jarvis/runtime/__init__.py:585-638); it
  additionally requires `realtime.enabled` and `response_run_lifecycle`, since without the
  latter no ResponseRun, terminalizer or registry is constructed at all
  (jarvis/runtime/__init__.py:1516-1523, :1571-1573). With the flag off no observer task
  is created and the event log is byte-identical to today.

### Per-turn assumptions and their disposition
| Consumer | Assumption | Disposition |
|---|---|---|
| jarvis/state/conversation.py:21, :223-232 | none — `MAX_RESPONSES_PER_TURN = 8`, keyed by `response_id` | no change; `phase` is already folded against `{"commentary","final"}` (:120-131). Prove two `PresentationRecord`s under one `ConversationTurn`. |
| jarvis/surface/voice_media.py:1852, :1930-1945 | none — buffers keyed by `response_id`, lane by `response_group_id` | no change; same-group successor enqueues after drain |
| jarvis/surface/voice_ledger.py:26-35 | none — `GenerationLease` carries the three ids, no uniqueness | no change |
| jarvis/surface/cli_render.py:35-36 | docstring: "For each turn the function emits exactly one `surface.response_emitted`" | **must be reworded** to per call / per response; the code at :459-470 already binds ids per run |
| jarvis/runtime/inherent_loop.py:1090-1095, `_tts_watcher` | keys `silent_turns` and `pipeline.begin_turn/handle_emitted` by `turn_id` | **lane must verify**: two open→emitted sequences under one `turn_id` is new for the legacy (non-voice_media) pipeline. State the finding either way. |
| jarvis/surface/inherent_output.py:149-205 | v1 card protocol keys `open`/`append`/`done` by `turn_id` | no change; with the flag on the v1 card shows a commentary sequence then the final's. Swift-side commentary presentation is lane C (ADR-0014 D26). |
| jarvis/state/inherent_view.py, jarvis/surface/inherent_presenter.py | — | **these files do not exist** at `88011b0` (`ls jarvis/state`, `ls jarvis/surface`); nothing to dispose of |

### Reused from A5 (docs/goals/gated-action-cancel.md)
- The `action:` EntityRegistry kind, registered at `action.dispatched` and evicted by
  `_STATUS_BOARD_TERMINAL_ACTION_TYPES` (A5 Target behavior, L2 route) — the observer
  reads it as the non-terminal action set instead of folding its own, so it never speaks
  progress for an action whose terminal already committed.
- A5's admission lookup on `ProjectionSet` — `dispatched_event_uid` for the action's own
  causal chain and `run_id` joined only from `run.started`. This card adds no fold.
- Not available from A5 and therefore derived here, named so the lane does not go
  looking: first-observed-`running` detection (the observer's in-memory
  `(action_id, row)` coalescing key plus the boot high-water anchor) and turn origin
  (the `turn.started` predicate above).

## Affected contracts and files
- shared jarvis/shared/realtime.py — `PresentationIntent`.
- L3 new jarvis/decision/commentary.py — the pure four-row mapping.
- L3 jarvis/decision/response_run.py — `deterministic_commentary_policy`.
- L5 jarvis/surface/cli_render.py — `_emit_response_open` phase argument (:186);
  docstring correction (:35-36).
- runtime jarvis/runtime/inherent_loop.py — the observer + its `watchers.append` entry
  (:3061-3078); jarvis/runtime/__init__.py — flag resolution (:585-638) and whatever the
  observer needs from `JarvisRuntime`.
- config/jarvis.yaml — the `realtime.commentary` block next to `realtime.response` (:158-168).
- tests/integration/test_lifecycle_commentary.py — new, beside
  tests/integration/test_wire_routine_streaming.py and test_wave4a_response_run.py.

## Boundaries and non-goals
- Layers that may change: shared, L3 (`commentary.py`, `response_run.py`), L5
  (`cli_render.py` only), runtime, config, tests, scripts.
- Must not change: `attention_policy()` and any other function in
  jarvis/decision/gates.py; `_RUNTIME_TRIGGER_TYPES` (pinned by
  tests/canary/test_canary_runtime_trigger_types.py); L4 (`jarvis/execution/`);
  `desktop/`; the `ResponsePlan` schema (jarvis/decision/gates.py:104-147); the
  routine_stream / streaming permits from A2/A3 and their L2 validators
  (jarvis/state/stream_emission.py:245-298).
- Non-goals: the "听到了" utterance-accepted row; commentary for `run.started`,
  `task.verified`, `gate.evaluated`, `response.cancelled`; model-generated commentary or
  preamble permits (ADR-0008 D2 policy rule 3, :182); timer-paced progress;
  `progress_only` enforcement on the final; Inherent/Swift commentary display (ADR-0014
  D26, lane C); ducking; RPi.

## Rejected approaches
- Delivering the phrase through `emit_permitted_segment` — L2 pins the run's
  `emission_mode` to `"routine_stream"`, demands a committed
  `gate.evaluated(stream_emit, outcome="permit")` row, and refuses any run whose source
  is not a user-input event (jarvis/state/stream_emission.py:245-270, :296-298). A
  deterministic run sourced from an action event fails both; taking this path means
  weakening two L2 invariants for a fixed string.
- Subscribing the observer to `CommittedEventBus` — `jarvis/execution/tools.py` is never
  handed a bus (grep: zero hits), so `action.dispatched` (:4886-4892) and every inline
  sync `action.result_observed` are committed unpublished. A subscriber would silently
  lose the D6 acknowledge row unless L4 changed, which this card forbids.
- Matching the pending confirmation to a specific `action_id` — the `action_snapshot`
  has six keys and none is `action_id` (jarvis/decision/__init__.py:3674-3679), and the
  slot is globally unique anyway (jarvis/state/projections.py:1327-1349).
- Adding the four action types to `_RUNTIME_TRIGGER_TYPES` so `decide()` produces the
  commentary — it is pinned by tests/canary/test_canary_runtime_trigger_types.py, and an
  LLM turn per lifecycle row is exactly what ADR-0008 D6 :369 forbids ("A deep model is
  never called only to generate 我在查").
- Superseding the final with the commentary, or cancelling the commentary when the final
  is ready — ADR-0006 :261 says the commentary-to-final handoff is `enqueue_after_drain`
  in one group, not supersession.

## Acceptance evidence
- Positive (hermetic, tests/integration/test_lifecycle_commentary.py, no tests/unit):
  raw pytest output covering — (1) the mapping's four rows produce the exact D6
  `intent_type`/phrase/`subject_ref`, and a non-mapped event (`run.started`,
  `gate.evaluated`, `action.cancelled`) returns `None`; (2) origin filter — an
  `action.dispatched` on a turn with no `turn.started`, and one whose `turn.started`
  trigger is a reconciliation event, both produce no commentary run; (3) confirmation
  guard — with a live `PendingConfirmationSlot`, no commentary run is opened; after the
  slot resolves, the next row does open one; (4) coalescing — a second
  `action.running` for the same `action_id` opens nothing, and a newer row arriving
  before `surface.playback_started` cancels the earlier run with
  `reason="superseded"`; (5) final-after-commentary — a commentary run and the turn's
  `final` run share one `response_group_id`, the final's `surface.response_emitted` is
  written, and voice_media enqueues it after drain rather than interrupting; (6) the
  commentary run's `surface.response_open`, `_chunk` and `_emitted` all carry
  `phase="commentary"` while an unchanged `render_response` caller still writes
  `"final"`; (7) no `cost.recorded` row is written for a commentary run.
- Positive (flag-off, hermetic): the same driven turn with `realtime.commentary.enabled`
  false yields an event log byte-identical to today — event types, payloads and order
  compared and shown equal, zero `phase="commentary"` rows, and no observer task in
  `serve_inherent`'s watcher list.
- Live run (required — playback ordering depends on the real TTS lane): daemon from this
  worktree on its own runtime root and port with `realtime.commentary.enabled`,
  `response_run_lifecycle` and `realtime.streaming_output` on, using the existing
  `~/.jarvis-realtime-test/inherent.sh` rig. Ask one routine tool-route question
  (`get_current_time` or `list_tasks`). Canary: quote the commentary `response_id`, its
  `surface.playback_started` row with `phase="commentary"`, then the final's
  `response_id` and its later `surface.playback_started`, with both timestamps showing
  the final played after; and confirm the final was spoken. Then flag off, ask the same
  question, and show zero `phase="commentary"` rows for that turn.
- Audio guard: `SwitchAudioSource -c -t output` shown before and after with the same
  device restored; output routed to `BlackHole 16ch` for the run; the microphone is never
  opened (text submit only).
- Regression: `PYTHONPATH=. .venv/bin/python -m pytest -q
  tests/integration/test_wave4a_response_run.py tests/integration/test_wire_routine_streaming.py
  tests/integration/test_wave2_streaming_media.py tests/integration/test_conversation_history.py
  tests/integration/test_inherent_loop_tts_watcher.py
  tests/canary/test_canary_runtime_trigger_types.py
  tests/canary/test_canary_response_terminal_only_through_cas.py
  tests/canary/test_canary_system_turns_never_tts.py` passes; the full hermetic suite
  `PYTHONPATH=. .venv/bin/python -m pytest -q -m "not live_llm and not live_codex"`
  matches the baseline observed at `88011b0` (852 passed / 63 deselected) plus the new
  tests, with any delta explained; `lint-imports`, `ruff check .`, `mypy --strict jarvis
  tests scripts tools` exit 0. Raw output shown.

## Docs to sync
- docs/adr/0008-real-time-response-streaming.md:367 — replace "it does not exist in
  `jarvis/` yet" with the built slice: `PresentationIntent` in
  `jarvis/shared/realtime.py`, the mapping in `jarvis/decision/commentary.py`, delivery
  as a `phase="commentary"` ResponseRun.
- docs/adr/0008-real-time-response-streaming.md:1171 — §10 rule 2 says the contract "is
  still to be introduced in code"; amend it the same way.
- docs/spec.html §3.6.3 (:1029-1050) — expected **unchanged**: the field set and the
  `intent_type` vocabulary are used exactly as written and `PresentationIntent` stays a
  contract, never an event. State that explicitly.
- docs/adr/0014-inherent-realtime-ux.md:226-231 — expected **unchanged**: the phase
  vocabulary already reads `commentary | final`. State that explicitly.
- docs/adr/0006-full-duplex-voice-session.md:251-262 — judged unchanged if the handoff
  shipped as `enqueue_after_drain` in one `response_group_id`; if the lane deviates,
  amend in place and say how.

## Open questions
(none)

## /goal condition
Implement docs/goals/lifecycle-commentary.md on the current branch. The goal is met when all of the following appear in the transcript: (1) the diff adds an ephemeral `PresentationIntent` in jarvis/shared/realtime.py with exactly spec §3.6.3's five fields and no Event Log write, a pure jarvis/decision/commentary.py mapping the four ADR-0008 D6 action rows to acknowledge/progress/error intents with the action_id as subject_ref and every other event type to None, a `deterministic` commentary ResponseRun policy with `allowed_phases=("commentary",)`, a runtime durable-cursor observer over `action.dispatched|running|result_observed|failed` anchored at boot high-water that opens one `phase="commentary"` run per intent in the turn's own `response_group_id` and delivers its single segment through `render_response`, the phase argument replacing cli_render's hardcoded `"phase": "final"`, and the default-false `realtime.commentary.enabled` flag — while leaving `_RUNTIME_TRIGGER_TYPES`, `attention_policy()`, every other function in jarvis/decision/gates.py, jarvis/execution/, desktop/, the ResponsePlan schema, and the routine_stream permits and their L2 validators unchanged; (2) raw pytest output of the new hermetic test file covering the four-row mapping plus non-mapped events returning None, the origin filter (an action whose turn has no user-input `turn.started` yields no commentary), the live-pending-confirmation guard, coalescing per (action_id, row) with an unstarted earlier run cancelled `superseded`, a final run in the same response_group_id still emitted and enqueued after drain, `phase="commentary"` on the run's open/chunk/emitted while an unchanged caller still writes "final", and no cost disposition for a commentary run — ending in a pass line; (3) raw output showing that with the flag off the same driven turn produces an event log byte-identical to today: event types, payloads and order compared and shown equal, zero `phase="commentary"` rows, and no observer task registered; (4) raw live-run output from a daemon with the flag on and a routine tool route, quoting the commentary `response_id` and its `surface.playback_started` row carrying `phase="commentary"`, the final's `response_id` and its later `surface.playback_started`, both timestamps proving the final played after, and the same question with the flag off showing zero commentary rows — plus `SwitchAudioSource -c -t output` before and after with the same device restored and a statement that the microphone was never opened; (5) raw output of the named regression files and of the full hermetic suite excluding live tests, with the observed passed/deselected counts compared against the 852 passed / 63 deselected baseline recorded at 88011b0 and any delta explained, plus `lint-imports`, `ruff check .`, and `mypy --strict jarvis tests scripts tools`, each ending with a pass line or exit 0; (6) each entry under Docs to sync updated or explicitly judged unchanged, following the rule: update the canonical document that owns a changed contract, do not document what the code makes clear, do not duplicate a fact across documents; (7) each slice committed with the project commit skill and `git status` clean; (8) a Progress line per slice in the card. Or stop after 70 turns.

## Progress
- Contract + mapping + policy — 90db339 — `pytest -q tests/integration/test_lifecycle_commentary.py` 4 passed; lint-imports KEPT 1/1, ruff clean, mypy strict clean 229 files.
- Phase argument through render_response — a3a1d1f — 81 passed (streaming media, routine-streaming wire, tts watcher, silent-turn canary).
- realtime.commentary flag in the activation graph — dad0bd7 — 43 passed (new file + Wave-4A response-run suite); shipped config asserted off.
- Runtime observer + hermetic acceptance — b7b5aad — 946 passed / 64 deselected
  (baseline 927/64 at `5094bb0` plus this file's 19 checks); flag-off log shown
  equal to the flag-on log minus its commentary rows (8 rows, sequences equal).
- ADR-0008 D6 / §10 rule 2 updated — 56d129c — spec §3.6.3, ADR-0014:226-231 and
  ADR-0006:251-262 judged **unchanged** with evidence: the five fields and the
  seven-value `intent_type` vocabulary are used verbatim, `PresentationIntent`
  reaches no `emit_event` call site (grep: 0), the phase vocabulary already reads
  `commentary | final`, and the handoff shipped as `enqueue_after_drain` in one
  `response_group_id`.
- Live run (daemon from this worktree, own runtime root
  `~/.claude/jobs/f3902c8d/tmp/lane-a-rt`, port 8016, DeepSeek presets untouched,
  `JARVIS_VOICE_DISABLE_WAKE=1` so the microphone is never opened, text submit
  only). Flag on, turn `T97212ea1`: commentary `RESPb91d93c4f168459da7809665d038b22e`
  `surface.playback_started phase=commentary` at 21:40:41.014Z, final
  `RESPf6e6c04e679f470f870ee9acfd35d12a` `surface.playback_started phase=final` at
  21:40:43.952Z — 2.938 s later and 2 ms after the commentary's
  `playback_completed`, i.e. enqueued after drain in one group
  `RGRPce5e913d78ea5095b9362b3bb51c06ef`; the final was spoken through to
  `playback_completed` at 21:40:51.228Z. Zero `cost.recorded` names the commentary
  run. Flag off, turn `T043ad1e5`: zero `phase="commentary"` rows and no
  `commentary_watcher` line in the daemon log. Audio guard:
  `SwitchAudioSource -c -t output` = `MacBook Pro Speakers` before and after,
  routed to `BlackHole 16ch` for the run and restored from a trap (hub's
  2026-09-05 rule followed: a captured `BlackHole 16ch` would have restored to
  `MacBook Pro Speakers` instead).
- Live-run finding, fixed — e08aa39 — the ActionRunner's inline synchronous path
  commits `action.result_observed` with an empty correlation, so the origin
  filter could not name its turn. The observer now joins through the action's own
  `action.dispatched` row.
- Live-run finding, no change needed: on a sub-millisecond synchronous tool the
  action's terminal commits ~1 ms after `action.dispatched`, before the observer's
  10 ms poll, so the acknowledge and progress rows are correctly suppressed by the
  non-terminal guard and only the `action.result_observed` phrase speaks. That is
  the card's "never speaks progress for an action whose terminal already
  committed" working as specified.
- `_tts_watcher` per-turn keying, verified as the card asks: the legacy
  (non-`voice_media`) `TTSPipeline` holds a single `self._turn_id`
  (jarvis/surface/voice_tts.py:2645-2698). Two open→emitted sequences under one
  `turn_id` are handled sequentially with no loss — the second `begin_turn` resets
  the buffer and, the turn id being identical, no chunk hits the
  `turn_id != self._turn_id` drop. The degraded case is interleaving: if the
  final's `surface.response_open` commits between the commentary's open and its
  chunk, the two texts flush as one utterance. Nothing crashes or is dropped. It
  cannot arise on the `voice_media.StreamingTTSPipeline` path (buffers keyed by
  `response_id`, lane by `response_group_id`), which is what
  `realtime.streaming_output` selects (jarvis/runtime/inherent_loop.py:1683-1700)
  and what the live run exercised. No code change: `jarvis/surface/voice_tts.py`
  is outside this card's may-change list.
- Card tension resolved, recorded in ADR-0008 D6: "the run is then closed through
  `complete(...)`" and "a newer intent cancels an earlier unheard run with
  `reason="superseded"`" cannot both hold if the run is terminalized right after
  render — a completed run's cancel returns `AlreadyTerminal` and writes nothing.
  The acceptance evidence requires an observable `superseded` cancel, so the run
  is closed through `ResponseTerminalizer.complete(...)` at
  `surface.playback_started` instead of immediately after render.
