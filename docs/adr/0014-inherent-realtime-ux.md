# ADR-0014 — Inherent Realtime UX and Recoverable Swift Contract

**Status:** Approved (2026-08-31, Allen)
Approved means the design is approved for implementation; implementation completeness is tracked only by §18 Definition of done.
**Date:** 2026-08-31
**Depends on:** ADR-0003 (Inherent text surface), ADR-0005 (voice foundation), ADR-0006 (full-duplex media and playback truth), ADR-0008 (ResponseRun and real-time response streaming), ADR-0009 (resident runtime), ADR-0012 (durable confirmation flow).
**Completes:** the physical Inherent/Swift client, recoverable panel delivery, and user-control contract intentionally left underspecified by ADR-0006 and ADR-0008.
**Supersedes/amends:** ADR-0003's outbound-only, best-effort `open/append/done` client protocol as the target Inherent path; ADR-0008's ambiguous use of one `generation_id` for all surface deliveries; and ADR-0012's lazy-only expiry for a confirmation currently visible on a connected v2 client. The old endpoint remains a compatibility and rollback surface.
**Does not use:** OpenAI Realtime API. Jarvis owns the WebSocket protocol, state reconstruction, UI reducer, controls, media interruption, and task/action presentation.
**Number note:** ADR-0013 is already assigned to Memory Boundary + Session Model in the existing design/implementation worktrees and declares 0014 as the next free number.

---

## 1. Context

ADR-0006 and ADR-0008 define the backend truth needed for a low-latency Jarvis:

- input, response generation, action execution, and playback have independent lifetimes;
- speech and document output may be sibling ResponseRuns;
- semantic response segments can reach a surface before full generation completes;
- playback interruption is generation-safe and does not imply task cancellation;
- long ActionRuns continue while new input is accepted;
- response, action, panel delivery, and heard-state are not the same fact.

The current Inherent client cannot represent those contracts.

The current Python-to-client transport is:

```text
surface.response_* Event Log rows
→ one polling cursor
→ InherentBroadcaster
→ {"op":"open|append|done|voice","payload":{...}}
→ outbound-only /inherent/ws
→ BridgeTurnGate(turnOpen: Bool)
→ NativeCardModel(one phase, one answer)
```

The concrete gaps are:

1. The main `jarvis` repository contains no Swift/Xcode source. The newest compatible client is an independently versioned sibling copy, so Python and Swift wire changes cannot currently be reviewed, committed, or tested atomically.
2. `BridgeTurnGate` has one Boolean and ignores `turn_id` after open. It has no response, group, generation, sequence, event, or cursor identity.
3. `NativeCardModel` combines state storage, state transition, rendering timers, HTTP effects, PTT, and history around one global response.
4. The receive chain uses several asynchronous hops before state mutation. Server send order is not a formal Swift apply-order guarantee.
5. Reconnect forces a destructive reset, while the server neither replays nor sends a projection snapshot.
6. A fixed 30-second “no done” watchdog deletes a valid 30–600 second waiting-action turn.
7. Swift disables PTT while a response is submitting/streaming. Even if that guard is removed, the server does not learn of the PTT until key-up, complete WAV upload, and ASR.
8. The current client renders tool/progress/confirmation-looking markup parsed from answer text. Those blocks are not projection truth, and confirmation pills are not interactive controls.
9. The Python broadcaster awaits each client sequentially. One slow panel can delay every later client and every later response event.
10. The forwarding CLI also consumes the v1 WebSocket, so silently changing the old message set would break a non-Swift client.

This ADR supplies the missing Layer-5 contract. It is not a visual redesign detached from the runtime. It defines the wire identities, snapshot/live handoff, Swift state and reducer, control semantics, compatibility boundary, file changes, tests, and latency budgets required before ADR-0006/0008 can become a good product experience.

## 2. Goals and non-goals

### Goals

1. Make the first safe semantic segment visible without adding client-side drip delay.
2. Represent concurrent input, speech, document output, background actions, playback, and confirmation without impossible single-enum states.
3. Reconstruct panel state after reconnect, daemon restart, sleep/wake, or window hiding without replaying old speech.
4. Keep one slow or broken client from delaying voice, controls, or another client.
5. Give every streamed response and action exact identity, ordering, deduplication, and terminal semantics.
6. Make “停止说话” and “取消任务” visibly and mechanically different.
7. Let explicit PTT interrupt the active speech generation as soon as the hold is recognized, before ASR completes.
8. Make confirmation buttons typed, accessible, idempotent, and backed by `PendingConfirmations` truth.
9. Keep Inherent inside Layer 5: it renders projections and submits `UserResponse`; it never owns task truth, decides risk, or calls tools directly.
10. Preserve v1 CLI/client behavior until v2 has passed deterministic tests and live burns.
11. Establish measurable local-overhead budgets so UI/protocol work does not hide cloud-model or tool latency.

### Non-goals

- Integrating OpenAI Realtime API or any hosted speech-to-speech session.
- Moving TTS playback into Swift. Python remains the only Jarvis response-audio owner.
- Persisting raw PCM, VAD probabilities, partial ASR tokens, waveform samples, or animation state.
- Treating transport acknowledgement as proof that Allen saw or heard content.
- Letting Swift infer action state, confirmation state, risk, or evidence quality from answer text.
- Making every historical response permanently resident in the floating card.
- Guaranteeing exactly-once network delivery. The protocol is at-least-once plus identity, ordering, idempotency, and snapshot repair.
- Replacing the old HTTP whole-WAV ASR path in the first v2 milestone.
- Automatically cancelling a real action when a response or speech generation is stopped.

## 3. Non-negotiable invariants

1. **Projection is truth.** Swift may own layout, selection, scroll position, window visibility, draft input, and animation. Response/action/confirmation truth comes from server projections and deltas.
2. **Identity precedes streaming.** No v2 response segment exists without
   `response_id`, `response_group_id`, and `sequence`. Speech/playback state
   additionally names the L5-minted `playback_generation_id`.
3. **One durable order.** All durable panel deltas are emitted by one Event-Log-ordered view sequencer. A direct bus and a poller are never competing payload producers.
4. **Snapshot is atomic.** Swift never renders a half-adopted snapshot. It stages all pages, verifies the end marker, swaps once, then applies later deltas.
5. **Durable data is never silently dropped.** A client queue overflow produces resync/close; only explicitly replaceable ephemeral updates may be coalesced.
6. **Stop speech is not cancel action.** A response-generation/playback target and an ActionRun target are different types and different controls.
7. **Controls are requests, not authority.** Swift submits a typed `UserResponse`. Runtime/L3 re-resolves current truth and policy before L4 execution.
8. **ACK is transport only.** `transport.ack` means a client reducer applied data. It is not “panel seen,” “heard,” confirmation accepted, or task completed.
9. **Historical speech never replays.** Snapshot/reconnect may restore document text and durable playback terminal/checkpoint state, but it never emits a speak effect.
10. **One Swift mailbox.** Every decoded server message reaches one MainActor store in socket order. No controller/model layer creates an additional unstructured ordering hop.
11. **Visibility is presentation only.** Hiding or fading the panel does not discard active response, action, or confirmation state.
12. **Liveness is transport-scoped.** Ping/pong and socket epochs detect a dead connection. A response is never reset merely because it ran longer than 30 seconds.
13. **Unknown is fail-safe.** Unknown optional fields are ignored; an unknown durable discriminator within the negotiated view schema is a protocol error, not a resync loop or partial application.
14. **No fake progress.** “正在查找/执行/整理结果” is rendered only from a real response or action lifecycle state, never from a timer or invented filler.
15. **One relevant row, one ACK unit.** Each Inherent-relevant Event Log row
    produces exactly one durable v2 envelope. An irrelevant row only advances
    scan cursor. If a relevant row changes several view entities, its envelope
    contains one atomically applied ordered mutation batch.
16. **Input is authenticated too.** Every v2 text, image, voice upload, confirmation, cancellation, and dismissal request uses the same authenticated client identity and idempotency rules.

## 4. Architectural decision

### D1. Vendor the canonical Swift client into this repository before protocol work

Implementation Step 0 copies the newer compatible client from:

```text
/Users/alllllenshi/Projects/jarvis-codex/desktop/inherent-swift
```

into:

```text
desktop/inherent-swift
```

The audited source repository baseline is:

```text
repository commit:
  65c39c79c35d30e75b777284696cb824ca3845fa

Swift subtree:
  66e248c88873313f28760ee15820048817b0d6f8

last commit that changed the Swift subtree:
  5fa923c
```

The import commit adds `desktop/inherent-swift/UPSTREAM_BASELINE.md` containing:

- source repository path and commit;
- import date;
- a SHA-256 manifest for every imported source/test/resource file;
- confirmation that the source directory was clean;
- the pre-change Xcode test result.

The baseline procedure runs `xcodegen generate` first because the generated
`.xcodeproj` is intentionally absent/ignored. It records macOS, Xcode,
XcodeGen, Swift, and test-command versions. The test is run from a clean
archive/copy so an old local build cannot satisfy the gate.

Before import, Step 0 also inventories user-visible behavior in all three
known copies. The currently dirty `jarvis-legacy` Swift tree contains local UI
work, including resizable-card behavior. Each behavior is explicitly migrated
or explicitly rejected with Allen's decision; it is never silently lost by
choosing the cleaner `jarvis-codex` source. The unused
`OutputSpeechPlayer.swift` is excluded.

The source is vendored rather than kept as a submodule or implicit sibling dependency because:

- Python and Swift wire changes must be atomic;
- repository CI must run both ends from one commit;
- reviewers must see the complete blast radius;
- rollback must restore a known pair of protocol implementations;
- the app already uses XcodeGen directory source discovery, so new files do not require hand-editing a generated Xcode project.

`jarvis-legacy` remains historical evidence only. It is not copied as the canonical client, and its unused Swift speech player remains disconnected.

### D2. Keep the six layers; Swift is the physical Layer-5 client

| Owner | Responsibility | Forbidden |
|---|---|---|
| L0/shared | session/turn/response/action identifiers already shared by ADR-0006/0008 | Swift UI concepts, transport state |
| L2/state | Event Log cursor/epoch, ResponseLedger, Action view projection, PendingConfirmations, durable idempotency receipt | rendering, localization, tool execution |
| L3/decision | ResponseRun truth, emission permits, attention/presentation intent, response interrupt policy, action-cancel gate, confirmation decision | socket handling, AppKit |
| L4/execution | ActionRun execution/cancel capability/quiescence/terminal | UI state, direct client frames |
| L5/surface Python | wire DTOs, safe view presentation, snapshot/delta encoding, per-client queues, media control façade | deciding risk, inventing action truth |
| L5/Inherent Swift | deterministic client projection, interaction capture, rendering, local presentation state | mutating truth, invoking tools, declaring completion |
| runtime | wires sibling layers, owns view sequencer and control routing | becoming a seventh domain layer |

The required control path remains:

```text
Swift interaction
→ typed UserResponse
→ authenticated L5 server endpoint
→ runtime routing
→ L3 resolution/policy/gate
→ L4 action only when authorized
→ durable events
→ L2 projection
→ L5 delta
→ Swift re-render
```

The one exception is time-critical physical response interruption already
authorized by ADR-0006: runtime may ask the L5 playback actor to perform
generation-CAS immediately. L3 records a response terminal only when the
issued policy requests `scope=generation`; `foreground_output` produces only
playback truth. This exception cannot name or cancel an ActionRun.

### D3. Model realtime UI state as orthogonal sub-states and separate identities

Swift replaces `NativeTurnPhase` as the protocol truth with:

```swift
struct InherentUXState: Equatable {
    var connection: ConnectionState
    var synchronization: SynchronizationState
    var input: InputPresentationState
    var responseGroups: [ResponseGroupID: ResponseGroupState]
    var responseOrder: [ResponseGroupID]
    var actions: [ActionID: ActionViewState]
    var pendingConfirmation: ConfirmationViewState?
    var capabilities: InherentCapabilities
    var foregroundGroupID: ResponseGroupID?
    var presentation: LocalPresentationState
}
```

The key state families are:

```text
connection:
  disconnected | connecting | synchronizing | live | reconnecting | offline

input:
  idle | listening | transcribing | committed | empty | failed

ResponseRun lifecycle (keyed only by response_id):
  generating | waiting_action | finalizing |
  completed | cancelled | failed

response phase:
  commentary | final

response channel:
  speech | document | both

panel stream (keyed by response_id):
  unopened | open | closed | failed

playback lease (keyed by response_id + playback_generation_id):
  idle | buffering | speaking | ducked | draining |
  completed | interrupted | failed

canonical action:
  proposed | authorized | dispatched | running |
  result_observed | failed | timeout_assumed | cancelled

local action command overlay:
  none | cancel_submitting | cancel_requested | cancel_rejected
```

These states intentionally coexist. Examples:

- `listening + playback.ducked + action.running`;
- a cancelled speech response beside a still-generating document response;
- a completed ResponseRun whose playback was interrupted;
- an old background ActionRun while a new foreground turn is listening;
- a failed TTS response beside a completed document sibling.

The exact identity shape is:

```swift
struct ResponseState: Equatable {
    let responseID: ResponseID
    let responseGroupID: ResponseGroupID
    var lifecycle: ResponseLifecycle
    var lifecycleRevision: EventCursor
    var panelStream: PanelStreamState
    var playbacks: [PlaybackGenerationID: PlaybackState]
}
```

Rules:

- `response_id` is never reused. A correction/retry that needs a new semantic
  lifecycle creates another ResponseRun, normally in the same response group.
- L3 owns exactly one lifecycle terminal per `response_id`.
- The panel stream is also keyed by `response_id` and orders semantic segments
  with `sequence`. It has one delivery terminal per response.
- `playback_generation_id` exists only after the L5 playback actor activates a
  speech lease. It is optional/absent for document-only responses and is the
  only generation accepted by playback CAS.
- The generic wire field `generation_id` is not used by v2. ADR-0006/0008 are
  amended to call their media-owned field `playback_generation_id`.

No single “THINKING/STREAMING/DONE” label is allowed to overwrite those facts.

### D4. Use a pure reducer and isolate every effect

The state transition seam is:

```swift
struct InherentReducer {
    static func reduce(
        state: inout InherentUXState,
        event: InherentClientEvent,
        now: ContinuousClock.Instant
    ) -> [InherentEffect]
}
```

The reducer:

- has no URLSession, AppKit, audio, filesystem, wall-clock, or animation calls;
- receives an injected monotonic time;
- validates socket epoch, log epoch, boot, cursor, entity revision, response identity, panel sequence, and playback generation;
- returns effects such as `sendAck`, `requestResync`, `announceAccessibilityMilestone`, or `scheduleLocalFade`;
- never emits a tool/action effect.

`RealtimeTransport` is an actor that owns exactly one receive task. It assigns
a monotonically increasing `receiveIndex` and places frames, close, failure,
and reconnect events into one `AsyncStream` mailbox. It awaits completion of
the MainActor apply before pulling the next mailbox item; actor reentrancy may
not create a second reader. DTOs, state, and effects are `Sendable`. Effect
results return as new reducer events and never mutate state directly.

`RealtimeStore` is the sole `@MainActor ObservableObject` and the sole state
mutation entry. `RealtimeEffectRunner` performs network and presentation
effects outside the reducer.

The current multi-hop `DispatchQueue.main.async → Task @MainActor → model Task` chain is removed from v2. Legacy v1 may stay behind its adapter until retired.

## 5. Wire protocol v2

### D5. Add a separate authenticated bidirectional endpoint

V2 uses:

```text
WS /inherent/ws/v2
```

The existing `/inherent/ws` remains byte-compatible v1 for the current CLI and rollback client.

V2 is authenticated because it accepts user input, confirmation, and
cancellation requests:

1. At boot the daemon creates a random 256-bit token.
2. It atomically writes it to `<runtime_root>/inherent-v2.token` with mode `0600`.
3. The Swift launcher resolves the same runtime root and loads the token.
4. The WebSocket upgrade sends `Authorization: Bearer <token>`.
5. The token is never placed in a JSON frame, URL, log, crash report, or analytics record.
6. A daemon restart rotates the token; a rejected reconnect reloads the token file once before normal backoff.

The token path is owned by `RuntimePaths` in L6. The launcher passes only the
runtime-root/token-file path to Swift, never the token value in an environment
variable. Creation rejects a symlink, non-regular file, wrong owner, unsafe
parent permissions, or a mode broader than `0600`.

The server remains bound to loopback. Loopback alone is not treated as
authorization. Client frames are limited to 64 KiB, decoded with strict
schemas, rate-limited per connection, and rejected before routing when
unauthenticated.

Every v2-originated mutating HTTP request uses the same Bearer token and
`client_instance_id`. This includes text submit, image staging/upload, and
ASR upload. The token protects against another OS user and browser/DNS-rebinding
style requests; it does not protect against an already compromised process
running as the same macOS user.

### D6. Common envelope and identity

Client and server envelopes are separate fixed schemas:

```text
ClientEnvelope<T>
  protocol_version
  message_type
  message_id
  client_instance_id
  connection_id?       # null only for client.hello
  sent_at_ms
  payload: T

ServerEnvelope<T>
  protocol_version
  message_type
  message_id
  delivery_class
  connection_id
  log_epoch
  boot_id
  event_cursor?
  ephemeral_sequence?
  sent_at_ms
  payload: T
```

For a client command, `message_id == request_id`. Clients do not submit a
trusted payload hash. The server computes `payload_hash` from the decoded,
validated DTO using its canonical JSON encoder and covers protocol version,
command discriminator, exact target/revision, decision/scope, and normalized
payload. Golden fixtures define the bytes.

Every durable v2 server delta uses:

```json
{
  "protocol_version": 2,
  "message_type": "view.delta",
  "message_id": "source-event-uid",
  "delivery_class": "durable",
  "connection_id": "C...",
  "log_epoch": "L...",
  "boot_id": "B...",
  "event_cursor": 1842,
  "ephemeral_sequence": null,
  "sent_at_ms": 1788200000000,
  "payload": {
    "source_event_uid": "source-event-uid",
    "changes": []
  }
}
```

Rules:

- `log_epoch` is the immutable identity of one append-only Event Log lineage. A byte-copy preserves that lineage; a clone intended to diverge or any replacement that may change physical row identity must explicitly rotate the epoch before serving v2.
- `boot_id` identifies one daemon process boot and all ephemeral media/input state.
- `connection_id` identifies one accepted WebSocket.
- Durable deltas carry `event_cursor = events.id` and no `ephemeral_sequence`.
- Ephemeral updates carry a connection-scoped monotonically increasing `ephemeral_sequence` and no Event Log cursor.
- Protocol/snapshot/control frames may carry neither.
- Every Inherent-relevant Event Log row maps to exactly one durable
  `view.delta` envelope; an irrelevant row emits no envelope and only advances
  the sequencer scan cursor.
- Its `message_id` is the source `event_uid`.
- If one row changes several view entities, `payload.changes` contains an
  ordered array of typed mutations. Swift validates and applies the entire
  array atomically before ACK.
- Deterministic message-ID suffixes are allowed only for snapshot, protocol,
  and ephemeral frames; never for independently ACKed durable deltas.
- Cursor gaps are legal because not every Event Log row is visible to Inherent. Cursor regression is not legal.
- `event_uid` provides durable deduplication; the physical cursor provides ordering/high-water semantics.
- `boot_id` never substitutes for `log_epoch`.

The Event Log gains a one-row operational metadata table holding `log_epoch`.
It identifies one append-only Event Log lineage:

- an existing database receives its epoch exactly once under
  `BEGIN IMMEDIATE` during migration;
- normal restart, VACUUM, and backup/restore that preserves row identity keep
  the epoch;
- a byte-copy preserves the epoch by definition;
- any operation that truncates, renumbers, merges, or replaces `events.id`
  must explicitly rotate the epoch before serving v2;
- a clone intended as a new lineage runs that maintenance operation;
- within one epoch, physical IDs never regress, repeat, or renumber.

This metadata table is an explicit operational exception to the Event Log
module's current “single table” comment. It is not an event or projection truth.

Unknown optional fields are ignored. An unknown `delivery_class=ephemeral`
message may be ignored and metered. The selected `view_schema_version` fixes
the complete durable mutation catalog. An unknown durable mutation within
that schema is a server protocol violation: Swift freezes durable apply/ACK
and closes the socket with `protocol_error`. If the server requires a newer
schema, hello fails with `upgrade_required` instead of entering an infinite
resync loop.

### D7. Handshake

The first client frame is:

```json
{
  "protocol_version": 2,
  "message_type": "client.hello",
  "message_id": "R...",
  "client_instance_id": "I...",
  "connection_id": null,
  "sent_at_ms": 1788200000000,
  "payload": {
    "supported_versions": [2],
    "client_build": "git-sha-or-build-number",
    "view_schema_versions": [1],
    "capabilities": [
      "paged_snapshot",
      "transport_ack",
      "response_control",
      "confirmation_control"
    ],
    "last_log_epoch": "L...or-null",
    "last_applied_cursor": 1830,
    "has_complete_local_state": true
  }
}
```

The server answers:

```json
{
  "protocol_version": 2,
  "message_type": "server.hello",
  "message_id": "R...",
  "delivery_class": "protocol",
  "connection_id": "C...",
  "log_epoch": "L...",
  "boot_id": "B...",
  "sent_at_ms": 1788200000001,
  "payload": {
    "selected_version": 2,
    "view_schema_version": 1,
    "resume_mode": "snapshot",
    "server_high_water_cursor": 1840,
    "required_client_capabilities": [
      "paged_snapshot",
      "transport_ack"
    ],
    "runtime_capabilities": {
      "text_input": true,
      "image_input": false,
      "voice_input": true,
      "response_interrupt": true,
      "action_cancel": true,
      "confirmation_actions": true,
      "natural_barge_in": false,
      "aec_profile": "headphones_only"
    }
  }
}
```

Runtime capabilities are computed from live routes/providers. For example,
`image_input` remains false while the image endpoint is a 501 stub.

V2.0 always chooses `resume_mode=snapshot` on a new socket. This is an intentional correctness choice: the current client has no durable local projection cache, and current projections are small enough for a local snapshot. The hello fields reserve a future incremental-resume mode, but implementation may enable it only after a client proves it retained a complete state with the same log epoch and view schema.

A WebSocket that does not send a valid hello within two seconds is closed. V1 clients never connect to this endpoint.

### D8. Snapshot and live handoff

A snapshot is a staged, paged bundle:

```text
snapshot.begin(snapshot_id, through_cursor, view_schema_version, section_order, counts)
snapshot.page(snapshot_id, section, page_index, items)
snapshot.page(...)
snapshot.end(snapshot_id, through_cursor, content_hash)
then durable deltas with cursor > through_cursor
```

Sections are:

- `response_groups` — active groups plus a bounded recent history;
- `actions` — open/recent actions and cleanup/quarantine state;
- `pending_confirmation` — zero or one live slot;
- `capabilities` and projection freshness;
- `surface_notices` that require user action.

Each page is at most 64 KiB. Large document bodies are paged by response/segment and may use an artifact reference when they exceed the configured inline snapshot budget.

The server handoff state transitions are serialized by the single
`InherentViewSequencer` actor. Socket sends and the wait for client ACK never
run inside that actor or block another client's sequencing:

1. Authenticate and post a short mailbox command. In normal sequencer order,
   it drains the canonical L2 projector through captured high-water H,
   registers the client as `snapshotting`, clones one bounded immutable
   `projection_checkpoint_at_H`, and returns that checkpoint plus a unique
   snapshot-attempt token. The clone excludes unbounded artifact bodies.
2. A per-client task outside the sequencer gives that immutable checkpoint to
   the L5 presenter, which builds and encodes
   `SnapshotPlan(H, checkpoint, pages)`. It never re-folds the full Event Log
   on the live path. Raw full-log projection rebuild is a startup/maintenance
   fallback completed before v2 clients are accepted.
3. The task posts one short mailbox command that verifies the attempt token
   and records the bounded plan/H/hash as awaiting ACK. Its sender then
   transmits begin/pages/end and waits for ACK outside the sequencer. The task
   retains the immutable projector checkpoint. Meanwhile the sequencer keeps
   draining global truth and serving other clients; it skips live fan-out to
   this snapshotting client and stores no duplicate delta payload buffer.
4. Do not send that client any durable `event_cursor > H` before snapshot
   ACK. Swift atomically adopts through H and ACKs the exact snapshot ID/H.
5. The verified ACK posts a new sequencer mailbox command. That command
   captures `B = MAX(events.id)`, clones the client's immutable projector at
   H, and folds every row `H < id <= B` in ascending order. Relevant rows
   produce the ordered catch-up; irrelevant rows still advance the private
   replay cursor.
6. Still in that one actor command, enqueue the bounded catch-up and atomically
   set `client.live_frontier = B`, then discard the checkpoint. If replay
   exceeds its frame/byte budget, close/resnapshot that client; never expose a
   partial catch-up.
7. Events committed after B append behind the catch-up through the same actor
   and per-client queue. No sender/notification path may overtake it.

Swift builds a separate `SnapshotStagingState`. `snapshot.end` must match:

- the active snapshot ID;
- expected page counts;
- the advertised view schema;
- the SHA-256 hash over the exact UTF-8 bytes of every complete
  `snapshot.page` server frame, ordered first by the section order advertised
  in `snapshot.begin` and then by `page_index`, excluding begin/end and
  WebSocket framing. The server hashes the byte slices it sends; Swift hashes
  the raw bytes before JSON decoding;
- the same `through_cursor` as begin.

Only then does the MainActor reducer atomically replace projection-backed state. Local draft, window position, expansion, selection, scroll anchors, and accessibility preferences survive the swap. Old input/playback ephemeral state is cleared on boot change; snapshot adoption never creates a speech-play effect.

If snapshot staging fails, the old visible state remains with a reconnect/resync banner.

### D9. One ordered server sequencer

ADR-0008's direct committed bus remains the low-latency wake source, but this ADR refines the panel consumer:

- a commit notification wakes `InherentViewSequencer`;
- the sequencer drains relevant Event Log rows after its cursor in `events.id ASC` order;
- it updates the in-memory view projector and produces typed deltas;
- the periodic SQLite watcher is only a wake/recovery mechanism;
- neither the direct bus payload nor the watcher directly fan out an independent client message.

This costs a local indexed SQLite drain after a semantic commit, but removes the unsound open/chunk/terminal cross-source race. TTS may continue to consume ADR-0008's exact committed event hot path under its active-generation and boot-high-water rules; panel sequencing and speech replay remain separate consumer contracts.

The wake is level-triggered and cannot be lost:

1. An after-commit notification sets one `asyncio.Event`.
2. The drain loop clears the flag, captures
   `H = MAX(events.id)`, and reads every row
   `scan_cursor < id <= H` in ascending order.
3. L2 fold logic updates projection truth; L5 maps a relevant transition to
   one `view.delta`. The scan cursor advances across irrelevant rows too.
4. `scan_cursor = H` advances only after all rows projected successfully.
5. The loop repeats immediately if the wake flag is set again or a fresh MAX
   exceeds the cursor.
6. A periodic timer sets the same flag as recovery; it is never another
   payload producer.

L2 owns all fold logic and immutable projection snapshots. Runtime owns only
sequencer lifecycle, scan watermark, wake/drain, and client orchestration. L5
maps already-derived truth to safe wire DTOs; it never derives canonical
action state, confirmation validity, or cancellability.

The sequencer is the only producer of durable v2 panel deltas. A view delta
carries the source row cursor/event UID and exactly one ordered mutation
batch. Startup drains through the current high-water before accepting v2
clients.

### D10. Message catalog

#### Durable mutations inside one `view.delta`

| Mutation | Source truth | Essential payload |
|---|---|---|
| `response.opened` | `response.started` + presentation projection | group/response/turn/session/source-request IDs, phase, channel, lifecycle, question/summary, created time |
| `response.segment` | permitted `surface.response_chunk` | response ID, sequence, phase, channel, text, segment hash |
| `response.delivery` | `surface.response_open/emitted/failed` | response ID, panel delivery state, reason |
| `response.lifecycle` | `response.started/completed/cancelled/failed` | response ID, lifecycle, terminal reason, entity revision |
| `playback.state` | durable playback checkpoint/terminal | response ID, playback generation ID, phase, conservative heard summary/checkpoint metadata |
| `action.upsert` | Action view projection after an action event | action ID, group/task refs, canonical state, safe label/target, revision, cancellable hint, cleanup/freshness |
| `confirmation.upsert` | PendingConfirmations live slot | confirmation ID, group/action refs, safe summary/target/risk, options, expiry, revision |
| `confirmation.cleared` | accepted/rejected/expired terminal, or a new requested row replacing the old slot | confirmation ID, clear reason including superseded, revision |
| `input.committed` | `utterance.received` | utterance/turn/session IDs, source, normalized text when presentation policy permits |
| `surface.notice` | projection-backed actionable failure/fallback | subject ref, severity, safe message code/text, required action |

#### Ephemeral server updates

| Message | Key/coalescing rule |
|---|---|
| `input.state` | latest by `utterance_id + revision` |
| `input.partial` | latest by `utterance_id + revision`; never authoritative transcript |
| `playback.progress` | latest by response/playback generation; includes buffering/playing/draining/silenced |
| `action.progress_hint` | latest by action/stage; must point to a real ActionRun and never invent completion |
| `connection.notice` | latest by notice code |
| `ephemeral.clear` | remove one exact evicted/ended ephemeral key; never leave stale client state |
| `ephemeral.baseline` | one atomic latest-value set plus a global watermark before live ephemeral delivery |

The generation-keyed `playback.progress` payload includes
`response_id, playback_generation_id, state, heard_through_sequence?,
silence_at_monotonic_ns?, cursor_quality?`. Only the L5 playback actor emits
`state=silenced`, after its conservative DAC/audible horizon crosses the kill
boundary. The earlier durable interrupted terminal records canonical
playback outcome but is not itself proof that device-buffer tail is inaudible.

The baseline payload is:

```text
EphemeralBaseline
  watermark_sequence
  items[]:
    key
    item_message_type
    item_sequence       # <= watermark_sequence
    typed_payload
```

Swift applies all baseline items atomically and sets
`last_ephemeral_sequence=watermark_sequence`. It replaces the complete
ephemeral store, so keys absent from the baseline are cleared, and it does
not replay items as individual announcements/effects.

#### Client controls

| Message | Meaning |
|---|---|
| `transport.ack` | reducer applied through cursor/snapshot |
| `state.resync_request` | client detected gap/schema/state conflict |
| `control.stop_speaking` | stop the current foreground speech presentation |
| `input.ptt_begin` | explicit hold recognized; may CAS-interrupt foreground speech |
| `input.ptt_cancel` | abandon one unclaimed capture token/capture lifecycle |
| `control.cancel_action` | request cancellation of one exact ActionRun |
| `control.confirmation_decision` | accept/reject the exact pending confirmation |
| `control.dismiss_item` | semantic dismissal of one presentation item |

Every client command uses the fixed `ClientEnvelope`. Semantic controls carry
the expected target ID/revision in their typed payload. The server computes
the payload hash. Reusing one idempotency key with a different computed hash
is rejected.

#### Control acknowledgement

The complete control-ACK envelope is:

```json
{
  "protocol_version": 2,
  "message_type": "control.ack",
  "message_id": "ACK-R...",
  "delivery_class": "control_ack",
  "connection_id": "C...",
  "log_epoch": "L...",
  "boot_id": "B...",
  "event_cursor": null,
  "ephemeral_sequence": null,
  "sent_at_ms": 1788200000100,
  "payload": {
    "request_id": "R...",
    "command": "control.cancel_action",
    "receipt_status": "queued",
    "reason_code": null,
    "canonical_event_uid": "optional",
    "current_entity_revision": 1847,
    "result": null
  }
}
```

Allowed receipt statuses are:

```text
received | applied | queued | already_applied |
rejected | stale | unsupported | failed
```

`result` is a strict command-discriminated union, not an untyped dictionary:

```text
NoControlResult

PTTStartedResult
  result_type: ptt_started
  capture_id
  capture_token
  expires_at_ms
  boot_id
  interrupt_result: applied | stale | not_active | rejected

StopSpeakingResult
  result_type: stop_speaking
  interrupt_result: applied | already_applied | stale | rejected
  playback_generation_id?
```

`input.ptt_begin` with an applied/already-applied receipt must carry
`PTTStartedResult`; otherwise Swift treats the ACK as protocol-invalid and
does not upload audio. An identical boot-scoped begin retry returns the exact
same capture token and result until claim/cancel/expiry. Capture tokens are
sensitive frame data and are excluded from logs, crash reports, and trace
attributes. Other controls use their exact result variant or
`NoControlResult`. Python and Swift golden fixtures cover every legal
command/status/result combination.

For action cancel, `queued` means the authorized cancel request exists; it does not mean the target ActionRun is cancelled. Only a later `action.upsert(state="cancelled")` changes canonical action state.

### D11. Per-client flow control and ACK

Built: `jarvis/runtime/inherent_hub.py` implements rules 1-5 and 9-12, and
`realtime.inherent.v2_sequencer.flow_control` holds their limits.

Each v2 client owns:

- one control-ack queue, default 32 frames;
- one durable/snapshot queue, default 256 frames and 1 MiB total;
- one coalescing map for at most 32 ephemeral keys;
- one sender task;
- an outstanding cursor/byte window;
- an ACK timer.

Sender priority is control ACK, durable/snapshot, then ephemeral. Snapshot pages are capped so a large document cannot monopolize a single frame.

Rules:

1. Durable response segments, terminals, action states, and confirmation states are never dropped or reordered.
2. Ephemeral partial transcript, playback progress, and progress-hint messages may replace an older update with the same key.
   Audible-tail state is protected separately as described in D12 and is not
   evicted by this generic 32-key map.
3. If a durable enqueue would exceed either limit, the server best-effort sends `server.resync_required(reason="client_backpressure")` on the control lane and closes that client with a retryable code.
4. No slow-client condition blocks the sequencer, TTS, playback interrupt, or another client.
5. The server tracks `last_sent_cursor`, `last_acked_cursor`, unacked durable
   frame count, and unacked encoded bytes.
6. Swift sends a cumulative ACK only after MainActor application:

```json
{
  "protocol_version": 2,
  "message_type": "transport.ack",
  "message_id": "R-ack...",
  "client_instance_id": "I...",
  "connection_id": "C...",
  "sent_at_ms": 1788200000200,
  "payload": {
    "snapshot_id": "optional",
    "through_cursor": 1847
  }
}
```

7. An ACK is valid only for the same connection/log epoch, with
   `last_acked_cursor <= through_cursor <= last_sent_cursor`. A regressing ACK
   is ignored only as an exact duplicate; an ACK past last sent is a protocol
   error. A snapshot ACK must match the active snapshot ID and H.
8. ACK is batched at 25 durable messages or 100 ms, whichever comes first.
9. Snapshot pages have their own byte budget and five-second adoption
   deadline. No ACK progress for five seconds while a durable/snapshot window
   is non-empty closes that client with resync-required; “mark slow” alone is
   not a terminal policy.
10. Control-lane overflow closes only that client. To prevent a control flood
    starving durable truth, the sender serves at most eight control frames
    consecutively before one ready durable frame.
11. ACK and control-receipt metadata are operational and are not appended as
    “seen” or “heard” truth.
12. A 33rd distinct generic ephemeral key never silently evicts client-visible
    state. The hub enqueues an increasing-sequence `ephemeral.clear(old_key)`
    before replacing that cache slot. If a slow client's bounded lane cannot
    preserve clear-before-new ordering, that client is closed/resynced.

### D12. Connection liveness and restart

The transport actor owns a monotonically increasing local `socketEpoch`. Every receive, failure, close, retry, and decoded message is tagged with it. A callback from an old epoch cannot mutate current state or schedule another reconnect.

The v2 local-daemon reconnect schedule starts at
`0.25/0.5/1/2/4/8` seconds and caps at 8 seconds with jitter. The old
1/2/4/8/16 schedule remains the v1 baseline. V2 backoff resets only after:

- a successful authenticated hello;
- a valid snapshot adoption or live durable frame;
- not merely any parseable unknown message.

WebSocket ping/pong checks transport liveness. Defaults:

- idle ping interval: 2 seconds;
- pong deadline: 1 second;
- hello deadline: 2 seconds.

On disconnect:

- keep the last valid document/action/confirmation state;
- overlay “正在重新连接”;
- disable server-mutating controls;
- keep local draft and navigation;
- never mark a response/action complete or failed by inference.

On a new `boot_id`:

- clear old listening/transcribing/speaking/ducked ephemeral state;
- clear pending local command overlays whose result is unknown;
- retain the old visible projection until the new snapshot atomically replaces it;
- never replay historical speech;
- show restart-reconciled response/action terminals from the snapshot.

There is no response-duration watchdog. Lifecycle-specific deadlines live with their owning backend state machines and appear as canonical failed/timeout states.

Resync is a terminal state for one socket epoch:

1. On a sequence/hash/cursor/state conflict, Swift enters
   `resyncRequired`, freezes durable application and ACK at the last valid
   cursor, and sends at most one `state.resync_request`.
2. Swift then closes the socket; later frames on that epoch are ignored.
3. A new socket epoch performs authentication and a complete snapshot.
4. The server never attempts an in-place partial snapshot on the conflicted
   socket.

Ephemeral ordering is independent:

- Swift tracks `last_ephemeral_sequence` per connection/boot.
- A lower/equal sequence is ignored; gaps are legal because values coalesce.
- Ephemeral frames received before snapshot adoption are discarded.
- One hub actor assigns the global sequence, replaces per-key cache entries,
  constructs/dequeues baselines, and orders each client's ephemeral lane.
- After catch-up reaches live, that actor captures watermark W and the latest
  typed generic item per key. It also asks the L5 playback actor for
  `pending_tail|silenced` state for every playback generation present in the
  adopted snapshot, merges/sorts those typed items by their own sequence,
  enqueues one atomic `ephemeral.baseline(W, items)`, and sets the client's
  ephemeral frontier to W in the same actor operation.
- The playback actor retains current-boot audible-tail state for every
  generation whose response remains inside D16 snapshot retention, independent
  of the generic 32-key cache. A reconnect after the horizon therefore gets
  `silenced`; if the horizon is still pending it gets `pending_tail` and the
  later transition. On a new boot the old device stream is gone and this
  current-boot registry is rebuilt empty.
- Ordinary updates with sequence `> W` append behind the baseline and are
  sent in increasing global-sequence order across keys. Thus an A5/B6/A7
  replacement cannot send A7 before B6 and make Swift discard B6.
- A boot change clears the cache/state before the new baseline.

## 6. Projection and presenter contracts

### D13. Add a panel-ready ActionViewProjection

Today's `StatusBoard.open_actions` only records “dispatched and not terminal.” It is insufficient for a work cockpit.

L2 gains a bounded `ActionViewProjection` folded from registered action, quiescence, cleanup, task, and safe progress events. One `ActionView` contains:

```text
action_id
task_id?
response_group_id?
action_type
canonical_state
state_revision_cursor
started_at_ms?
updated_at_ms
safe_target_ref?
result_available
failure_code?
cleanup_state: none | pending | quiesced | completed | quarantined
cancel_request: CancelRequestView?
freshness: fresh | stale

CancelRequestView
  request_id
  state: received | authorized | quiescing | rejected | failed | resolved
  revision_cursor
  reason_code?
```

It does not contain raw tool arguments, secrets, unbounded stdout, authorization leases, gate internals, or unverified tool self-report.

L3 computes `cancellable_hint` and `user_action_required` from canonical
state and policy. L5 only localizes/serializes labels from those typed
presentation fields; it never derives cancellability. The safe presentation
fields are:

```text
display_label
safe_target_label
cancellable_hint
progress_label
user_action_required
```

`cancellable_hint` only controls whether a button is offered. The server revalidates live state and policy when clicked.

“Cancel requested” is not added to the canonical eight-state ActionLifecycle.
The UI may show an immediate local submitting overlay. After reconnect, the
separate durable `CancelRequestView` is folded from the canonical
UserResponse/gate/action trail, so an accepted-but-not-terminal request does
not disappear. Its transition rules are:

- `received` after the exact canonical UserResponse;
- `authorized` after the gate/outbox pass;
- `quiescing` after `ActionHandle.cancel()` accepts but before target
  quiescence wins;
- `rejected` for stale/policy/unsupported requests that never reach L4;
- `failed` for a cancel handler or dispatch-debt failure with a safe reason;
- `resolved` when the target reaches any canonical terminal, including normal
  completion winning the race; `reason_code` distinguishes cancelled,
  completed-before-cancel, failed, or already-terminal.

Only `action.cancelled` changes the target's canonical ActionLifecycle to
cancelled. Cleanup/quarantine remains an independent field. A second request
while one is received/authorized/quiescing resolves to the existing request;
after resolution, a target already terminal is rejected rather than creating
a permanent new “cancel requested” state.

### D14. Serialize PendingConfirmations, never reconstruct it from prose

The v2 presenter reads the existing `PendingConfirmations` projection. A live confirmation payload includes:

```text
confirmation_id
response_group_id?
action_id?
action_summary
target_label
risk_level
requested_at_ms
expires_at_ms
options: accept | reject
revision_cursor
```

Swift does not receive a lease, hidden tool arguments, or authority to append confirmation events.

The client may disable buttons when its local clock passes `expires_at_ms`, but it can only show “正在同步确认状态.” It cannot declare rejection or expiration without a projection delta.

ADR-0014 replaces lazy-only expiry while a confirmation is live with:

```text
confirmation.expired
  owner: L3
  required: confirmation_id, expired_at_ms
  source_event_id: exact confirmation.requested event
```

`ConfirmationTerminalizer` is the sole accepted/rejected/expired exit.
Inside one `BEGIN IMMEDIATE` transaction it:

1. re-folds/validates the exact confirmation ID and expected revision;
2. verifies that it is still pending and that decision/expiry preconditions
   hold;
3. appends exactly one terminal event;
4. commits and returns `Event | AlreadyTerminal | Stale`.

A runtime expiry timer and boot reconciliation call this same terminalizer.
`PendingConfirmations` folds historical schema-v1 terminals plus
`confirmation.expired`. Thus an idle connected panel receives a durable
`confirmation.cleared(reason="expired")` rather than guessing from its clock.

ADR-0012's no-extra-event supersede rule remains. A new
`confirmation.requested(B)` atomically replaces slot A in the projection; the
one `view.delta` sourced by B's request contains ordered
`confirmation.cleared(A, reason="superseded")` then
`confirmation.upsert(B)`. There is no unregistered
`confirmation.superseded` event. A queued decision for A subsequently fails
the exact terminalizer CAS as stale.

### D15. Keep response generation, panel delivery, and playback separate

For each ResponseRun Swift stores:

- L3 lifecycle;
- content phase and channel;
- permitted semantic segments;
- L5 panel-delivery state;
- zero or more L5 playback leases;
- exact panel sequence and optional playback generation;
- terminal reason;
- conservative heard metadata when present.

Required consequences:

- `response.completed + playback.interrupted` displays “回答已生成，语音已停止”;
- `response.cancelled` does not automatically cancel its document sibling;
- `playback.failed + document.closed` displays successful text with voice unavailable;
- `surface.response_emitted` closes document production but does not stand in for `response.completed`;
- a response terminal with a missing segment gap is staged but not presented as a complete document until resync repairs it.

The identity/terminal matrix is:

| Truth | Key | Exactly-one rule |
|---|---|---|
| L3 ResponseRun lifecycle | `response_id` | one completed/cancelled/failed terminal |
| panel semantic stream | `response_id` | one emitted/failed delivery terminal; sequence starts at zero |
| playback | `response_id + playback_generation_id` | one completed/interrupted/failed playback terminal |

`surface.response_failed` is added as the missing L5 delivery terminal:

```text
surface.response_failed
  owner: L5
  required: response_id, response_group_id, turn_id, reason
  optional: session_id, retryable, playback_generation_id
  source_event_id: surface.response_open or upstream response.failed
```

For v2, `surface.response_emitted` and `surface.response_failed` are mutually
exclusive per `response_id` and use one L2 same-transaction delivery
terminalizer. Retrying as a new semantic response creates a new
`response_id`.

### D16. Bound snapshot scope without losing durable truth

The floating cockpit snapshot includes:

- every non-terminal response group;
- every group linked to an open action or pending confirmation;
- the most recent 20 terminal groups by default;
- every open/recent action required by those groups;
- the one pending confirmation;
- freshness and capability metadata.

Older content remains in the Event Log/artifact store and can be loaded through an explicit history/detail request later. It is not copied into every reconnect snapshot.

Large document bodies above the inline budget are represented by a projection-backed artifact reference plus a bounded preview. The inline budget is 16384 bytes of UTF-8 per response document body: a body at or under it is inlined in full; above it the snapshot carries a bounded preview plus the durable reference (`response_id` and the `surface.response_emitted` `event_uid`). Fetching an artifact is a read-only surface operation and must verify the reference; Swift does not invent filesystem paths.

## 7. Swift reducer rules

### D17. Durable ordering and deduplication

The store tracks:

```text
active_socket_epoch
active_connection_id
log_epoch
boot_id
last_applied_cursor
recent_message_ids (bounded)
expected panel sequence per response_id
entity revision per action/confirmation/response
```

Rules:

1. A message from an old socket epoch or connection ID is ignored.
2. A log-epoch change requires snapshot adoption; no old cursor carries across it.
3. A durable cursor lower than the last applied cursor is accepted only as a known duplicate message ID; otherwise request resync.
4. A duplicate cursor/message ID is idempotent.
5. Entity updates below the stored revision are ignored.
6. A panel segment for a terminal/unknown `response_id` is ignored or causes
   resync according to the snapshot baseline; playback updates additionally
   validate `playback_generation_id`.
7. `sequence == expected` appends once and increments expected.
8. `sequence < expected` must have the same stored segment hash; a hash conflict requests resync.
9. `sequence > expected` is buffered up to 32 segments, not rendered, and triggers one resync request. Arrival of the missing segment may drain the buffer in order.
10. Buffer overflow or a terminal arriving across an unresolved gap keeps the last complete prefix and requires snapshot repair.
11. A ResponseRun has at most one L3 lifecycle terminal per `response_id`.
    Its panel stream has at most one delivery terminal. Each playback lease
    has at most one terminal per
    `(response_id, playback_generation_id)`. Duplicate identical terminals are
    no-ops; conflicts request resync.
12. Snapshot replacement resets dedup/sequence baselines to the snapshot's exact content and cursor.

### D18. Foreground and background selection

The reducer, not ad-hoc views, applies these presentation rules:

- a newly committed user turn becomes the foreground group;
- a previous group with a running action remains in the background task shelf;
- the globally unique pending confirmation is pinned in an always-visible
  blocking region even when its owning group is background/collapsed; it
  includes navigation back to that group and does not erase other content;
- new commentary does not overwrite an unresolved confirmation;
- an action result may open a later final response in the original group even when another group is foreground;
- only explicit user selection changes which history/background group is expanded;
- a network reconnect never changes foreground identity by itself.

### D19. Local presentation state stays local

The following never appear as server truth:

- panel position/size;
- selected response group;
- scroll position;
- expanded/collapsed task rows;
- draft text and staged local attachment UI;
- reduce-motion and accessibility preferences;
- temporary hover/focus;
- whether the floating window is hidden.

Closing/hiding the window only changes `presentation.isVisible`. It does not call `discardOpenTurn`, reset a gate, ACK as seen, reject confirmation, or cancel anything.

## 8. Interaction contracts

### D20. Stop speaking

The following is the typed payload inside
`ClientEnvelope<StopSpeaking>`:

```json
{
  "response_id": "RESP...",
  "expected_playback_generation_id": 3,
  "scope": "foreground_output",
  "reason": "button"
}
```

Server behavior:

1. Authenticate and validate the exact active target.
2. Check the L3-issued ResponseInterruptPolicy.
3. Ask the L5 playback actor to CAS-interrupt that generation immediately.
4. Return `applied`, `already_applied`, `stale`, or `rejected`.
5. Let PlaybackTerminalizer append `surface.playback_interrupted`.
6. Do not call ResponseTerminalizer and do not append
   `response.cancelled`.
7. Leave the ResponseRun, linked ActionRuns, and document siblings alive.

`control.stop_speaking` always maps to
`ResponseCancelRequest(scope="foreground_output")`:

| Route | Button behavior |
|---|---|
| document-only | no Stop speaking button |
| speech-only | interrupt playback/TTS output; ResponseRun may continue/finalize |
| speech + document siblings | interrupt the speech response playback only; document sibling continues |
| channel=both | expose only if speech can detach while panel generation continues; otherwise split the route into siblings first |

Swift shows “正在停止语音” after sending. An applied control ACK means the
CAS control was accepted, not that the speaker is already silent. It shows
“语音已停止” only after same-boot/same-generation
`playback.progress(state="silenced")` reports that the conservative audible
horizon reached the interrupt boundary. A daemon boot change also proves the
old output device stream is gone. Any running task remains visibly running.

### D21. Explicit PTT interruption

The current 220 ms Return-hold threshold remains for compatibility. When the
hold becomes PTT rather than text-submit, Swift immediately sends a
`ClientEnvelope<InputPTTBegin>` whose payload is:

```json
{
  "capture_id": "CAPTURE...",
  "expected_response_id": "RESP...or-null",
  "expected_playback_generation_id": 3,
  "client_started_at_ms": 1788200000000
}
```

This happens before key-up, WAV finalization, upload, and ASR.

The expected response ID and playback generation are either both present or
both null. Any mixed pair is invalid.

Explicit PTT is not a reversible candidate duck. At the confirmed threshold
it mechanically applies the current ResponseInterruptPolicy:

- the normal interactive speech-only policy performs playback CAS first and,
  when `generation_action=cancel`, issues
  `ResponseCancelRequest(scope="generation")` to L3 for that speech
  ResponseRun;
- a policy that keeps generation alive performs only `foreground_output`
  interruption;
- an interruptible `channel=both` route must first be split into speech and
  document siblings;
- PTT never names or cancels an ActionRun.

The ACK returns a random opaque `capture_token`, `expires_at_ms`, `boot_id`,
and interrupt result. The token is bound to authenticated principal, client
instance, PTT-begin request ID, capture ID, and boot; it is short-lived and
single-claim.

Recording and ASR continue independently. V2 uses the authenticated endpoint:

```text
POST /inherent/asr-submit/v2

request_id             # this is the upload request ID
client_created_at_ms
capture_token
capture_id
client_instance_id
audio_sha256
```

Its response adds:

```text
session_id
utterance_id
turn_id
status
text
emotion
```

For ASR, the common `request_id` is the upload request ID and is distinct
from the PTT-begin request ID. One short transaction claims the request
receipt plus capture token as `processing`; audio decode/ASR never holds the
SQLite transaction. A final `BEGIN IMMEDIATE` verifies the same request/hash,
appends exactly one `utterance.received`, and stores
`(request_id, audio_sha256, input_event_uid, session_id, utterance_id,
turn_id, result)` before commit. A retry joins/returns the same result. After
a crashed processing lease expires, an identical re-upload may resume the
same request; no event existed before the final transaction. A different
hash is rejected.

`input.ptt_cancel` releases an unclaimed capture and is sent on Escape,
recording failure, or explicit abort. Client crash/timeout expires it
automatically. A stale expected playback generation is a safe no-op for
playback and does not itself fail ASR.

PTT capture gains configured duration and byte caps. System-media ducking through AppleScript stays off the MainActor and outside the Jarvis playback hot path.

#### V2 text and image input

All repository-owned v2 input uses authenticated/idempotent endpoints:

```text
POST /inherent/submit/v2
POST /inherent/image-submit/v2
POST /inherent/asr-submit/v2
```

Common client request fields are `request_id, client_instance_id,
client_created_at_ms`. `client_created_at_ms` is untrusted telemetry only;
the authenticated endpoint stamps `source_surface=inherent_v2` server-side.
Text adds raw text, which the server normalizes. Image
submit is one bounded authenticated multipart request and adds
`mime_type, byte_count, sha256` plus the file part. There is no second public
staging endpoint in v2.0. The server streams to a private temporary file,
checks the advertised bounds/hash/type, finalizes a content-addressed
artifact, then commits the idempotency receipt plus canonical input event
that references it. A crash before the Event Log commit may leave only an
unreferenced content-addressed artifact, which startup GC may remove; it may
not leave a submitted turn without its artifact. Identical retries resolve
the receipt/reference rather than restaging. Until the **image** route
exists the advertised `image_input` capability remains false; the text and
ASR routes are built.

The accepted response returns
`status, request_id, input_event_uid, turn_id, session_id?, artifact_ref?`.
The canonical
input event carries `source_client_request_id`. A later
`response.opened` carries that source request ID and its newly minted
`response_group_id`:

```text
local pending input
→ request_id
→ input_event_uid / turn_id
→ response_group_id / response_id
```

An identical request-ID/payload-hash retry returns the original result.
Different payload is rejected. A lost HTTP response therefore cannot
duplicate a turn.

All three routes use `InputSubmissionInbox` with the idempotency key
`(authenticated_principal, client_instance_id, request_id)`. For text, one
`BEGIN IMMEDIATE` claims/resolves the payload hash, appends the exact canonical
input event carrying `source_client_request_id`, stores its IDs/result, and
commits. Image performs content-addressed file finalization first, then uses
that same transaction for receipt + input event + artifact reference. ASR
uses the processing lease and final transaction described above. Only a
committed canonical input event enters the durable intent pump; there is no
receipt-without-event or event-without-result window.

The inbox server-mints `turn_id` before that final transaction and writes it
into the canonical `surface.user_intent`/`utterance.received` payload and
receipt/result. Clients never choose a turn ID. ADR-0008's
`claim_input_once(trigger_event)` — named `claim_turn_once` in this ADR's
first draft — reuses that exact payload turn ID when it appends
`turn.started`; it may not mint a replacement.

### D22. Cancel task

The payload inside `ClientEnvelope<CancelAction>` names one exact action:

```json
{
  "action_id": "A...",
  "expected_action_revision": 1846
}
```

Runtime converts this into a `UserResponse`/`surface.user_intent`. L3:

- re-resolves the canonical live ActionRef;
- rejects missing, ambiguous, terminal, or stale targets;
- applies the existing tool surface, entity trust, policy, lease, and confirmation rules;
- dispatches the registered `cancel_action` only after a durable gate pass.

Swift may show `cancel_submitting` and then `cancel_requested` as local overlays. It must keep the canonical row “running” until `action.cancelled` appears. Unsupported cancel restores the canonical running display and explains the safe reason. Cleanup pending/quarantined remains visible after the action terminal when resources are not yet safely released.

### D23. Confirmation decisions

The payload inside `ClientEnvelope<ConfirmationDecision>` is:

```json
{
  "confirmation_id": "CONF...",
  "expected_revision": 1844,
  "decision": "accept"
}
```

L5 performs authentication and schema validation only. It constructs:

```text
UserResponse(
  response_type="confirmation_decision",
  source_surface="inherent_v2",
  source_event_id=<exact confirmation.requested uid>,
  payload={
    confirmation_id, expected_revision, decision,
    client_instance_id, request_id
  }
)
```

Runtime routes that exact typed object to one serialized L3
`ConfirmationDecisionRunner`. It is never converted into the unscoped text
“可以/不要.” L3 re-resolves the exact current slot and calls
`ConfirmationTerminalizer` for an atomic compare-and-append. Two clients,
expiry, or supersede races produce one winner and stale losers.

On accept, ADR-0012 D6 remains unchanged after the terminal:

```text
verify frozen snapshot/artifact
→ mint single-use lease
→ deterministic re-proposal
→ full pre_action_gate
→ authorized dispatch
→ fixed evidence-bounded response
```

The accepted/rejected registry advances to schema v2:

```text
required:
  confirmation_id
  response_source: voice_grammar | inherent_control

conditional voice_grammar:
  utterance_raw, grammar_rule_id

conditional inherent_control:
  client_instance_id, request_id, expected_revision

source_event_id:
  exact confirmation.requested uid
```

The registry gains a conditional validator; `PendingConfirmations` continues
to fold historical schema-v1 rows. A button does not fabricate a spoken
utterance or grammar hit.

The button is disabled after submission and displays “正在确认.” Duplicate
retries with the same idempotency key are idempotent. Stale, expired,
superseded, or mismatched requests fail closed and trigger projection repair
where needed.

Swift never appends `confirmation.accepted/rejected` itself. It waits for the canonical projection delta.

Confirmation controls are real SwiftUI `Button` values with keyboard and VoiceOver support; they are not answer-text parser decorations.

#### Semantic control idempotency and crash recovery

The idempotency key is:

```text
(authenticated_principal, client_instance_id, request_id)
```

For cancel, confirmation, and dismiss, one L2 `BEGIN IMMEDIATE` transaction:

1. reads or inserts the operational receipt;
2. rejects the same key with a different server-computed payload hash;
3. validates/appends or resolves the exact canonical UserResponse outcome;
4. stores `canonical_event_uid` on the receipt;
5. commits.

Only after commit may runtime route the canonical event. A crash after commit
is recovered from that event; retry returns the stored receipt and never
appends another semantic event. For confirmation, the same transaction is
owned by `ConfirmationTerminalizer`. For cancel/dismiss it appends the
corresponding `surface.user_intent/surface.dismissed` through
`SurfaceControlInbox`.

The receipt stores `processing_state=pending|consumed`. Runtime routes only
the committed canonical event UID, then marks the receipt consumed after L3
accepts it. Boot reconciliation re-enqueues every pending canonical UID.
Reprocessing is expected: L3 target/revision checks, terminalizers, gate
leases, and ActionRun idempotency make it a no-op or return the already known
outcome rather than repeating an external effect.

Authorization and dispatch use a second, distinct crash boundary. In the
same L2 transaction that appends any passing `gate.evaluated` for an
external-effecting ActionRequest, the gate inserts an
`authorized_dispatch_outbox` row keyed by the gate event UID and holding the
stable `action_id`, frozen bounded ActionRequest/reference, and hash. This
includes the original action deterministically re-proposed after an accepted
confirmation, not only cancel/dismiss controls.

For a confirmation-backed request this transaction first re-reads and claims
`source_confirmation_event_id` exactly once, revalidating the current slot,
expiry, scope, and `max_uses`. Uniqueness is on the canonical accepted-event
UID, not a freshly generated lease ID. The passing gate event, accepted-event
claim, and outbox row either all commit or all roll back; a losing concurrent
runner returns `AlreadyConsumed` and has no dispatchable debt. Event insertion
uses the no-commit L2 append primitive, and no sequencer/direct-bus publish is
allowed before the outer commit succeeds.

Runtime claims the row with a boot/lease owner and routes it only through the
typed L4 authorized-dispatch port. There is no cancel variant: `cancel_action`
is an ordinary L2 tool admitted by the Pre-action Gate's admission arm
(ADR-0008 D10) and never confirmation-backed, so it never enters this outbox.
L4 atomically appends or resolves
`action.dispatched` for that same stable action ID while consuming the
outbox; only then may an external-effecting handler start. Boot
reconciliation reclaims expired outbox leases. If a durable dispatch exists
but outcome is unknown, L4 uses tool-specific postcondition reconciliation;
a non-idempotent append/write is never blindly re-executed. A terminal action
returns its stored outcome. The outbox is operational debt, not an extra
authorization fact or ActionLifecycle state.

The receipt table is mutable operational delivery metadata, not projection
truth and not evidence that content was seen/heard. Canonical outcomes remain
append-only Event Log events.

One serialized ConfirmationDecisionRunner handles both live decisions and
boot debt. If a crash leaves `accepted_unconsumed`, the supervisor resumes the
existing frozen-snapshot re-proposal; it does not accept a second human
decision. Serialization is an efficiency choice, not the safety boundary: the
L2 accepted-event consumption CAS plus the gate/outbox transaction prevent
concurrent accept/replay from dispatching twice even with two runners or boot
debt duplication.

Boot-scoped stop-speaking/PTT controls use generation CAS, the capture-token
claim, and a bounded in-memory request cache; their old targets become stale
on restart and do not need a durable semantic receipt.

### D24. Escape, dismiss, and clarify

- While local PTT capture is active, Escape cancels local recording and sends
  `input.ptt_cancel` for its server capture lifecycle.
- Otherwise Escape hides the floating panel locally.
- Escape never means reject confirmation, stop speech, cancel response, or cancel task.
- An explicit “Dismiss this item” control sends `control.dismiss_item` and may become `surface.dismissed` after routing.
- Clarification is submitted as text/voice user input with the owning subject/group reference; it is not a local rewrite of projection truth.

## 9. Product behavior

### D25. Three visible status lanes

The compact card presents independent lanes:

1. **Input lane** — listening, transcribing, input error.
2. **Foreground response lane** — generating commentary, speaking, finalizing, interrupted, failed.
3. **Task lane** — waiting, dispatched, running, cancel requested, cleanup pending, completed/failed.

Connection/reconnect is a separate banner. Pending confirmation is a blocking action card.

Examples:

| Runtime truth | User-visible result |
|---|---|
| listening + speech ducked | “正在听” and “Jarvis 语音已降低” |
| commentary complete + action running | commentary remains; task row says “任务进行中” |
| speech interrupted + document generating, horizon pending | “正在停止语音”; document keeps streaming |
| response completed + playback interrupted + silenced | “回答已生成”; speech badge says “已停止” |
| TTS failed + document completed | “语音不可用，文字回答已完成” |
| action failed + final explanation generating | failed action row plus “正在整理错误信息” |
| old task running + new turn listening | new turn foreground; old task in background shelf |
| reconnecting | old content stays visible under reconnect banner |

### D26. Commentary, final, speech, and document

- Commentary is short, truthful, and tied to a real ResponseRun or action event.
- Final carries the conclusion/result.
- Speech shows the short answer/transcript and playback state.
- Document carries complete evidence, code, tables, and long-form detail.
- Sibling responses share a group but never share cancellation or terminal state.
- The client may visually collapse completed commentary, but it does not delete it while the group/action is active.
- Markdown parsing remains a renderer. It is not used to discover tool, progress, confirmation, or action semantics.

### D27. Fade and history

The following states never auto-fade:

- listening or transcribing;
- playback buffering, speaking, ducked, or draining;
- waiting/running action;
- pending confirmation;
- a pending local control, cancel requested, or cleanup pending;
- reconnecting/offline;
- actionable failure.

A terminal final response may fade after a user-configured idle period only
when every visible response delivery/playback is terminal, the group has no
active action/confirmation/control, and accessibility focus is not inside it.
Fading hides the window; it does not delete state.

History IDs come from `response_group_id`, never random client UUIDs. Snapshot replay cannot duplicate a history item.

### D28. Offline degradation

When the daemon is unavailable:

- keep draft, last document, task shelf, and confirmation display;
- mark them stale with last-updated time;
- disable server-mutating buttons;
- offer reconnect/retry;
- never pretend a local recording is being transcribed if upload did not start;
- do not retain raw failed audio by default;
- show capability downgrade: MiniMax → system voice → text-only, without changing document truth.

## 10. Accessibility and rendering performance

### D29. Accessibility

The client must support:

- `accessibilityReduceMotion`: no pulse, waveform loop, character drip, animated scroll, or moving transition;
- status labels and icons, never color alone;
- Dynamic Type/large text without clipping confirmation/actions;
- keyboard traversal for confirmation, stop speech, cancel task, history, and retry;
- focus moves to a newly presented confirmation title, but terminal updates do not steal focus back to input;
- VoiceOver milestones only: Listening, Transcribing, Needs confirmation,
  Task started, Task failed, Final ready;
- no VoiceOver announcement per token, PCM chunk, or high-frequency progress update;
- while Jarvis TTS is active, announce “Jarvis 正在说话” rather than reading the entire same answer simultaneously;
- ordinary semantic segments update readable content but do not post
  announcements; confirmation/actionable errors may be assertive.

An `AccessibilityDeliveryLedger` keyed by
`(milestone, entity_id, revision)` survives snapshot replacement within the
client process, so reconnect does not re-announce or re-focus the same
confirmation/final. Moving VoiceOver accessibility focus is distinct from
changing the AppKit keyboard first responder; snapshot adoption never steals
keyboard focus while Allen is typing or holding PTT.

### D30. Remove artificial display latency

V2 renders each permitted semantic segment atomically or with a visual transition no longer than one display frame. The current 8–30 ms per-character drip is not used for v2.

Markdown parsing/rendering is batched at most once per display frame while segments arrive. Large code/document blocks are parsed off-main where safe and published as immutable render models. One reducer application must not synchronously parse an unbounded document.

### D31. Initial local SLOs

These exclude cloud LLM, external tool, ASR model, and TTS provider time:

| Metric | Target |
|---|---|
| local key/click → immediate local input/control feedback | p95 ≤ 16 ms |
| accepted input event → response.started/thinking visible | p95 ≤ 100 ms |
| action lifecycle commit → task-lane state visible | p95 ≤ 50 ms |
| committed semantic event → v2 client enqueue | p95 ≤ 10 ms |
| WebSocket frame receipt → MainActor reducer commit | p95 ≤ 16 ms, p99 ≤ 50 ms |
| safe segment commit → visible text | p95 ≤ 50 ms, p99 ≤ 100 ms |
| recognized PTT hold → playback CAS applied | p95 ≤ 120 ms |
| initial key-down → explicit PTT playback stop, including 220 ms threshold | p95 ≤ 350 ms |
| playback CAS applied → conservative physical-silence horizon | p95 ≤ 150 ms |
| Stop button/key → conservative physical-silence horizon | p95 ≤ 250 ms |
| hung local daemon → reconnect/stale banner | p95 ≤ 3.5 s |
| daemon endpoint available → snapshot applied | p95 ≤ 1 s for ≤ 1 MiB snapshot |
| local reconnect hello + bounded snapshot adoption | p95 ≤ 500 ms for ≤ 1 MiB snapshot |
| reducer main-thread work per normal delta | ≤ 8 ms |
| v2 visual tail after final segment | ≤ 50 ms; no character-drip seconds |
| slow panel effect on another client/TTS/control | no measurable queue coupling; regression budget ≤ 5 ms p95 |

These budgets do not allow a 20-second silent pre-model window:
`response.started` is committed before provider inference begins, and a real
action status replaces it when applicable.

Every stage emits trace ID, warm/cold state, payload-size bucket, queue/load
bucket, and monotonic trace points:

```text
event_committed
view_sequencer_woken
view_delta_encoded
client_enqueued
socket_sent
socket_received
reducer_applied
first_segment_visible
control_received
playback_cas_applied
estimated_dac_silence
snapshot_begin/end/applied
```

Within one process, spans use its monotonic clock. Cross-process E2E burns use
a dedicated harness based on Darwin continuous-time ticks exposed in both
Python and Swift (or report separate server/client spans when calibration is
unavailable); arbitrary process-local clocks and wall timestamps are never
subtracted.

## 11. Failure modes

| Failure | Required behavior |
|---|---|
| malformed/oversized frame | reject frame; close on protocol abuse; no partial control |
| auth token stale | reload token once, then normal backoff |
| old socket callback | ignore by socket epoch |
| log epoch mismatch | require full snapshot |
| boot changes | clear ephemeral state; adopt durable snapshot; no speech replay |
| cursor regression/conflict | retain last valid UI; request resync |
| response sequence gap | retain complete prefix; buffer bounded future segments; resync |
| snapshot page/hash mismatch | discard staging snapshot; keep old UI; retry |
| conflict after resync requested | freeze apply/ACK; close epoch; full snapshot on new socket |
| durable client queue overflow | send resync-required if possible; close only that client |
| ephemeral queue overflow | coalesce by key |
| ephemeral sequence regresses/gaps | ignore regression; gaps legal; post-snapshot baseline repairs latest state |
| control lane floods/overflows | bounded fairness; close only abusive/stuck client |
| control ACK lost | retry same request ID; idempotent result |
| same request ID/new payload | reject as idempotency conflict |
| confirmation expires/supersedes during click | atomic terminal CAS yields one winner; stale loser; never retarget |
| accepted confirmation crashes before dispatch | accepted-unconsumed debt runner resumes frozen re-proposal once |
| stop target stale | safe no-op/stale ACK; do not stop newer playback generation |
| PTT capture cancelled/client dies | cancel/TTL releases unclaimed token; hard interrupt never leaves permanent duck |
| ASR upload ACK lost | same upload ID/audio hash returns original utterance/turn; no duplicate event |
| v2 input auth missing | reject before artifact/audio decode or event append |
| action cancel unsupported | action remains canonical running; show explanation |
| action process terminal but cleanup pending | show terminal plus cleanup/quarantine state |
| TTS fails after partial speech | playback failed/interrupted; document sibling continues |
| panel hidden mid-stream | keep reducer state; render latest state when reopened |
| daemon offline during PTT upload | preserve draft/transcript metadata when safe; raw audio not retained by default |

## 12. File-level change map

This section is a historical seed for goal cards under `docs/goals/`, not an acceptance contract; §15 Verification and §18 Definition of done remain binding.

### New repository-owned Swift files

Under `desktop/inherent-swift/InherentCard/`:

- `RealtimeProtocol.swift` — versioned Codable envelopes/payloads and strict discriminator decode.
- `RealtimeState.swift` — orthogonal response/action/input/playback/connection state.
- `RealtimeReducer.swift` — pure transition logic and validation.
- `RealtimeStore.swift` — sole MainActor state owner.
- `WebSocketTransport.swift` — authenticated transport actor, socket epoch, hello, ping, reconnect, ACK.
- `RealtimeEffects.swift` — network/presentation/accessibility effect runner.
- `InherentControls.swift` — typed stop/PTT/cancel/confirmation/dismiss requests.
- `InputSubmissionClient.swift` — authenticated idempotent text/image/ASR v2 requests and correlation.
- `RealtimeCardViews.swift` — foreground response, task shelf, confirmation, connection/status lanes.

Under `desktop/inherent-swift/InherentCardTests/`:

- `RealtimeProtocolTests.swift`
- `RealtimeReducerTests.swift`
- `SnapshotAdoptionTests.swift`
- `WebSocketTransportTests.swift`
- `RealtimeControlTests.swift`
- `RealtimeAccessibilityTests.swift`
- `InherentCardUITests/` — keyboard, focus, window, VoiceOver-labelled control, and end-to-end UI tests.
- `Fixtures/V2/*.json` — canonical cross-language fixtures.

### Existing Swift files that change

- `Project.yml` — explicit fixture resources, UI-test target/test plan, and `SWIFT_STRICT_CONCURRENCY=complete`.
- `InherentCardApp.swift` — v1/v2 store/transport startup and clean shutdown.
- `BridgeBackend.swift` — keep HTTP/v1 adapter; move v2 types/transport out; retain returned IDs.
- `NativeCardModel.swift` — shrink to local presentation/input adapter or replace with RealtimeStore for v2.
- `NativeCardController.swift` — one store/transport wiring path; remove unstructured v2 message hops; hiding does not discard.
- `NativeCardView.swift` — bind typed state and real controls; keep Markdown renderer as content renderer only.
- `NativeVoiceRecorder.swift` — duration/byte caps and capture-token correlation; whole-WAV fallback remains.
- `SystemAudioDucker.swift` — remove synchronous AppleScript from MainActor hot path.
- `launcher.py` — resolve runtime root/token-file path, expose v1/v2 mode,
  regenerate when the source/test/resource set changes, and invoke
  incremental `xcodebuild` before every launch instead of using binary
  existence/mtime as the build decision.
- `README.md` — replace the legacy server instructions with the repository-owned daemon/build/test path.

`OutputSpeechPlayer.swift` is not introduced/connected. Python remains the sole response speech owner.

### New Python files

- `jarvis/surface/inherent_protocol.py` — v2 DTO schemas/codec; imports shared IDs but owns L5 wire.
- `jarvis/surface/inherent_presenter.py` — projection/event to safe view DTO mapping.
- `jarvis/runtime/inherent_hub.py` — per-connection client sessions, snapshot staging, ACK/backpressure; runtime rather than L5 because D9 gives client orchestration to the runtime.
- `jarvis/surface/legacy_v1_serializer.py` — one pinned compatible final/document stream per v1 turn.
- `jarvis/runtime/inherent_view_sequencer.py` — ordered Event Log drain, projector, snapshot/live barrier.
- `jarvis/state/inherent_view.py` — the L2 Inherent view fold: response-group truth from `surface.response_*` rows, immutable checkpoints, and the terminal-group and inline-body bounds of D16.
- `jarvis/state/control_inbox.py` — operational idempotency receipt plus same-transaction canonical UserResponse append; never a competing domain truth.
- `jarvis/state/input_submission_inbox.py` — authenticated text/image/ASR request receipts, processing leases, same-transaction canonical input append/result correlation, and crash recovery.
- `jarvis/state/authorized_dispatch_outbox.py` — gate-bound debt, stable ActionRequest identity, lease claim, dispatched-before-effect handoff, and boot reconciliation for all authorized external actions.
- `scripts/test_inherent_swift.sh` — XcodeGen/build/unit/UI test entry used locally and by macOS CI.
- `scripts/bench_inherent_realtime.py` — trace/SLO report harness.

### Existing Python files that change

- `jarvis/surface/inherent_server.py` — v2 endpoint/auth/hello/control receive; v1 remains.
- `jarvis/surface/inherent_output.py` — v1 broadcaster stays isolated; no v2 sequential fan-out.
- `jarvis/surface/voice_pipeline.py` — capture-token claim, upload idempotency, and returned ID correlation.
- `jarvis/runtime/inherent_loop.py` — instantiate sequencer/hub/control router and lifecycle.
- `jarvis/deployment/__init__.py` — unique runtime token path/creation/permission owner.
- `jarvis/state/event_log.py` — immutable log-epoch metadata; v2/expiry/failure registrations and conditional validators.
- `jarvis/state/lifecycle_terminal.py` — confirmation and surface-delivery atomic terminalizers alongside ADR-0006/0008 terminal primitives.
- `jarvis/state/projections.py` — ActionViewProjection and snapshot support; reuse ResponseLedger/PendingConfirmations.
- `jarvis/shared/realtime.py` — reuse IDs/contracts from ADR-0006/0008; do not duplicate wire DTOs here.
- shared `UserResponse` contract — typed confirmation/cancel/dismiss sources and exact target revisions.
- `jarvis/decision/__init__.py` / `confirm_grammar.py` / `policy.py` — shared ConfirmationDecisionRunner, typed source metadata, exact response interrupt/action cancel policy.
- `jarvis/cli/__init__.py` — authenticate repository-owned mutations;
  harden v1 by validating `turn_id` and optional response ID on every
  append/done; bound its receive queue. It does not consume v2.
- configuration schema — v2 feature flags, queue/snapshot/auth/heartbeat budgets.
- macOS CI workflow/test plan — run the shared Python fixtures plus Xcode unit/UI suites.

### Canonical fixture rule

Python generates/validates the JSON fixtures stored in the Swift test fixture directory. Swift decodes the same bytes. There are not two hand-maintained fixture sets.

## 13. Compatibility and rollout

### D32. Never mix v1 and v2 in one reducer

- `/inherent/ws` sends only v1 `open/append/done/reset/voice`.
- `/inherent/ws/v2` sends only v2 envelopes after hello.
- `LegacyBridgeAdapter` may translate v1 into a synthetic single response group for fallback.
- `RealtimeReducer` never accepts a v1 dictionary.
- A client selects one mode for one socket.
- The v2 client model lives in its own Swift module, the `InherentRealtime` static library target (Swift 6 language mode, strict concurrency complete), while v1 stays in the `InherentCard` target.

`LegacyV1Serializer` is the only producer for future v2-identified events on
the v1 endpoint. Per submitted turn it selects at most one eligible stream:

```text
phase == final
channel in {document, both}
```

Once selected, it pins `(turn_id, response_id)`. Only that identity may emit
v1 open/append/done. Commentary, speech-only siblings, corrections, and other
responses do not enter that turn's v1 stream. Cancel/failure of the selected
response emits one bounded safe terminal line plus done, so a CLI cannot wait
forever. Historical events without v2 IDs retain old behavior.

If `turn.failed` occurs before any eligible response is selected, or all
ResponseRuns for that submitted turn become terminal without an eligible
`final + document|both` stream, the serializer emits exactly one bounded
synthetic `open/append/done` failure stream keyed to that turn. It then seals
the turn against any second fallback. The forwarding CLI therefore has a
terminal path even when selection never began.

The forwarding CLI filters turn ID on open, append, and done and, when
present, response ID too. Its queue is bounded.

Legacy Swift remains incapable of concurrent anonymous turns. Therefore:

- dual/v2-default may expose v1 to the hardened forwarding CLI, not an
  unmodified legacy Swift card;
- rollback to legacy Swift is one coupled configuration change that also
  disables sibling/interleaved realtime delivery and restores the serial
  full-response renderer;
- reconnecting legacy Swift alone to a realtime v1 broadcast is not a valid
  rollback.

In `dual` and `v2_default`, the hardened repository CLI authenticates both
its submit request and the v1 WebSocket upgrade with the same runtime token;
unauthenticated v1 read or mutation is disabled. Unauthenticated v1 is
available only under the explicitly enabled legacy policy in `off/shadow`.

### Feature modes

```text
off:
  serial full-response v1; legacy unauthenticated ingress may be explicitly enabled

shadow:
  v2 sequencer/projection/fixtures run; no production Swift cutover;
  legacy ingress policy remains explicit;
  selected by realtime.inherent.v2_sequencer.enabled (requires realtime.enabled)

dual:
  v1 CLI endpoint + v2 endpoint; repository CLI and Swift authenticate every
  mutation; unauthenticated mutation disabled by default

v2_default:
  Swift uses v2; hardened CLI/v1 envelope rollback remains;
  unauthenticated mutation disabled

v1_retired:
  future decision only, after usage and rollback review
```

Rollback changes the Swift launcher/config back to v1 and disables the v2 endpoint. It does not require reverting Event Log data or ADR-0006/0008 response events.

## 14. Build order and dependency gates

This section is a historical seed for goal cards under `docs/goals/`, not an acceptance contract; §15 Verification and §18 Definition of done remain binding.

| Step | Work | Required proof |
|---|---|---|
| 0 | Inventory three Swift copies; vendor the audited repo commit/subtree; record behavior decisions; generate Xcode project; baseline unit/UI behavior; fix launcher stale project/binary detection | clean archive baseline green; provenance/hash/toolchain manifest; resize/hotkey/window/history behavior disposition |
| 1 | Amend ADR-0006/0008 identity vocabulary; define Client/Server envelopes, one-row/one-batch durable schema, v2 input/control payloads, and canonical fixtures; harden CLI v1 identity/queue | Python and Swift decode the same bytes; generic generation ambiguity absent; v1 smoke unchanged |
| 2 | Implement strict-concurrency Swift state/reducer/store/single mailbox with no production socket | exhaustive reducer/Sendable/order/resync tests; no SwiftUI needed |
| 3 | Add log epoch, terminalizers, confirmation schema/expiry, control/input receipts, authorized-dispatch outbox, ActionView/Response/Pending projections, presenter | atomic terminal/idempotency/fold/debt tests |
| 4 | Add level-triggered ordered sequencer plus authenticated v2 hub, hello, paged snapshot-ACK-catch-up-live barrier, ephemeral baseline, and per-client queues | concurrent snapshot race, ACK validation, slow/flood client, overflow, lost-wake, restart tests |
| 5 | Add authenticated idempotent text/image/ASR endpoints and connect Swift v2 transport/input clients in shadow/dual mode | request→input→turn→group correlation; lost HTTP ACK retry; reconnect keeps content; old epoch ignored |
| 6 | Route response/action/confirmation mutations as one-row `view.delta` batches; render cockpit state | sibling/interleaved groups never mix; v1 serializer pins one stream; no character drip |
| 7 | Add playback/input ephemeral projection and exact stop/PTT begin-cancel-upload control | hold interrupts correct speech response before ASR; token/auth/hash retry rules; action/document remain correct |
| 8 | Add task shelf, durable cancel-request state, confirmation buttons, dismiss/clarify and accepted-unconsumed debt | two-client/expiry/supersede CAS; full ADR-0012 gate; idempotent recovery |
| 9 | Complete UI tests, accessibility ledger, reduce-motion, offline/freshness, artifact detail, trace harness, and SLO gates | UI/accessibility snapshots; warm/cold/load-tagged performance budgets |
| 10 | Run authenticated dual-path live burns; make v2 default only after ratchet gates | v1 CLI and coupled legacy-Swift rollback proven; no P0/P1; Allen approves cutover |

Dependency gates:

- ADR-0008 may implement ResponseRun/backend Steps 1–7 before this ADR, but must not enable interleaved Swift realtime delivery until Steps 0–6 above pass.
- ADR-0006 may implement media/player Steps 1–5 before this ADR, but explicit Swift PTT hard interruption cannot be declared complete until Step 7 above passes.
- Confirmation/action controls require the existing ADR-0012 projection and ADR-0008 canonical ActionRef/cancel gate; they never add a shortcut.
- V2 does not become default merely because protocol tests pass; it must pass live reconnect, long-action, interruption, slow-client, and accessibility burns.

## 15. Verification

### 15.1 Python–Swift contract tests

1. Every server/client message has a golden JSON fixture decoded by Python and Swift.
2. Missing required IDs, negative sequence/cursor, invalid enum, oversized text, and wrong delivery class fail closed.
3. New optional fields are forward compatible.
4. Unknown durable mutation in the selected schema prevents ACK advancement and closes with protocol error; a newer required schema fails hello.
5. Snapshot and delta use the same response/action/confirmation DTOs.
6. Full WS `ClientEnvelope` fixtures for stop, PTT begin/cancel, cancel action,
   confirmation, resync, and ACK pass Python/Swift validation. Separate HTTP
   request/response fixtures plus the image/ASR multipart manifest fixtures
   cover text/image/ASR; they are never decoded as WebSocket envelopes.
7. Reusing a request ID with a different payload is rejected.
8. One Inherent-relevant Event Log row maps to one view.delta; a multi-change batch applies atomically and ACKs once, while an irrelevant row only advances scan cursor.
9. V1 golden envelopes remain byte/field compatible.
10. Injected crashes around text/image/ASR receipt/event/result commits never
    create an orphan receipt, duplicate input/turn, or input event without its
    required artifact/result reference.

### 15.2 Sequencer/snapshot/transport tests

1. Commit notification and fallback watcher for the same row emit once.
2. Delayed direct notification cannot make a chunk cross its open.
3. Snapshot H, ACK, actor-captured B, catch-up, and atomic live-frontier switch with concurrent commits loses and duplicates nothing.
4. Snapshot page loss/hash conflict never partially replaces state.
5. No cursor > H is sent before snapshot ACK; post-ACK catch-up cannot be overtaken.
6. Log epoch mismatch forces snapshot.
7. Boot change clears ephemeral input/playback but restores durable document/action/confirmation.
8. Two clients, one artificially slow: fast client and TTS/control remain within budget.
9. Durable/control queue overflow closes/resyncs one client; control fairness holds; ephemeral updates coalesce.
10. ACK regression, ACK beyond last sent, wrong snapshot ID, and no-progress deadline follow D11.
11. Lost/coalesced wakes still advance scan_cursor across all rows.
12. Ephemeral pre-snapshot data is discarded; baseline then increasing/coalesced updates produce current state. A5/B6/A7 replacement and baseline-vs-live races preserve B6 then A7 semantics and the captured watermark.
    Same-daemon Swift restart after a silenced item left the generic cache
    still restores `silenced` from the playback registry; a 33rd distinct key
    yields ordered clear/new or client resync, never stale state.
13. ACK timeout and old socket callbacks cannot tear down a newer socket.

### 15.3 Deterministic reducer tests

Identity/order:

- duplicate event/message gives identical state;
- segment order 0,2,1 renders only 0, then 0/1/2;
- gap overflow requests one resync and never displays corrupt suffix;
- terminal before a missing segment does not fabricate completion;
- a late panel segment cannot alter a terminal/new response; playback generation N cannot alter N+1;
- boot A delta after boot B snapshot is ignored;
- old socket epoch failure cannot alter the new connection.

State combinations:

- listening and speaking/ducked coexist;
- speech interruption leaves linked action running;
- response completed stays completed when playback later interrupts;
- playback interrupted/CAS ACK remains “正在停止” until matching-boot/generation
  `silenced`; stale or wrong-generation silence cannot change the badge;
- TTS failure plus document completion is a text-only success;
- commentary completion plus action running remains waiting-action;
- action result creates/updates final sibling without overwriting commentary;
- old action remains in background shelf during a new foreground turn.

Controls:

- stop-speaking effect contains no action ID;
- cancel-action requires one exact live action and expected revision;
- queued cancel ACK does not set canonical cancelled;
- only action projection terminal sets cancelled;
- Escape hides/cancels local capture only;
- duplicate confirmation click emits one request;
- stale/expired confirmation fails closed;
- two clients accept/reject or expiry/supersede races produce exactly one terminal;
- `confirmation.requested(B)` clears A as superseded and upserts B in one
  atomic delta; an in-flight A decision becomes stale;
- accepted-unconsumed restart resumes one deterministic re-proposal;
- crash before/after gate pass, authorized-outbox claim, and
  `action.dispatched` neither loses the action nor blindly replays an
  ambiguous non-idempotent effect;
- stop speaking never emits a ResponseRun terminal;
- PTT begin/cancel/upload token and hash retries are idempotent;
- text/image/voice lost-ACK retries create one input/turn;
- reconnect snapshot restores the one live confirmation.
- cancel-request received/authorized restart recovery reaches rejected,
  failed, quiescing, or resolved and never remains permanently pending;
- cancel versus normal target completion, unsupported handler, duplicate
  request, and cleanup quarantine preserve separate truths;

Offline/restart:

- disconnect preserves last content and draft;
- snapshot cursor filters all older deltas;
- restart never emits speech effect;
- failed submit keeps retryable draft;
- capability downgrade selects correct text/voice UI.

Property tests add legal duplicates, stale playback generations, reconnect
boundaries, batched mutations, and permitted permutations; final reducer
state must equal the canonical snapshot state.

### 15.4 UI and accessibility tests

- `⌘ Space` global hotkey still opens the card; repeated presses do not create duplicate controllers or sockets;
- IME marked-text Return is owned by `NSTextInput` and never submits or starts PTT;
- ordinary Return and keypad Enter have identical short-press/220 ms hold behavior;
- key repeat creates at most one `input.ptt_begin` for a physical hold;
- Escape, focus loss, and recording-start failure each send at most one
  matching `input.ptt_cancel`; a confirmation accessibility-focus move does
  not take ownership of those keys;
- top-right anchoring follows the active display and remains on-screen across display add/remove, resolution, and scale changes;
- non-card window regions preserve passthrough hit testing, while every visible control remains clickable;
- drag position restoration and the Step-0-approved resize behavior survive relaunch and snapshot replacement;
- follow-up submission, draft restoration, clipboard image, drag/drop image, history popover, and history clear retain their current user-visible behavior, subject to advertised v2 capabilities;
- fade-generation cancellation prevents an older timer from hiding newly active content;
- parent-process watchdog and clean shutdown terminate the app without leaving a stale controller/socket;
- debug snapshot and fake-turn harnesses render through the same reducer path as production frames;
- golden/snapshot renderings for every combined state in §9;
- Listening + Speaking simultaneously visible;
- Stop speaking and Cancel task labels/targets visually distinct;
- speech stopped while task remains running;
- confirmation keyboard/VoiceOver behavior, with Escape not equal to reject;
- VoiceOver order: connection/status, question, response, tasks, actions;
- milestone announcements exactly once under duplicate deltas;
- snapshot/reconnect does not repeat confirmation focus/announcement or steal keyboard first responder;
- Reduce Motion removes pulses, waveforms, drip, and moving transitions;
- Dynamic Type does not truncate confirmation or destructive targets;
- active action/confirmation/reconnect/offline never auto-fades;
- disconnect banner does not cover the delivered document;
- long document rendering stays within main-thread budget.

### 15.5 Existing regression suites

Retain and run:

- Python Tier-1;
- `tests/integration/test_serve_inherent_smoke.py` v1;
- ASR/PTT integration and v1 TTS watcher suites;
- existing Swift BridgeDispatch, NativeCardModel, ReconnectBackoff, SubmitRequest, display/renderer tests;
- `xcodegen generate` followed by
  `xcodebuild test -project InherentCard.xcodeproj -scheme InherentCard -destination 'platform=macOS' -derivedDataPath build`;
- Swift UI-test plan and strict-concurrency build through `scripts/test_inherent_swift.sh`.

### 15.6 Live burns

1. Simple conversation: first semantic segment appears while the model still generates.
2. 30–60 second action: truthful commentary, persistent task row, new input accepted.
3. Speech + long document siblings: stop speech; document/action continue.
4. PTT during playback: measure key-down, hold recognition, CAS, estimated
   DAC silence, and physical loopback silence separately.
5. Two concurrent groups with an old action result arriving during a new turn.
6. Confirmation appears, app reconnects/restarts, decision remains safe and usable.
7. Network disconnect during segment; snapshot reconstructs without duplicate text or speech.
8. Daemon restart during open response and ActionRun reconciliation.
9. Slow/paused Swift receiver while TTS and a second client continue.
10. MiniMax failure before/after first audible segment; document fallback remains.
11. Mac sleep/wake and window hide/show with active action.
12. VoiceOver + Reduce Motion full interaction.

## 16. Spec changes and explicit deviations

1. Spec §3.6 remains authoritative: Inherent is a visual cockpit, not a state owner or tool executor.
2. Spec §3.6.3 `UserResponse` is the client-to-runtime contract; transport ACK/control ACK are operational and do not replace canonical events.
3. Spec §3.6.7 gains an explicit projection snapshot/delta transport contract for Inherent.
4. Spec §5.2/§5.4 gains no raw token/audio events. Canonical UserResponse
   outcomes remain events; transport ACK and idempotency receipts may be
   durable operational metadata but are not Event Log truth/evidence.
5. Spec §6 projections gain panel-ready response/action/confirmation reconstruction and freshness.
6. Spec §18's “voice short, panel rich” becomes mechanically representable through sibling response channels.
7. ADR-0003's outbound-only v1 remains compatibility-only.
8. ADR-0008 §4.3 is refined for panel delivery: the direct commit path wakes one ordered Event Log drain; it does not race a watcher as a second payload source.
9. `surface.response_emitted` remains panel delivery production truth, not ResponseRun or playback completion.
10. Client transport ACK remains explicitly weaker than panel-seen and unrelated to heard-state.
11. The registry/spec explicitly gain `surface.response_failed` and
    `confirmation.expired`, confirmation accepted/rejected schema v2
    conditional sources, and source-client-request correlation fields.
12. ADR-0006/0008's media field is renamed
    `playback_generation_id`; document panel streams use immutable
    `response_id + sequence` and never require a playback lease.
13. L2's log-epoch/control-receipt tables are operational metadata, not Event
    Type Registry entries or a competing State Object. L6 owns only runtime
    token-path creation and permission policy.
14. In dual/v2-default mode every repository-owned mutating surface request
    is authenticated; v1 envelope compatibility does not imply an
    unauthenticated control surface.

## 17. Consequences

### Positive

- Inherent can display safe model output with near-zero additional client tail.
- Long tasks remain usable because response, action, input, and playback no longer block or overwrite one another.
- Reconnect and restart become projection reconstruction rather than destructive reset.
- Typed controls make stop/cancel/confirmation semantics visible and safe.
- Slow clients are isolated.
- Python and Swift protocol changes become one repository/CI unit.
- The design retains Jarvis's state/evidence/gate model instead of trading correctness for a voice-demo illusion.

### Costs

- The v2 protocol, snapshot barrier, ActionView projection, and Swift reducer are meaningful multi-module work.
- V1 and v2 coexist during rollout.
- The Swift model/view must be decomposed rather than patched with more booleans.
- Cross-language fixtures and Xcode CI add maintenance.
- Snapshot paging and backpressure add protocol complexity.
- Transport ACK deliberately does not solve “did the human actually see this”; that remains a future product/evidence problem.

## 18. Definition of done

ADR-0014 is complete only when:

1. The Swift source is owned by this repository with a recorded baseline and green pre-change tests.
2. V2 is typed, authenticated, versioned, bidirectional, and isolated from v1.
3. ResponseRun, panel stream, and playback generation identities/terminal
   owners are distinct and ADR-0006/0008 use the amended names.
4. One ordered sequencer supplies exactly one atomic durable batch per
   Inherent-relevant Event Log row and no envelope for irrelevant rows.
5. Snapshot/ACK/catch-up/live handoff is atomic and passes concurrent-commit/restart tests.
6. Each client has bounded independent flow control; a slow/flood client cannot affect voice/control/another client.
7. Swift has one receive mailbox, one MainActor store, and a pure keyed reducer with deterministic gap/stale/duplicate/resync behavior.
8. Authenticated idempotent text/image/voice input closes request→turn→group correlation without duplicate turns.
9. Speech/document siblings and multiple response groups render without overwrite.
10. Stop speaking, PTT interrupt/capture, task cancel, and confirmation use exact typed targets and the correct terminal/gate paths.
11. Confirmation expiry/competition/crash debt and action cancel-request recovery are durable and deterministic.
12. Hiding/reconnecting never destroys semantic state, and historical speech never replays.
13. Per-character v2 drip is removed and local/silence/failure-detection SLOs pass.
14. Accessibility/Reduce Motion/keyboard/focus contracts pass.
15. V1 CLI and coupled legacy-Swift rollback path remain proven.
16. ADR-0006/0008 dependency gates and all live burns pass.
17. No P0/P1 finding remains from cross-layer, current-client, reliability, security, or accessibility review.
18. Allen changes this ADR's status to Accepted/Approved.
