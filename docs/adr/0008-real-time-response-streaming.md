# ADR-0008 — Real-time Response Streaming

**Status:** Approved (2026-08-31, Allen)
Approved means the design is approved for implementation; implementation completeness is tracked only by §13 Definition of done.
**Date:** 2026-08-31
**Supersedes:** ADR-0003 Step 2's post-hoc replay as the default realtime path. The old full-response `drive_turn → ResponsePlan → render_response` path remains the compatibility and high-risk fallback.
**Depends on:** ADR-0001/0002 decision, evidence, and action foundations; ADR-0003 Inherent surface; ADR-0005 Voice foundation; ADR-0009 resident runtime.
**Paired with:** ADR-0006 (full-duplex media, streaming TTS, barge-in, playback ledger). ADR-0008 Step 1 reuses the L2 terminal primitive created by ADR-0006 Step 2; Steps 1–7 can otherwise use text/fake surfaces. Step 8 requires ADR-0006's accepted streaming-output capability, while ADR-0006 Step 6 requires this ADR's interrupt/cancel/terminal contracts.
**Does not use:** OpenAI Realtime API. Jarvis owns the stream protocol, response lifecycle, safety gates, tool orchestration, and cancellation. Existing cloud text LLM providers are accessed through a provider-neutral streaming adapter.

**Identity amendment (ADR-0014):** a ResponseRun is keyed only by `response_id` and has exactly one L3 terminal. The original generic `generation_id` field is removed from the v2 response/panel contract. Speech playback has a distinct, optional `playback_generation_id` minted only by L5; document-only responses do not have one. Retry or correction creates a new `response_id` and links it causally rather than reusing an ID with another generation.

---

## 1. Context

Current Jarvis has two features named “streaming,” but neither lowers model-to-user latency:

1. `LLMClient.chat_stream()` exists but has no production caller. Its current contract represents text only and does not safely carry streamed tool calls, usage, provider response IDs, or cancellation.
2. `render_response(streaming_enabled=True)` receives an already-complete, gated `ResponsePlan`, splits it into sentences, and writes all `surface.response_*` events within a few milliseconds. ADR-0003 correctly calls this “post-hoc replay” and “No latency win.”

The full path is serial:

```text
utterance.received
→ SQLite watcher
→ await one complete drive_turn
→ complete LLM chat
→ optional tool dispatch
→ complete tool result
→ another complete LLM chat
→ optional Pre-emit Gate retry with another complete LLM chat
→ complete ResponsePlan
→ parse complete <voice>/<document>
→ post-hoc sentence events
→ complete TTS audio
→ playback
```

Production history confirms the effect:

- recent ordinary no-tool turns took roughly 7.3–7.8 seconds from intent to response open;
- historical median was about 4.7 seconds, P90 about 42.8 seconds, P95 about 56 seconds;
- several 42–48 second tool turns used 4–5 serial deep-model calls while individual web actions were often only 0.2–2.2 seconds.

There is also a deeper concurrency problem. `spawn_worker` is marked `is_async`, but `ToolRegistry.dispatch()` directly invokes its handler, and that handler synchronously waits for `run_codex_action()` for up to 600 seconds. `_user_intent_watcher` awaits that whole turn before it consumes the next utterance. A microphone capable of barge-in would therefore produce “fake full duplex”: the machine hears Allen, but the decision runtime does not process him.

This ADR turns answer generation, action execution, input acceptance, and physical playback into separately cancellable lifecycles. It keeps Jarvis's quality and evidence model: streaming is allowed only when L3 has explicitly authorized the segment.

## 2. Goals and non-goals

### Goals

1. Deliver the first safe, meaningful sentence before the complete answer for routine turns.
2. Give complex tasks an immediate, truthful acknowledgement and lifecycle-based progress while the deep model/action continues.
3. Keep consequential and high-risk claims behind full-text or structured gates.
4. Make response cancellation independent from action cancellation.
5. Let new utterances be processed while a long action is running.
6. Make LLM text, tool calls, usage, completion, failure, and cancellation one provider-neutral typed stream.
7. Separate short speech from long document output so a screen answer need not block first audio.
8. Reduce unnecessary serial LLM rounds and parallelize independent actions when policy permits.
9. Preserve complete audit and restart behavior without putting token deltas on the durable Event Log.
10. Improve latency without lowering useful-answer quality; fast routing is enabled only after task evals pass.

### Non-goals

- Sending unsafe token deltas directly to TTS.
- Streaming high-risk output and later “resetting” what Allen already heard.
- Letting L5 infer output risk or tool permission.
- Letting the LLM invent progress not backed by an action lifecycle event.
- Cancelling a real action when Allen only says “别说了.”
- Replacing every old `drive_turn` caller in one migration.
- Guaranteeing cloud-model network latency; Jarvis owns and budgets its local overhead and route selection.

## 3. Architectural decision

### D1. ResponseRun becomes an explicit L3 lifecycle

Every user-visible output has a `response_id`. Different outputs belonging to one user turn may share a `response_group_id`:

- a short speech response;
- a longer document response;
- a later action-result response.

This allows the speech response to be cancelled without destroying a separately running document or ActionRun.

The L3 state machine is:

```text
idle → generating → finalizing → completed
             ↕
        waiting_action

generating/waiting_action/finalizing → cancelled | failed
```

Rules:

- exactly one terminal state: `completed`, `cancelled`, or `failed`;
- cancellation is idempotent;
- `waiting_action` means the response is quiet while linked actions continue; it does not occupy the input watcher;
- an action result may resume the same logical group with a new response or continue the existing run according to the route policy;
- physical playback completion is not a ResponseRun terminal condition. L3 generation and L5 delivery are separate truths. The one exception is a D6 lifecycle-commentary run, whose text is a fixed phrase with no generation to complete; see D6.

Exactly-one is a mechanism, not an assertion. L3 owns a single `ResponseTerminalizer`; completion, cancel, provider failure, shutdown, and recovery all call it. The terminalizer invokes one L2 atomic append operation: inside the same `BEGIN IMMEDIATE` transaction it verifies that `response_id` has no terminal, appends exactly one canonical terminal event, and commits, returning `Event | AlreadyTerminal`. There is no separate claim marker followed by a later emit. Losing callers cannot append another terminal.

The L2 operation cannot call today's auto-committing `emit_event()` from inside that transaction. `jarvis.state.event_log` adds `append_event_in_transaction(conn, ...)`, which performs the same registry/source/schema validation and INSERT but requires `conn.in_transaction` and never commits or publishes. Public `emit_event()` remains the compatibility wrapper for one-event transactions and rejects use inside an existing caller-owned transaction. Terminal, confirmation-consumption, inbox/outbox, and receipt CAS primitives exclusively own `BEGIN IMMEDIATE/COMMIT/ROLLBACK`, call the no-commit append, and publish to `CommittedEventBus` only after the outer commit succeeds.

Runtime never writes a L3 terminal directly. On restart it calls the L3 recovery port, which folds open ResponseRuns and asks `ResponseTerminalizer` to close them.

Request admission and response cancellation share an ordering fence. A token
checkpoint before prompt/pricing/client setup is insufficient: cancellation can
win during that setup. L3 therefore commits a `response.request_admitted` fact
under the response fence after setup, then releases the fence before provider
I/O. A request admitted first may finish after cancellation; cancellation first
prevents admission. Admission is neither a provider-success nor an output fact.
Decision and fresh-context reviewer requests use this boundary. The cancellation
deadline includes waiting for this fence and for SQLite, so network lifetime
must never become lock lifetime. The event contract is in spec §5.4.4.

`ResponseRun` owns:

```text
response_id
response_group_id
turn_id
phase                  # commentary | final
channel                # speech | document | both
emission_policy
linked_action_ids
cancellation_token
committed_text_prefix
next_segment_sequence
provider_response_id?
```

It does not own microphone frames, PCM buffers, or ActionHandle internals.

### D2. Use pre-route policy plus a permit for every emitted segment

True voice streaming cannot rely on the current `pre_emit_gate(active_subject_ref=None)` default because that path can classify output as `routine/sentence` without enough context. It also cannot wait for the old full-text gate and still be realtime.

Two explicit contracts are added without weakening the existing frozen `ResponsePlan`:

```text
ResponseEmissionPolicy
  response_id
  emission_mode          # deterministic | routine_stream | progress_only | full_text | structured
  output_risk_ceiling
  gate_mode
  allowed_phases
  allowed_channels
  active_subject_ref
  evidence_snapshot_hash
  policy_hash

EmissionPermit
  response_id
  segment_sequence
  phase
  channel
  segment_hash
  policy_hash
  gate_mode
  source_gate_event_uid
  issued_at_ms
```

The initial policy is determined before LLM generation from the complete turn context, not from a convenience default:

| Emission mode | When | Streaming rule |
|---|---|---|
| `deterministic` | fixed response, refusal, known local answer | no LLM; emit approved fixed segment |
| `routine_stream` | low-risk, no tools, no consequential active subject | complete stable sentences may receive permits |
| `progress_only` | tool/deep task whose final answer is not yet verified | only lifecycle-derived commentary may stream; final buffers |
| `full_text` | consequential claim, ambiguous risk, evidence-sensitive answer | no speech until complete ResponsePlan passes gate |
| `structured` | high-risk or structured confirmation | only approved structured block emits |

Policy rules:

1. Ambiguity defaults to `full_text`.
2. Policy may become stricter during a run, never looser for already generated content.
3. The first observed tool-call delta freezes ordinary model text; no generated “preamble” is spoken unless it was separately permitted as commentary.
4. Consequential/high-risk routes have zero TTS before final approval.
5. L5 verifies the response/sequence/hash/gate-event/permit binding and does not re-derive risk.
6. A safe stable sentence/subclause is the smallest v1 permit unit. Tokens are never presentation units.

A new `stream_emission_gate(policy, segment, risk_context)` is introduced. It requires an explicit `ResponseRiskContext`:

```text
ResponseRiskContext
  active_subject_ref          # explicit none/unknown value, never omitted
  linked_action_ids
  pending_action_risk
  confirmation_state
  evidence_snapshot_hash
  attention_channel

StreamGateOutcome
  permit | buffer_full_text | refuse
```

For every candidate, the gate derives `candidate_risk = routine | consequential_claim | high_risk_claim | unknown` from the segment text plus the complete context. Completion/send/delete/change claims, pending action results, unsupported evidence references, or unknown classification cannot pass a routine ceiling. The gate permits only when `candidate_risk <= policy.output_risk_ceiling`; `unknown` becomes `buffer_full_text`. A high-risk structured block must be fully materialized and approved before one atomic emission—there is no incremental structured streaming.

V1 makes that derivation executable with one versioned, local, deterministic `SegmentRiskClassifier`; it does not reuse `_derive_output_risk()` and does not call another LLM per segment:

1. compute a route/context floor from `ResponseRiskContext`; pending actions, confirmations, consequential subjects, incomplete evidence context, or any missing required field raise the floor to `consequential_claim|unknown`;
2. scan the complete stable candidate with Chinese/English data-driven rules for completion, mutation, send/delete/change, authorization, credential/security, financial/medical/legal, evidence/verification, structured block, and tool/progress claims;
3. return the monotonic maximum of context floor and text risk. Negation never lowers risk: “没有发送” and “I did not delete it” still buffer unless they come from an approved deterministic template;
4. classifier error, unknown rule version, or hard timeout returns `unknown` and therefore `buffer_full_text`.

Model-authored `routine_stream` is initially limited to a pre-routed `casual_or_explanatory` no-tool turn with no active real-world action/confirmation/consequential subject and a complete risk context. Deterministic lifecycle templates are classified by registered template ID/hash. The classifier target is p95 ≤5 ms with a 20 ms hard cap; the existing 500 ms segment budget includes assembly, classification, durable gate append, and permit creation rather than hiding a second model call. Broader semantic streaming requires a separate eval-backed rule-version decision.

Every outcome is durable before any permit is usable:

```text
gate.evaluated
  gate=stream_emit
  response_id, sequence, segment_hash
  candidate_risk, policy_hash, evidence_snapshot_hash
  outcome, reasons
```

`EmissionPermit.source_gate_event_uid` points to that committed gate event. L5 writes `surface.response_chunk` with the gate event in the Event's top-level `source_event_id` column. If the gate event or surface append fails, nothing is published or spoken. The existing `active_subject_ref=None → routine` shortcut is forbidden on this path.

### D3. Keep ResponsePlan as final authority and make spoken prefixes immutable

The current frozen `ResponsePlan(text, response_hash, output_risk_class, required_gate_mode, ...)` remains the final answer contract.

For `routine_stream`, L3 uses a dedicated finalizer:

```text
finalize_stream(committed_prefix, uncommitted_suffix, policy)
  -> ResponsePlan | StreamFinalizationFailure
```

- every permitted segment whose `surface.response_chunk` committed is appended to `committed_text_prefix`; a permit without its chunk was never exposed and is not prefix;
- the `committed_text_prefix` a ResponseRun carries (D1) is a running copy, not the authority: at finalization it is validated against the prefix reconstructed from the Event Log under the policy the permits were issued with, and a mismatch on either is a typed failure;
- the final gate may inspect the accumulated full answer, but a retry may regenerate only the uncommitted suffix;
- the final `ResponsePlan.text` must be byte-for-byte `committed_prefix + approved_suffix`;
- a retry or rewrite may not alter text already spoken;
- the finalizer writes no terminal. If it finds the committed prefix itself invalid, the caller terminalizes the current response as failed/limited with `committed_prefix_hash` populated and creates a separate correction ResponseRun linked by `corrects_response_id`. It never performs an old whole-answer retry or silently resets voice history.
- a stream sealed with zero permits never reaches that machinery: nothing was exposed, so there is no prefix to protect. It degrades to full-text delivery in place — the text already generated is judged by the ordinary Pre-emit Gate on the same ResponseRun, with no regeneration and no correction run (D2 rules 1 and 4). The run keeps the `routine_stream` policy it opened under; only the delivery shape changes.

For `full_text/structured`, the existing gate/retry path remains unchanged and no prefix is committed early.

ADR-0003 contemplated a `surface.response_reset` for speculative realtime text. This ADR does not use reset as a voice safety mechanism: audible words cannot be recalled. Text-only UI may add a reset protocol later, but speech safety is established before emission.

### D4. Replace `ChatStreamChunk` with a provider-neutral typed stream

The LLM adapter emits a discriminated union:

```text
LLMTextDelta
  llm_request_id
  provider_response_id?
  text
  content_block_index

LLMToolCallStarted
  llm_request_id
  call_index
  call_id?
  name?

LLMToolArgumentsDelta
  llm_request_id
  call_index
  call_id?
  arguments_delta

LLMToolCallCompleted
  llm_request_id
  call_index
  call_id
  name
  arguments_json

LLMUsageCompleted
  llm_request_id
  input_tokens?
  output_tokens?
  cache_read_tokens?
  cache_write_tokens?
  usage_status           # provider_final | partial | unavailable

LLMResponseCompleted
  llm_request_id
  provider_response_id?
  finish_reason

LLMResponseFailed
  llm_request_id
  provider_response_id?
  error_code
  retryable
```

The handle is an asynchronous iterator with explicit lifecycle:

```text
LLMStreamHandle
  events() -> AsyncIterator[LLMStreamEvent]
  cancel(reason) -> awaitable
  aclose() -> awaitable
```

Provider adapters are responsible only for protocol normalization:

- OpenAI-compatible adapters assemble `tool_calls[index].id/name/arguments` deltas;
- Anthropic adapters assemble `content_block_start/delta/stop` tool-use blocks;
- incomplete JSON at stream completion is a failed tool proposal, never an action;
- the LLM adapter never dispatches a tool;
- usage and provider IDs are finalized whenever the protocol provides them; absence is an explicit accounting status, never silent omission;
- cancel closes the remote stream and prevents further events from leaving the adapter.

Every provider request receives one stable `llm_request_id`, carried by every normalized event. Adapters consume through protocol EOF so OpenAI-compatible usage-only chunks with empty `choices` and Anthropic final-message usage are not dropped. Each stream produces exactly one accounting disposition before/with its terminal: provider-final usage when available, otherwise explicit `partial|unavailable`; cancellation and failure may not silently become zero cost. L3 remains the sole owner of `cost.recorded` and calls an idempotent `CostRecorder.record_once(llm_request_id, ...)`. Its L2 transaction claims the request ID and appends the event through `append_event_in_transaction()`; replay returns the existing accounting result. The event carries provider/model/response ID, usage status, and known token counts. The cost canary is extended from `.chat()` to `.chat_stream()`/typed stream consumers, and dynamic fixtures cover completion, cancellation, failure, usage-only terminal chunks, and duplicate replay.

Concurrent ResponseRuns never share the current mutable `LLMClient`. Before the intent pump becomes concurrent, L3 adds `LLMSessionFactory.create(preset_snapshot) -> LLMRequestClient`. Each request client owns an immutable provider/model/preset snapshot and request-local `_last_*` metadata; shared provider SDK transports may be pooled only if their API is concurrency-safe. Runtime routing never calls a shared `switch_model()`.

Provider fallback is prefix-safe:

- before the first EmissionPermit, a failed stream may retry through the old full `chat()` path;
- after any permitted segment, automatic restart from the beginning is forbidden;
- v1 ends the response with a concise limitation or silent panel error instead of repeating/contradicting speech;
- continuation from the committed prefix is a later optimization and requires a dedicated test/eval.

### D5. Incremental sentence assembly is syntax-aware

`ResponseStreamEngine` owns a stateful segment assembler. It buffers deltas until a stable boundary and understands at least:

- Chinese and English sentence punctuation;
- decimals and abbreviations;
- URLs and email addresses;
- Markdown code fences and inline code;
- XML-like text from old prompts;
- maximum buffer size and forced safe boundary.

Code blocks, URLs, raw tool JSON, XML control tags, and incomplete Markdown are not spoken directly. The assembler yields a semantic candidate; the stream gate decides whether it may become a `ResponseSegment`.

Document candidates may use the configured larger text bound. Speech candidates use safe subclause boundaries and an initial maximum of 60 Unicode code points or roughly 2.5 seconds of expected speech. A forced split is itself a new candidate and must receive its own durable gate event and permit; the assembler never cuts an arbitrary buffer and sends it straight to TTS.

Legacy sentence-boundary fixtures are reused and extended. The old full-text `split_into_sentences()` stays for compatibility but is not the stateful streaming parser.

### D6. Commentary and final are different phases

Jarvis adopts the useful interaction pattern, not OpenAI's hosted pipeline:

- `commentary`: short acknowledgement or progress while real work continues;
- `final`: the actual answer after the required evidence/gates.

V1 commentary is primarily deterministic and lifecycle-driven:

| Observed truth | Permitted example |
|---|---|
| utterance accepted, route selected | “听到了。” |
| `action.dispatched` committed | “我开始处理了。” |
| `action.running` first observed | “任务已经在运行。” |
| `action.result_observed` committed | “结果回来了，我整理一下。” |
| `action.failed` committed | “这一步失败了，我告诉你具体原因。” |

Forbidden examples without corresponding evidence:

- “马上好。”
- “已经查到了。” before `action.result_observed`.
- timer-based fake progress when no lifecycle changed.

`PresentationIntent(acknowledge/progress/error)` is the ephemeral L3→L5 contract this D6 work introduced: the type lives in `jarvis/shared/realtime.py`, the four-row mapping from a committed action event in `jarvis/decision/commentary.py`, and delivery is a `phase="commentary"` ResponseRun opened by a runtime durable-cursor observer over the action types. Spec §3.6.3 owns its definition and field set (`intent_type`, `surface_hint`, `subject_ref`, `content_hint`, `freshness_required`); the table above only lists which of §3.6.3's `intent_type` values this ADR uses and the phrases they map to. A commentary segment may be durable as a delivered response segment, but its truth derives from the durable action event: the run's `source_event_id` IS that event. Repeated progress is coalesced; a timer may decide when to surface a new known state, but may not invent a new state.

A commentary run is the one exception to D1's "generation completion, not physical delivery, is the terminal condition". Its text is a fixed phrase, so generation has nothing to complete; the run instead stays open until `surface.playback_started` names it, and that is what makes "a newer lifecycle row cancels an unheard earlier phrase as `superseded`" reachable at all. A commentary already playing is past that point and finishes.

Commentary is always routine, short, interruptible, and independently permitted. Final output follows its own policy. A deep model is never called only to generate “我在查.”

### D7. Speech and document output are typed channels, not blocking XML regions

The current `<voice>...</voice><document>...</document>` parser requires complete closing tags and blocks first audio. New `ResponseSegment` carries:

```text
response_id
response_group_id
turn_id
sequence
phase                  # commentary | final
channel                # speech | document | both
text
output_risk_class
required_gate_mode
segment_hash
emission_permit
```

Routing:

- routine short answer: `channel=both`; the same permitted sentence reaches panel and TTS;
- complex task: short commentary uses a speech ResponseRun, while a sibling document ResponseRun may continue independently;
- long final: speech ResponseRun gives a concise summary; document ResponseRun carries details, links, code, and tables;
- system/queue-review turns retain `silent_log`/non-voice channel guards.

The old XML path remains for full-text compatibility during migration. It is not accepted on the new routine streaming route. Provider-facing prompts are changed route by route, not globally in one commit.

If Allen interrupts the speech run, the sibling document/action run may continue. If one run uses `channel=both`, cancellation stops only future delivery/generation for that run; already displayed panel text cannot be retracted. Routes that need independent future lifetimes must create sibling response IDs in the same response group.

### D8. Runtime consumes input concurrently and owns foreground arbitration

The runtime coordinator, inlined in `jarvis/runtime/inherent_loop.py` (ADR-0006 D1; no `realtime_session.py`), provides:

```text
accept_intent(trigger_event)
start_response(response_request)
supersede_foreground(new_turn_id)
interrupt_response(response_id, reason)
request_action_cancel(cancel_request)
handle_action_terminal(event)
shutdown()
```

It owns:

- the one foreground speech response;
- response group relationships;
- mappings from turns/responses to ActionRuns;
- in-memory bounded control/data queues;
- task creation and shutdown order.

It does not choose tools, output risk, or cancellation authorization.

Of this API only `accept_intent` and `start_response` have inline equivalents in substance: `_user_intent_watcher`'s claim-and-enqueue flow, and `_start_drive_turn_response` opening one durable ResponseRun through `start_response_run`. `supersede_foreground`, `interrupt_response`, `request_action_cancel`, `handle_action_terminal`, and `shutdown` are unbuilt.

`_user_intent_watcher` changes from “poll one event, await its complete turn” to:

1. poll the next committed trigger;
2. ask L3 to idempotently claim it by durable `turn.started`, with the trigger's `event_uid` in the Event's top-level `source_event_id`;
3. enqueue the claimed turn in a bounded runtime intent queue;
4. advance the watcher cursor only after the durable claim and queue acceptance;
5. continue polling while prior ActionRuns execute;
6. let the coordinator enforce one foreground speech response and explicit supersession.

`claim_turn_once(trigger_event_uid)` uses `BEGIN IMMEDIATE` to query/append one `turn.started` keyed by the source trigger and returns the existing claim on a duplicate. It reuses the server-minted `turn_id` already required on the canonical input payload and rejects a conflicting reuse; it does not mint a second ID. The existing `_handle_utterance` is changed to hydrate/pass through that claimed turn; it may not emit a second ordinary `turn.started`. Legacy callers also enter through `claim_turn_once`.

First adoption is bounded. Enabling the realtime consumer atomically records a `consumer.adopted(name="realtime_intent", adoption_row_id=MAX(events.id))` operational milestone; only input rows after that watermark are eligible for unclaimed recovery. Historical pre-adoption utterances are not replayed. Subsequent progress uses durable turn claims rather than the current startup-MAX shortcut.

This explicitly supersedes the current startup `MAX(events.id)` assumption for user-intent consumption. Startup reconciliation scans eligible post-adoption rows:

- input events with no turn claim → claim and enqueue;
- claimed turn with no response/action milestone → safe to re-enqueue;
- claimed turn with an open action → restore `waiting_action`, never redispatch the original action;
- claimed turn with an open response → ask L3 ResponseTerminalizer to close it as restart failure; never restart the old LLM blindly.

If the queue is full, the watcher leaves the event unconsumed and applies backpressure; it does not drop a durable utterance. A crash after claim but before processing is recovered by the above scan. Barge-in presentation control uses ADR-0006's separate in-memory priority queue and does not wait behind ordinary intents.

Group continuation is distinct from foreground supersession. When an ActionGroup barrier reaches its declared terminal/deadline, L3 uses the stable source action/barrier event UID to atomically claim at most one successor `response.started(phase="final", response_group_id=<same group>)`. A duplicate callback or restart scan returns the existing response ID instead of creating another final. Same-group successors enter a coordinator-owned semantic continuation queue and never call `supersede_foreground`; a background group may create and publish its document final while a newer group owns foreground speech. A speech successor uses ADR-0006 `enqueue_after_drain` only while that same group owns the active presentation lane. Otherwise it remains pending until explicit foreground policy selects it or degrades to document/silent delivery; it cannot displace another group. Startup reconciliation re-enqueues a durable action/barrier terminal only when its continuation claim has no final milestone.

Built (selection and refusal, not the document/silent fallback): `decide_foreground` in `jarvis/decision/response_run.py` is that explicit foreground policy, injected into the L5 media actor by `make_foreground_decision_callable` (`jarvis/runtime/__init__.py`) — the seam is what keeps `jarvis/surface` free of a `jarvis.decision` import. It selects a cross-group candidate only when that candidate's Event Log row id is later than the incumbent's; a same-group candidate is left to ADR-0006's drain lane.

The lane matches the mechanism to its state rather than weakening the policy. While the incumbent is live, a selected candidate supersedes it. Once the incumbent has committed its terminal the lane is draining, not occupied — the player already released its lease — so the selected candidate is queued for that draining lane instead (ADR-0006 amended accordingly) and is spoken when the incumbent's terminal debt clears. Either way the displacement is of a GROUP, so it discards that group's queued continuation, including an already-queued final; which of the two mechanisms applies must never change what the user hears, because it turns on a sub-millisecond timing accident. A candidate the policy does not select is declined in both states: terminalized, no playback opened, no terminal event beyond that, and no `pending` state — the "remains pending ... or degrades to document/silent delivery" half of the paragraph above is still unbuilt and waits on the `foreground_output` lease. No `supersede_foreground` method exists.

The current turn-driven migration needs a narrower completion guarantee before
that full continuation design is available. Semantic completion identifies its
consumed trigger durably on `turn.ended` (spec §5.4.4); a separate operational
consumption marker may optimize recognition but cannot be its sole proof.
Otherwise a marker write failure after completed effects would cause a held
terminal to be driven again when live ownership releases. A true orphan remains
eligible. This does not make partial decision effects, delivery, and provider
requests one crash-atomic transaction.

Turn concurrency is forbidden from relying on the current snapshot-only confirmation single-use check. The pure Pre-action Gate remains a provisional decision, but any confirmation-backed pass must commit through one L2 primitive:

```text
BEGIN IMMEDIATE
  re-read current confirmation slot and accepted event
  re-check expiry, scope, max_uses, and accepted-event identity
  claim source_confirmation_event_id exactly once
  append passing gate.evaluated
  insert authorized_dispatch_outbox(stable_action_id, gate_event_uid)
COMMIT
→ AuthorizedDispatch | AlreadyConsumed | Rejected
```

Consumption uniqueness is keyed by `source_confirmation_event_id`, not the freshly generated `lease_id`: duplicate handling of one `confirmation.accepted` must resolve to the same stable action/authorization claim. A loser cannot append a second passing gate or outbox row. This atomic claim is a prerequisite for enabling more than one concurrent intent worker and amends ADR-0012's sequential snapshot-only single-use mechanism.

Expiry is evaluated after the write lock is acquired at both authorization and
L4 outbox admission. Time spent waiting for SQLite cannot extend a lease: a
pre-lock clock sample would silently authorize work after its permission ended.

### D9. `spawn_worker` becomes a true ActionRun

L4 gains a provider-neutral execution boundary:

```text
ActionRunner.submit(ActionRequest) -> ActionHandle

ActionHandle
  action_id
  result() -> awaitable[RawResultBundle]
  cancel(reason) -> awaitable[CancelOutcome]
  progress() -> AsyncIterator[ActionProgress]   # optional capability
  cancellation_mode: unsupported | cooperative | terminate_process
```

Rules:

- submit returns after dispatch, not after the action finishes;
- a synchronous tool may return an already-completed handle;
- Codex and other long-running workers run in an owned executor/process/task and expose a real handle;
- `_dispatch_one_tool_call` consumes `ActionSubmission(handle, initial_events)`; it may not read a `RawResultBundle` until the handle completes;
- each job receives an explicit `ActionExecutionContext(running_event_uid, runtime_paths, lifecycle, resource_lease, stash_state, ...)` and opens/closes its own Event Log connection inside the worker;
- `action.dispatched` means accepted and possibly queued; `action.running` is emitted only after the runner obtains its execution slot and resource lease and actually begins;
- lifecycle events remain L4-owned;
- no ActionHandle calls L3 or L5 directly.

True asynchrony also transfers the current synchronous cleanup ownership:

- during the first migration slice, a legacy driver task may await the handle and continues to own live-action release plus verify-then-stash-restore;
- before action-result re-entry becomes fully event-driven, those duties move to an `ActionGroupFinalizer`/ActionRunner terminal finalizer;
- stash restore happens only after `verify_diff` reaches its terminal decision;
- the current live-action/stash canaries are adapted and kept.

`verify_diff` remains an independently gated and audited child ActionRun because its configured command is not safe to invoke as an ungated L4 helper. To avoid self-deadlock, L4 adds `resource_scope_id` and `parent_action_id`. After the normal L3 gate passes, a child may borrow the parent's existing resource lease only when L4 proves that every requested resource key is a subset of the parent's scope. The child receives no raw lease token, cannot expand or release the scope, and does not reacquire the repo lock. Its terminal unblocks verification but only the root action's cleanup terminal releases the lease. An unrelated same-repo action remains blocked through verification and stash cleanup; different repos may proceed. Verification-needed lifecycle events travel through runtime back to L3—an L4 finalizer never calls L3 directly.

Current migration limitation: a new root in the same turn that conflicts with a
predecessor's cleanup debt is promptly refused with an explicit instruction to
finish the turn before retrying. Turn cleanup waits for that turn's in-flight
jobs, so accepting a successor that waits for the predecessor's lease creates
a cycle, even if the predecessor already quiesced. Submission-order reservations
also cover predecessors still awaiting a lease. This refusal preserves physical
debt instead of borrowing a sibling scope or restoring the stash early; it is
not support for consecutive conflicting roots within one turn. Cross-turn
serialization and explicit read-only verification children remain supported.

Canonical action terminal, worker quiescence, and repository cleanup are three separate facts. `ActionExecutionContext` owns an explicit cleanup state until the resource is safe:

```text
worker_active → quiescing → worker_quiesced
worker_quiesced → verifying | verification_skipped(reason)
→ restoring_stash | conflict_surfaced
→ cleanup_completed | cleanup_failed
```

- `CancelOutcome.cancelled` is returned only after the child process/task is confirmed quiescent; if cancellation is unsupported or unconfirmed, the action remains running/pending and no `action.cancelled` is emitted.
- a supervisor `action.timeout_assumed` may become canonical while an OS process is still suspected alive, but its repo/resource lease is quarantined and not released until quiescence and cleanup finish.
- heartbeat and callbacks must stop before cleanup; late writes move cleanup to failed/quarantined.
- successful actions verify before stash restore; cancel/fail/timeout paths durably record `verification_skipped(reason)` before restore/conflict handling.
- `worker.quiesced` and `action.cleanup_completed/failed` are bounded L4 operational events used for restart reconciliation; they are not extra ActionLifecycle terminals.
- startup scans canonical-terminal actions lacking cleanup terminal events and re-establishes resource quarantine before accepting new conflicting work.

Concurrency is resource-leased from the first ActionRunner step, not postponed until parallel tool streaming:

```text
ToolConcurrency
  resource_keys          # canonical identifiers, e.g. realpath(repo)
  mode                   # read_shared | write_exclusive | global_exclusive
  independence_declared
  resource_key_resolver
```

- `spawn_worker` uses canonical realpath(repo) as a write-exclusive resource key;
- after `action.dispatched`/queued and before `action.running`, `resource_key_resolver(ActionRequest, L2 snapshot)` resolves all keys; resolution failure emits one `action.failed(reason="resource_key_resolution")` and never runs unlocked;
- two mutating actions in the same repo serialize, so neither can stash or verify the other's work;
- different repos may run concurrently;
- a mutating tool without declared resource semantics defaults to global-exclusive;
- `max_concurrent_runs` limits jobs only after resource leases are respected.

Canonical action truth is the L2 durable fold. The L4 transition FSM is a live validator/cache. Terminal races use the shared L2 atomic terminal-append primitive, not only that in-memory FSM:

```text
BEGIN IMMEDIATE
  check action_id for action.result_observed/action.failed/
                      action.timeout_assumed/action.cancelled
  append exactly one canonical terminal event if none exists
COMMIT
→ Event | AlreadyTerminal
```

If completion wins, a later cancel request returns `already_terminal`. If cancellation wins, a late result is counted only in bounded ephemeral late-worker telemetry, not as `action.result_observed` and not as a second canonical terminal event.

That telemetry is L4-owned, keyed by `(action_id, worker_epoch)`, capped at 100 records per action and evicted after the cleanup terminal or 10 minutes. It never releases a handle/resource; a detected late write instead forces `cleanup_failed` and keeps the resource quarantined.

### D10. Response cancellation and action cancellation are separate protocols

Current migration boundary: pending authorized-dispatch outbox rows have no daemon automatic recovery scan, so accepted admission does not guarantee eventual execution.

The shared contracts are complete and frozen:

```text
ResponseInterruptPolicy
  response_id
  policy_hash
  candidate_playback: duck | ignore
  confirmed_playback: interrupt_expected_playback_generation | ignore
  generation_action: cancel | continue
  action_action: never

ResponseCancelRequest
  request_id
  response_id
  expected_playback_generation_id?
  scope: foreground_output | generation
  reason
  source_utterance_id?
  policy_hash

CancelActionRequest
  request_id
  target_action_id
  caller_principal
  risk_level
  reason
  requested_by_turn_id
  source_event_uid
  authorization_gate_event_uid
  authorization_lease?
```

The `response.cancelled` reason vocabulary is closed
(`jarvis/shared/realtime.py`): `user_stop`, `superseded`, `shutdown`,
`operator_request`, and — since `keyword-ptt-safe-barge-in` — `barge_in`, so a
confirmed barge-in is distinguishable in the Event Log from a panel Stop
press. `confirmed_playback` now defaults to
`interrupt_expected_playback_generation` rather than `ignore`; a run may still
be started with `ignore`, and the runtime cancels nothing in that case. The
scope table is unchanged: `generation` is reused, `foreground_output` and its
playback lease are still absent, and that remains the stop-speech card's work.

L3 issues the interrupt policy when a ResponseRun starts. Runtime may only apply that policy mechanically; it cannot invent a cancel scope.

`expected_playback_generation_id` is required for `foreground_output` and
whenever an active playback lease exists, so the L5 interrupt is exact CAS.
It may be null only for a pure provider-generation cancel on a ResponseRun
that has no playback lease. ADR-0014 Stop/PTT controls always target active
speech and therefore require the non-null exact playback generation.

Response cancel scope has exact semantics:

| Scope | Playback/TTS | Provider LLM generation | ResponseRun terminal | Linked ActionRun |
|---|---|---|---|---|
| `foreground_output` | CAS-interrupt/abort | continue if a non-speech/document route still needs it | unchanged | continue |
| `generation` | CAS-interrupt/abort | cancel | `cancelled` if still open and terminal CAS wins | continue |

Natural barge-in uses `generation` for the foreground speech ResponseRun. A deliberate “mute speech but keep writing the document” control uses `foreground_output` and requires sibling/continuing document semantics.

Action cancellation reuses the existing gate without weakening its API. A raw LLM/string target is never trusted directly. L2 extends the existing `EntityRegistry` fold with a fourth durable namespace derived only from registered action lifecycle events. The opener is `action.dispatched` — the first durable event that says the action was admitted, so a proposal the gate refused never becomes a cancel target — and any of the four action terminals (`action.result_observed`, `action.failed`, `action.timeout_assumed`, `action.cancelled`) evicts the entry, so the `action:` namespace is exactly the non-terminal set:

```text
action.dispatched with action_id=A
→ EntityRegistryEntry(
     entity_id="action:A",
     entity_type="action",
     canonical="A",
     aliases=(),
     confidence="exact",
     source_event_id=<that action.dispatched uid>,
     last_seen_ms=<that event timestamp>,
   )
```

The same fold pass records each open action's admission — `action_id → {dispatched_event_uid, admission_gate_uid, lease_id?, run_id?}` — where `run_id` is joined only from `run.started` (matched by its `source_event_id`, the action's `action.running` uid, or its correlation `action_id`); `action.running` carries no `run_id` and is never read for one.

This does not make an action cancellable merely because its ID is known. L3's `resolve_cancellable_action()` reads the canonical ActionRun fold and returns an `ActionRef("action:<id>")` only for the single open action, or for the open action a qualified request names; several open actions are ambiguous (a clarifying question, never a confirmation), none is a plain answer, and a raw id that names no open action never resolves to itself. The same fold entry is present in the `EntityRegistry` snapshot given to `pre_action_gate()`. L4 performs a final current-state check before signaling the handle. Thus entity trust proves durable provenance; the ActionRun FSM proves current cancellability.

L3 then constructs a fully registered `cancel_action` gate candidate. The definition uses the current `ToolDefinition` shape exactly; it does not invent `internal_control`, `mutating`, or a target-derived definition:

```text
ActionRequest
  action_id=request_id
  tool_name="cancel_action"
  target_entity_ref="action:<target_action_id>"
  arguments={target_action_id, reason}
  caller_principal=CallerPrincipal.JARVIS_LLM
  risk_level="L2"
  authorization_lease=<lease when active policy requires one, else None>
  run_id?, turn_id, payload=<CancelActionRequest, flattened>

ToolDefinition(
  name="cancel_action",
  description="Request cancellation of one exact, currently cancellable ActionRun.",
  allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
  risk_level="L2",
  result_semantics="ack",
  is_async=False,  # command acceptance is sync; target quiescence is a separate lifecycle
  input_schema={
    "type": "object",
    "properties": {
      "target_action_id": {"type": "string"},
      "reason": {"type": "string"},
    },
    "required": ["target_action_id", "reason"],
    "additionalProperties": false,
  },
  handler=cancel_action_handler,
  domain="agent_control",
  read_only=False,
  requires_entity=True,
  requires_confirmation=False,
  post_action_check=None,
  result_budget_s=None,
)
```

L2 is the fixed risk for the cancellation command because stopping further effect is not assigned the target action's mutation risk dynamically. Under the default L3 confirmation threshold, Allen's explicit cancel utterance does not trigger a redundant second question. A deployment that puts L2 at/above its confirmation threshold must register the matching `requires_confirmation=True` variant at boot; existing policy validation rejects a mismatched definition. The target action's own risk and authorization never transfer to this new request.

That complete `ActionRequest + ToolDefinition + policy + ledger + entity_registry + pending_confirmations + action_admissions` passes the existing `pre_action_gate()`. L3 fills `CancelActionRequest.authorization_gate_event_uid` from the L2 admission lookup: the uid of the last passing `gate.evaluated(gate="pre_action")` row that carries the target's `action_id`. It is read from `gate.evaluated`, not from `action.dispatched.source_event_id`, because only the confirmation-backed outbox dispatch sets that source; the ordinary dispatch path leaves it unset and every unleased action would otherwise fail the match. The gate's `cancel_action` arm is the single checker of that uid — absent, unknown or mismatched refuses, never `confirm_required`, since re-granting cannot repair a stale uid — and nothing downstream re-checks it. The shipped mechanism is the ordinary gated dispatch of the registered `cancel_action` tool; there is no separate `dispatch_authorized_action_cancel` runtime entry point and no ungated shortcut. The control ActionRun synchronously acknowledges the runner's `CancelOutcome` (accepted / already terminal / unconfirmed / unsupported — an unconfirmed cancel is reported as not stopped, never as a success); the target ActionRun may emit `action.cancelled` only after process quiescence wins terminal arbitration.

ADR-0014 closes the remaining gate-to-dispatch crash window for every
external-effecting ActionRequest, including a confirmation-approved
deterministic re-proposal: the same L2 transaction that appends a passing gate
also inserts a bounded `authorized_dispatch_outbox` row keyed by the gate UID
and stable action ID. For a confirmation-backed request, that transaction also
performs the accepted-event consumption claim defined in D8; a previously
consumed acceptance cannot produce either the pass or the outbox row. L4 atomically establishes `action.dispatched` before a
handler may produce an external effect. Unknown post-dispatch outcomes use
tool-specific reconciliation; non-idempotent append/write handlers are never
blindly replayed. This outbox is operational debt, not an additional gate or
lifecycle truth.

#### “别说了” / natural barge-in

```text
BargeInSignal confirmed
→ runtime applies current ResponseInterruptPolicy
→ L5 interrupt_playback(expected_playback_generation_id) CAS
→ ResponseCancelRequest(response_id, scope=generation)
→ L3 ResponseTerminalizer + LLM stream cancel
→ response.cancelled only if generation was still open and terminal claim wins
→ surface.playback_interrupted
→ linked ActionRun continues
```

If generation had already completed, `response.completed` remains true; only playback is interrupted. The response lifecycle is not rewritten after terminal completion.

#### “把正在做的任务停掉”

```text
utterance.received
→ L3 resolves exact target action
→ construct registered cancel_action ActionRequest
→ existing Pre-action Gate / confirmation as required
→ CancelActionRequest(target_action_id, requested_by_turn_id, authorization_gate_event_uid, reason)
→ L4 ActionHandle.cancel()
→ exactly-one terminal arbitration
→ action.cancelled only if cancellation wins and is confirmed
```

When `action.cancelled` is emitted, `authorization_gate_event_uid` is supplied through the Event's top-level `source_event_id` argument. The gate event already links to the source utterance. Neither ID is duplicated as an event payload field.

Ambiguous “停一下” during speech defaults to presentation cancellation, not world-action cancellation. Action cancellation is never handled by the out-of-band media control path.

When final ASR later emits the text that caused a barge-in, ADR-0006's deterministic `InterruptUtteranceRouter` runs before confirmation grammar, Tier 0, or the LLM. A standalone stop phrase is control-only and deduplicates by `utterance_id`; it cannot create a second conversational answer or cancel an ActionRun. A compound stop-plus-new-request submits only the remainder as the new ordinary intent.

### D11. Reduce serial LLM rounds without hiding work

The desired latency improvement is not only first-token streaming. Complex tasks currently pay for repeated deep-model turns.

The tool-stream migration adds:

1. complete tool-call delta assembly before Pre-action Gate;
2. one LLM response may propose multiple independent calls;
3. calls run concurrently only when their tool metadata declares them side-effect-safe, independent, and resource-compatible;
4. dependent or consequential calls remain ordered;
5. result aggregation uses `ActionGroup(expected_action_ids, barrier_policy, deadline)` and waits for the planned terminal set or an explicit partial/deadline barrier before one follow-up model call;
6. a long worker returns `waiting_action` immediately and later action events re-enter the response coordinator;
7. fixed acknowledgement/progress uses no LLM call.

The first production streaming route is intentionally `tools=None`. Tool-call streaming lands only after provider adapters, cancellation, ActionRunner, and gates pass deterministic tests.

`result_group_window_ms` is only a UI/progress debounce and never decides that an action group is complete.

### D12. Route for quality first, then latency

No extra LLM call is introduced merely to decide which model to call. L3 derives a route from existing policy, risk, tool cues, active subject, and request shape:

| Request | Initial route |
|---|---|
| deterministic status/refusal/fixed confirmation | Tier-0 deterministic |
| conversational low-risk, no tool/evidence need | evaluated routine streaming preset |
| tool use, code work, research, ambiguous request | deep/action route + immediate truthful commentary |
| consequential/high-risk | deep route + full/structured gate |

The current `fast` preset is not enabled by intuition alone. It must pass the Jarvis scenario eval against the current deep baseline on:

- factual correctness;
- task/tool selection;
- adherence to evidence and confirmation rules;
- useful-answer score;
- refusal/uncertainty calibration;
- first-delta and total latency.

If the fast preset loses meaningful quality, routine streaming uses the stronger model and still benefits from first-sentence delivery. Complex work always keeps the deep model where it adds value.

## 4. Durability and delivery

### 4.1 Ephemeral hot path

These remain in memory:

- raw LLM token/tool argument deltas;
- partial sentence buffers;
- cancellation tokens;
- PCM and audio frames;
- playback cursor ticks;
- transient action progress callbacks.

Bounded queues are mandatory, but their full policies differ:

| Boundary | Capacity unit | Full behavior | Shutdown |
|---|---|---|---|
| provider → LLM assembler | events/bytes | pause provider reads; timeout cancels ResponseRun | cancel/close provider, discard unpermitted deltas |
| permitted segment → committed surface | semantic segments | never drop; backpressure, then fail response if commit/delivery budget expires | persist accepted permit outcome or fail before publish |
| TTS reader → player | PCM milliseconds | never drop/reorder; backpressure, then fail current speech response | CAS-interrupt generation, then close reader |
| per-client panel sender | envelopes/bytes | disconnect or drop live socket delivery; projection rebuild remains | close sender task; never block voice |
| barge-in control | coalesced control states | independent highest-priority lane; coalesce duplicate candidate signals, never queue behind data | apply/ack active CAS or mark stale |
| action callback → runtime | lifecycle messages | never drop canonical terminal; backpressure through owned worker channel | terminalize/reconcile before owner closes |

`InherentBroadcaster` uses one bounded queue and sender task per client rather than sequentially awaiting every `ws.send_json`. A slow client cannot delay another client, TTS, or cancellation.

### 4.2 Durable events

New L3 events:

```text
response.started
  required: response_id, response_group_id, turn_id,
            phase, channel, emission_mode, output_risk_class,
            required_gate_mode, policy_hash, active_subject_ref,
            evidence_snapshot_hash
  optional: provider, model, corrects_response_id, route

response.completed
  required: response_id, response_group_id, turn_id, response_hash
  optional: provider_response_id, generated_text_hash, segment_count

response.cancelled
  required: response_id, response_group_id, turn_id, reason
  optional: interrupted_by_utterance_id, interrupted_by_turn_id, generated_text_hash,
            committed_prefix_hash, cancel_scope

response.failed
  required: response_id, response_group_id, turn_id, reason
  optional: provider_response_id, retryable, committed_prefix_hash

gate.evaluated (additive fields for gate=stream_emit)
  required on realtime stream path: response_id, sequence, segment_hash,
            candidate_risk, policy_hash, evidence_snapshot_hash,
            outcome, reasons

consumer.adopted (L2 operational milestone)
  required: name, adoption_row_id

worker.quiesced (L4 operational, non-terminal)
  required: action_id, worker_epoch
  optional: run_id, exit_code, reason

action.cleanup_completed (L4 operational, non-ActionLifecycle-terminal)
  required: action_id, worker_epoch, verification_outcome
  optional: stash_ref, resource_keys

action.cleanup_failed (L4 operational, non-ActionLifecycle-terminal)
  required: action_id, worker_epoch, reason
  optional: stash_ref, resource_keys, quarantine_reason
```

Additive optional fields on existing L5 events:

```text
surface.response_open:
  session_id, response_id, response_group_id, playback_generation_id?,
  phase, channel, emission_mode

surface.response_chunk:
  session_id, response_id, response_group_id, playback_generation_id?,
  sequence, phase, channel, segment_hash, permit_hash

surface.response_emitted:
  session_id, response_id, response_group_id, playback_generation_id?
```

Additive optional fields on `action.cancelled`:

```text
requested_by_turn_id
cancel_scope
cancellation_mode
```

All additions are optional on existing event types, preserving schema version 1 compatibility. A realtime-emitter conditional validator/canary nevertheless requires session/response/group/sequence/phase/channel/hash fields as applicable, requires `playback_generation_id` only for a speech playback route, and verifies `EmissionPermit.source_gate_event_uid == Event.source_event_id`. The causal source is not duplicated in payload. New `response.*` and `consumer.adopted` types receive their own version 1 registrations.

The causal chain is fixed:

```text
turn.started.source_event_id                  -> input trigger event
response.started.source_event_id              -> turn.started or source utterance
gate.evaluated.source_event_id                -> response.started
surface.response_chunk.source_event_id        -> gate.evaluated(stream_emit)
response terminal source_event_id             -> response.started or cancel source
surface playback checkpoint/terminal source   -> surface segment / response lifecycle event
action.cancelled.source_event_id               -> authorized cancel gate/source utterance
```

ADR-0006 adds `surface.playback_checkpoint/completed/interrupted/failed` and the heard-state projection.

### 4.3 Commit before publish, then direct delivery

Permitted semantic segments are low-frequency enough to persist. To avoid waiting for the SQLite polling interval and still preserve audit:

```text
L3 commits gate.evaluated(stream_emit)
→ L3 issues EmissionPermit bound to gate event_uid
→ L5 surface emitter appends surface.response_chunk
→ commit returns canonical Event.event_uid
→ CommittedEventBus publishes that exact committed event in memory
→ TTS may process the exact event immediately under its active playback lease
→ panel bus delivery only wakes InherentViewSequencer
→ sequencer rereads Event Log order and is the sole durable panel producer
→ same-process SQLite watcher remains a fallback wake source
```

Panel projection/delivery deduplicates by `(event_uid, response_id, sequence)`
inside the sequencer and is never conditioned on an active playback lease.
Speech consumers additionally validate the optional
`playback_generation_id` against the active L5 lease:

- append fails → nothing is spoken;
- append succeeds but direct publish fails → the watcher wakes both
  sequencer and eligible current-boot TTS recovery; panel truth is not lost;
- direct publish succeeds then watcher sees it → duplicate wake/delivery is
  ignored by sequencer cursor and speech event identity;
- daemon restarts → historical events rebuild projections/panel state, never old speech.

The TTS consumer accepts only tuples registered in the current boot's active-playback registry. Inside the daemon startup barrier, it executes `SELECT COALESCE(MAX(id), 0) FROM events` once and stores the result as `boot_high_water_event_log_id`; wall-clock time and `event_uid` are never replay-order boundaries. The watcher query may speak an event only when `events.id > boot_high_water_event_log_id` **and** its `(session_id, response_id, playback_generation_id)` tuple is still live in `ActivePlaybackRegistry`. It never speaks a pre-boot `surface.response_chunk`. Panel clients use projection reconstruction rather than pretending old events are a live stream. Cross-restart guaranteed delivery would require a separate durable consumer-offset/ack protocol and is not claimed here.

Token deltas never enter this path. Only permitted semantic segments are durable and publishable.

### 4.4 Conversation reconstruction

Future L3 context distinguishes:

- spoken assistant context: only the `heard_text` boundary from ADR-0006;
- panel-available document context: committed/published for a panel but delivery is unknown;
- panel-acknowledged context: only when a future reliable client acknowledgement exists; v1 does not assume this;
- generated but neither played nor displayed: audit-only, excluded from conversational history;
- action state: derived independently from L4 events.

V1 may expose a panel-available artifact/reference to L3 as such, but it must not label it user-seen or spoken.

This is wired, not merely projected. `SituationPacket` carries three bounded, typed collections—`spoken_heard`, `panel_available`, and `audit_only`—and the ordinary/stream prompt builders consume those labels without flattening them into one assistant-history string. `spoken_heard` advances only from ADR-0006 playback checkpoints; `panel_available` is explicitly described as available on screen but not known-seen; `audit_only` is excluded from conversational prompt history.

No fabricated `[Interrupted by user]` user message is inserted. The interruption is represented by `response.cancelled` and `surface.playback_interrupted` metadata linked to the real new utterance.

On daemon restart, ownership remains in the layer that owns the truth:

- runtime calls L3's response reconciler; L3 terminalizes an open ResponseRun once as `response.failed(reason="daemon_restart")`;
- runtime invokes the existing L4/L6 action reconciliation ports for open ActionRuns;
- runtime asks L5 to close any active playback state; L5 PlaybackTerminalizer owns its event;
- the ResponseLedger reconstructs the last heard checkpoint and panel-available state;
- no old response generation or PCM automatically resumes.

## 5. File-level change map

This section is a historical seed for goal cards under `docs/goals/`, not an acceptance contract; §9 Verification and SLOs and §13 Definition of done remain binding.

### New files

- `jarvis/shared/realtime.py` — extends ADR-0006's base session IDs/messages with ResponseSegment, gate, interrupt, and cancel contracts; it does not create a competing file/schema.
- `jarvis/decision/response_stream.py` — ResponseRun, route policy, incremental assembler, segment gate.
- `jarvis/state/cost_accounting.py` — L2 idempotency claim and same-transaction Event Log append for one cost disposition per `llm_request_id`.
- `jarvis/execution/action_runner.py` — ActionRunner/ActionHandle and terminal arbitration.
- `tests/fixtures/llm_streams/` — provider event fixtures.
- `scripts/bench_realtime_turn.py` — deterministic/live stage trace and JSONL summaries.

### Existing files that must change

- `jarvis/decision/llm.py` — typed async stream adapters, cancellation, and immutable per-ResponseRun request-client factory.
- `jarvis/decision/__init__.py` — route selection, no-tool stream entry, later tool loop migration, final ResponsePlan integration, and L3-owned `CostRecorder` invocation.
- `jarvis/decision/gates.py` — explicit stream policy/permit gate; complete high-risk classification.
- `jarvis/decision/packet.py` — add bounded spoken-heard, panel-available, and audit-only response context to `SituationPacket`.
- `jarvis/decision/conversation.py` (ADR-0006 §7) and `jarvis/decision/response_stream.py` — build prompts from the typed context; never flatten panel-available into heard speech.
- `jarvis/decision/policy.py` — replace `interrupt_policy` placeholder with real response/action distinction.
- `jarvis/execution/tools.py` — dispatch through ActionRunner; do not block on declared background work.
- `jarvis/execution/codex_action.py` — expose a cancellable owned process/task handle.
- `jarvis/execution/codex_client.py` — cancellation/progress seam where supported.
- `jarvis/runtime/inherent_loop.py` — non-blocking intent pump, committed-event direct bus, the inlined coordinator (ADR-0006 D1).
- `jarvis/runtime/__init__.py` — preserve old `drive_turn`; add realtime entry and action-result regrouping.
- `jarvis/state/event_log.py` — response registry and additive optional fields.
- `jarvis/state/lifecycle_terminal.py` — reuse/extend ADR-0006 Step 2's same-transaction terminal append primitive; do not create a second arbiter.
- `jarvis/state/projections.py` — carries the conversation fold from `jarvis/state/conversation.py` (ADR-0006 §7) plus ActionRun-derived canonical `action:` entries in `EntityRegistry`.
- `jarvis/surface/cli_render.py` — compatibility renderer plus response IDs/channels.
- `jarvis/surface/sentence_splitter.py` — keep old full-text helper; expose/reuse shared boundary fixtures.
- `jarvis/surface/inherent_output.py` — response/group/phase/channel envelopes and slow-client isolation.
- `jarvis/surface/voice_tts.py` — consume permitted committed segments through ADR-0006.
- `config/jarvis.yaml` — route/model/progress/concurrency rollout settings.

## 6. Configuration

Canonical configuration is the top-level `realtime:` block in `config/jarvis.yaml`; ADR-0006 §5 owns the master `realtime.enabled` gate and the Wave-1 `concurrency_safety` primitives. Keys this ADR owns:

- `realtime.response.{response_run_lifecycle,independent_response_cancel,cancel_timeout_ms}`, plus `typed_conversation_history`, which the reader (`Wave4ResponseFlags` in `jarvis/shared/realtime.py`) supports but the yaml does not yet set — response lifecycle, cancellation, and typed heard/available history. `independent_response_cancel` and `typed_conversation_history` both require `response_run_lifecycle`; `jarvis/runtime/__init__.py` downgrades an invalid combination once, with one warning, to the legacy batch path.
- `realtime.response.routine_streaming.enabled` (Step 8, default false) — the D2 `routine_stream` route for a pre-routed `casual_or_explanatory` turn; requires `response_run_lifecycle` and downgrades the same way. The pre-route's tool cues live in `config/tool_cues.yaml` next to the Tier 0 patterns.
- `realtime.actions.{action_runner,true_async_workers,max_concurrent_runs,lease_timeout_s}` — ActionRunner dispatch and true-async workers; `true_async_workers` requires `action_runner`.
- `realtime.input.{intent_pump,queue_capacity,max_concurrent_turns}` — D8's durable-claim intent queue.

There is no `realtime.routing.*` or `realtime.progress.*` block and no `undeclared_mutation_mode` key, in config or code. `default_resource_key_resolver` in `jarvis/execution/tools.py` hardcodes the fail-closed `global_exclusive` mode for any mutating tool that declares no resource semantics; a knob whose only legal value is its default is not configuration. Preset routing and spoken-progress throttling remain unbuilt.

## 7. Failure modes

| F# | Failure | Required behavior |
|---|---|---|
| F1 | stream fails before first permit | retry old full `chat()` path if budget allows |
| F2 | stream fails after spoken prefix | never restart from beginning; close with limitation/failure at next safe boundary |
| F3 | segment gate times out or lacks context | buffer; downgrade to full-text path |
| F4 | risk upgrades mid-run | stop issuing permits; spoken prefix stays immutable; buffer/fail remainder |
| F5 | tool-call JSON incomplete | no ActionRequest; response fails/repairs before any action |
| F6 | tool delta arrives after ordinary text | only separately permitted commentary may have spoken; freeze other content |
| F7 | ActionRunner submit fails | emit one `action.failed`; ResponseRun produces truthful failure final |
| F8 | cancel and result race | transaction selects one canonical terminal; loser returns already-terminal |
| F9 | action does not support cancel | keep action running; say so; never emit `action.cancelled` falsely |
| F10 | input queue full | do not advance durable cursor; barge control queue remains independent |
| F11 | slow/dead panel client | isolate/drop live socket delivery; projection remains panel-available; TTS unaffected |
| F12 | Event Log append fails | segment is not published or spoken |
| F13 | direct publish fails after commit | panel sequencer recovers the committed row from Event Log regardless of playback; TTS watcher recovery is allowed only for the current-boot active `(response_id, playback_generation_id)` lease |
| F14 | daemon restarts with open response | reconcile once to `response.failed`; never resume old LLM stream |
| F15 | progress event repeats | coalesce by action state/time; never synthesize timer-only claims |
| F16 | full/high-risk content leaks to TTS | canary/test failure; rollout blocked |
| F17 | crash after turn claim but before processing | startup scan re-enqueues only when no response/action milestone exists; otherwise owner-specific reconciliation |
| F18 | same-repo mutating actions arrive together | write-exclusive canonical repo lease serializes them; no cross-stash/diff pollution |
| F19 | shared mutable LLM preset changes mid-run | impossible: each ResponseRun owns an immutable request-client snapshot |
| F20 | response/playback completion races cancel/restart | owner terminalizer + L2 CAS chooses one terminal; loser is `AlreadyTerminal` |
| F21 | action group has one slow member | expected-action barrier waits or reaches declared deadline; UI debounce never triggers an incomplete final |
| F22 | cancel action cannot resolve one canonical live `ActionRef`, populate the current registered tool shape, or pass entity trust | refuse cancellation request; never call handle or bypass Pre-action Gate |
| F23 | action terminal occurs before worker quiesces/cleanup | keep resource quarantined; emit operational cleanup outcome before lease release |
| F24 | first realtime adoption sees historical intents | adoption row ID excludes all pre-adoption events from recovery |
| F25 | two concurrent turns see the same accepted confirmation | accepted-event consumption CAS permits exactly one passing gate/outbox; loser returns `AlreadyConsumed` and dispatches nothing |
| F26 | verification child targets the repo lease already held by its parent | validated child borrows the parent resource scope without reacquiring; unrelated same-repo work remains blocked until root cleanup |
| F27 | stream completes/cancels/fails without provider usage | L3 records one explicit `partial|unavailable` cost disposition for the stable `llm_request_id`; never omit accounting or silently record a known-zero cost |

## 8. Build order

This section is a historical seed for goal cards under `docs/goals/`, not an acceptance contract; §9 Verification and SLOs and §13 Definition of done remain binding.

This order is designed for one small commit per step, Tier 1 green at every commit, and feature flags off until acceptance. Per repository policy, Python verification uses canaries, data-driven regression checks, integration/scenario harnesses, fixtures, and required live burns; it does not recreate `tests/unit` or add new Python unit tests.

The 2026-08-31 read-only shadow audit of 70 complete production turns provides only bounds, not a claimed permit hit rate: 24/70 were structurally no-action/sentence/allow/empty-claim candidates; a broader conservative lexical proxy left 22/70 (31.4%); adding 25 action turns that could receive deterministic lifecycle commentary gives a 67.1% upper bound for **any** early feedback. Historical rows lack the new `ResponseRiskContext` and first stable candidate, so exact fail-closed replay is 0/70 today. Step 0 must report three separate read-only metrics—`first_model_candidate_permitted`, `any_truthful_early_feedback`, and `buffer_full_text` reason histogram—before Steps 7–8 investment or rollout decisions. It reads a copy or SQLite `mode=ro` and never writes the production log.

| Step | Change | Verification |
|---:|---|---|
| 0 | Baseline quality/latency eval; add request-sent, first-delta, first-permit, TTS, playback, tool-wait traces; run the read-only permit/feedback shadow report above | repeatable JSONL report on routine/deep/tool scenarios; three distinct hit-rate/reason metrics; no behavior change or production DB writes |
| 1 | Extend shared realtime contracts; add response/gate schemas, no-commit transactional event append, shared L2 lifecycle-terminal CAS, ResponseRun FSM/terminalizer, accepted-event confirmation consumption CAS, durable input claim, ActionRun-derived `action:` EntityRegistry fold, and recovery scan with no behavior change | two-connection terminal/confirmation barrier races; rollback leaves event+outbox empty; ADR-0006 playback-CAS compatibility; registry/causal-chain/ActionRef and crash-window input checks |
| 2 | Add immutable `LLMSessionFactory`; wrap existing full `drive_turn` in ResponseRun lifecycle and independent response cancellation without streaming | concurrent preset snapshots isolated; exactly-one response terminal; legacy output unchanged |
| 3 | Add ActionRunner/ActionHandle/ExecutionContext, same-transaction action terminalizer, resource-key resolver/leases, validated child resource-scope borrowing for `verify_diff`, quiescence/cleanup events; adapt synchronous tools/RawResult ownership while driver still waits | result/cancel/timeout races; parent→verify no self-wait; unrelated same-repo serialization through cleanup; different-repo parallel; resolver/scope escalation locked out; DB/stash/live-action canaries |
| 4 | Make `spawn_worker` truly background; move quiescence/verify/stash/live-action cleanup and quarantine release to owned finalizer; change watcher into adoption-watermarked durable-claim intent pump | long fake action + second utterance; cancel/timeout live-process cleanup; crash after claim; historical inputs not replayed; no double dispatch |
| 5 | Map real action lifecycle to deterministic acknowledge/progress/error PresentationIntents | no false progress; coalescing; system-turn channel guard |
| 6 | Implement typed cancellable LLM adapters, stable `llm_request_id`, EOF usage normalization, exactly-once L3 cost disposition, and provider fixtures, unused by production route | OpenAI/Anthropic text/tool/usage-only/error/cancel fixtures; static stream-cost canary; each request has exactly one accounting disposition |
| 7 | Add incremental assembler, versioned local `SegmentRiskClassifier`, explicit risk context/outcome, durable stream-gate audit, permits, and suffix-only finalization | punctuation/Markdown and bilingual risk tables; classifier p95/hard cap; causal gate chain; routine permits; unknown/timeout buffers; full/high-risk zero early output; correction run |
| 8 | Enable true streaming for `tools=None` routine route through CommittedEventBus and ADR-0006 TTS | first permitted sentence arrives before LLM completed; duplicate delivery impossible |
| 9 | Add typed speech/document sibling ResponseRuns; remove XML dependency from new route | speech starts while document continues; speech cancel leaves document/action per policy |
| 10 | Assemble streamed tool calls, gate only complete args, build `ActionGroup` barriers, and dispatch only resource-compatible declared-independent calls concurrently | malformed args no dispatch; parallel vs ordered/resource-conflict plans; slow-member barrier; one grouped follow-up |
| 11 | Migrate action-result re-entry and complete ActionGroup finalizer ownership; reduce serial deep-model rounds | multi-tool scenario uses bounded planned LLM rounds; verify-before-stash-restore; verified final unchanged |
| 12 | Run quality non-inferiority eval; decide fast/deep routing and defaults | quality gates pass; warm latency/SLO burns; rollback tested |

Combined program order with ADR-0006:

```text
observability + deterministic fixtures
→ IDs/FSM/event schemas + three terminal arbiters + durable input claim
→ generation-CAS player/ledger + single-reader streaming TTS + async media owner (0006 Steps 2–4)
→ AudioDuplexBackend + single AudioIngress (0006 Step 5)
→ ActionRunner execution context + repo lease + immutable LLM request client (0008)
→ true background worker + durable concurrent intent pump (0008)
→ truthful lifecycle progress (0008)
→ keyword barge-in (0006)
→ no-tool safe LLM streaming (0008)
→ typed speech/document + tool streaming (0008)
→ partial ASR / semantic endpoint / natural headphone barge-in (0006)
→ speaker AEC qualification (0006)
```

This order produces useful latency improvements early without making natural full duplex depend on unfinished action concurrency or unsafe token streaming.

## 9. Verification and SLOs

### 9.1 Trace definitions

Every trace uses correlation IDs and a monotonic clock:

```text
utterance_committed
intent_queue_accepted
response_started
llm_request_sent
llm_first_delta
first_stable_segment
first_emission_permit
surface_segment_committed
tts_text_pushed
tts_first_pcm
first_pcm_accepted
first_nonzero_buffer_submitted
estimated_first_audible
action_dispatched
action_running
action_result_observed
response_completed/cancelled/failed
```

Provider time, Jarvis local overhead, tool time, and playback time are reported separately. “10 seconds total” without stage timing is not an acceptable benchmark result.

### 9.2 Initial product targets

| Metric | Target |
|---|---:|
| utterance committed → LLM request sent, local p95 | ≤ 75 ms |
| LLM first delta → first stable permitted segment, p95 | ≤ 500 ms for normal prose |
| permitted segment commit → TTS push, local p95 | ≤ 30 ms |
| action.dispatched → progress intent created, local p95 | ≤ 50 ms |
| action.dispatched → truthful audible acknowledgement, warm p95 | ≤ 1.5 s |
| new utterance accepted while a long ActionRun exists, local p95 | ≤ 200 ms |
| routine end-of-utterance → first meaningful audio, warm p50 / p95 | ≤ 2.0 s / ≤ 3.5 s |
| false lifecycle progress claims | 0 |
| high-risk/consequential segment emitted before required gate | 0 |
| response barge-in that cancels linked action | 0 |
| duplicate direct+watcher segment delivery | 0 |
| action with two canonical terminal events | 0 |

Cloud provider TTFT is reported separately and may breach the end-to-end target; route/model/provider changes are considered only with quality eval evidence.

### 9.3 Quality gates

The realtime route cannot become default unless it is non-inferior to the current path on the project scenario set:

- deterministic and routine factual correctness;
- correct tool selection and arguments;
- evidence/verified-claim discipline;
- confirmation and high-risk refusal behavior;
- action success and result interpretation;
- useful-answer human rubric;
- interruption follow-up coherence using only heard speech;
- no regression in document completeness for complex tasks.

Fast preset adoption is a separate decision inside Step 12. Streaming the strong model is acceptable if it preserves quality but misses an aggressive TTFT target; silently lowering quality is not.

### 9.4 Required deterministic checks

- ResponseRun legal/illegal transitions and exactly-one terminal;
- response/action/playback completion-vs-cancel-vs-restart terminal races;
- two connections concurrently consume one accepted confirmation; exactly one accepted-event claim, passing gate, and dispatch outbox exist even when duplicate handlers propose different random lease/action IDs;
- transaction fault injection proves no event/outbox row survives rollback and no direct-bus publish occurs before commit;
- injected crash at terminal transaction boundaries never leaves a durable claim without its canonical terminal event;
- cancellation before first delta, mid-sentence, and after permit; after `response.completed`, only playback may become interrupted and the response terminal remains completed;
- OpenAI and Anthropic tool-call delta assembly;
- OpenAI empty-choices usage-only chunk and Anthropic final-message usage; completion/cancel/failure/replay each yield exactly one `cost.recorded` disposition per `llm_request_id`;
- incomplete/malformed tool args never dispatch;
- incremental boundaries across delta splits, decimals, abbreviations, Chinese punctuation, URLs, Markdown, XML;
- `routine_stream` permits only complete stable segments;
- every permit has a committed `gate.evaluated` source with matching segment/policy/evidence hashes;
- routine-route segments that contain completion/mutation/evidence claims derive consequential/unknown and buffer;
- `full_text/structured/high_risk` produce zero early surface/TTS events;
- committed spoken prefix cannot be rewritten by finalization;
- invalid committed prefix creates a new correction ResponseRun rather than whole-answer retry;
- direct bus + SQLite replay deduplication;
- crash before/after durable turn claim, with no lost or duplicate turn;
- first-adoption watermark ignores historical utterances; claimed `_handle_utterance` emits no second `turn.started`;
- long action plus second/third utterance concurrency using isolated LLM request clients;
- same-repo mutating workers serialize and different-repo workers may run concurrently;
- a gated `verify_diff` child borrows only a subset of its parent resource scope without reacquiring; child terminal cannot release the root lease and scope escalation fails closed;
- dynamic resource-key resolution failure never executes a tool unlocked;
- DB connection, running-event UID, verify, stash restore, and live-action ownership survive background completion;
- cancel-before-verify, timeout-with-live-process, late-worker-write, and cancelled-stash cleanup keep resource quarantine until cleanup terminal;
- response cancel leaves ActionRun alive;
- explicit cancel action respects confirmation and exactly-one terminal race;
- `action:` EntityRegistry entries arise only from registered durable action lifecycle events; raw LLM IDs, unknown actions, terminal actions, and ambiguous “current task” references cannot become a cancellable `ActionRef`;
- registered `cancel_action` supplies every current `ToolDefinition` and `ActionRequest` field, passes the existing entity-trust/lease checks, and `action.cancelled.source_event_id` equals its authorization gate event UID;
- lifecycle progress is emitted only after corresponding committed event;
- slow WebSocket client cannot delay TTS/control;
- each panel client has an isolated bounded sender queue and overflow/disconnect behavior;
- ActionGroup waits for expected terminals/deadline, not a debounce timer;
- daemon restart closes open response and reconstructs heard-checkpoint/panel-available context without replaying speech; TTS recovery requires `events.id > boot_high_water_event_log_id` plus a live current-boot registry tuple, and never compares timestamps or orders by `event_uid`.

### 9.5 Live burns

- simple conversation on evaluated routine model: first-sentence and completion latency;
- deep no-tool answer: quality plus streamed first sentence;
- 30–60 second Codex/web action: immediate acknowledgement, new utterance accepted, result final later;
- two independent tools: parallel action timing and one grouped follow-up model call;
- interruption before/after LLM completion and before/after TTS completion;
- MiniMax/provider failure before and after a permitted segment;
- daemon restart during open response and while ActionRun continues/reconciles;
- text panel disconnected/slow while voice continues.

## 10. Spec changes and explicit deviations

1. Spec §3.4.13's sentence/full-text/structured gate remains authoritative. This ADR adds a conservative pre-route policy, explicit segment risk derivation, durable `gate.evaluated(stream_emit)`, and per-segment permit; it does not weaken full/high-risk gates.
2. Spec §3.6.3's L3→L5 ephemeral PresentationIntent is defined there and is now in code as `jarvis.shared.realtime.PresentationIntent`, still a contract object and never an Event Log row; action lifecycle is its truth source.
3. Spec §3.6.6 remains: L5 consumes permits and does not decide risk.
4. ADR-0003 post-hoc replay remains the compatibility path but is superseded as the target default for routine realtime responses.
5. The ADR-0003 deferred `surface.response_reset` approach is not adopted for voice because already-heard output cannot be reset.
6. Spec §5.2/§5.4 gains `response.started/completed/cancelled/failed` and additive stream-gate/surface fields with the causal chain defined in §4.2.
7. Spec §6 gains response/heard/panel-available projection semantics; Event Log commit alone is not user-visible acknowledgement.
8. `surface.response_*` remain L5 delivery events; new `response.*` events represent L3 generation lifecycle.
9. Action lifecycle taxonomy/validation stays in L4 while canonical terminal truth is the L2 fold. Response cancellation and action cancellation are formally separate.
10. The existing `interrupt_policy` placeholder becomes `ResponseInterruptPolicy` before keyword/natural barge-in is enabled.

## 11. Consequences

### Positive

- Jarvis can speak a safe first sentence without waiting for full generation.
- Long tasks feel alive through truthful progress while the deep model/action keeps working.
- A new utterance no longer waits behind a 600-second “async” worker.
- Answer quality and safety stay in L3; L5 remains a replaceable surface.
- Tool waits become observable lifecycles instead of silent serial model loops.
- Speech and document can have different lengths and cancellation lifetimes.
- The Event Log remains the durable truth while the hot path avoids polling latency.

### Costs

- L3 gains a second, incremental response path alongside the old full path during migration.
- Provider stream normalization and cancellation require substantial fixture coverage.
- True background actions require explicit ownership, shutdown, cancel capability, and terminal arbitration.
- Once speech is emitted, it is immutable; stream policy must be conservative.
- Quality/latency routing must be evaluated continuously as models and providers change.

## 12. Out of scope

- Hosted speech-to-speech or OpenAI Realtime API integration.
- Natural speakerphone barge-in without ADR-0006 AEC acceptance.
- Streaming raw chain-of-thought or hidden reasoning.
- Free-form model-generated progress in v1.
- Cross-device realtime audio transport.
- Replacing the full text/CLI/scenario path before the new route proves parity.
- Automatically resuming an interrupted cloud stream from an arbitrary token boundary.

## 13. Definition of done

ADR-0008 is complete only when:

1. ResponseRun, response events, typed cancellable LLM streams, durable per-segment gate audit, and source-bound permits are implemented.
2. `spawn_worker` is truly background and new utterances are processed while it runs.
3. Routine no-tool output reaches the surface/TTS before full LLM completion.
4. Consequential/high-risk routes produce no speculative output.
5. Response cancellation never implies action cancellation.
6. Explicit action cancellation uses a real handle and exactly-one terminal arbitration; response and playback have equivalent owner-specific terminalizers.
7. Commentary is backed by committed lifecycle truth and never fakes progress.
8. Speech/document sibling runs work without the streaming route waiting for complete XML.
9. Durable intent claims/recovery, repo resource leases, per-response immutable LLM clients, and action verify/stash ownership pass their crash/concurrency canaries.
10. Quality gates, Tier 1 tests, scenario tests, and required live burns pass.
11. Legacy fallback and rollback switches are proven.
12. Every full and streamed provider request has exactly one L3-owned cost accounting disposition, including cancel/failure with unavailable usage.
13. Allen changes this ADR's status to Accepted/Approved.
