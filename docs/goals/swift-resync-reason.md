# Goal: swift-resync-reason

## Goal
The Swift client surfaces the server's own `server.resync_required` reason
string to its caller instead of collapsing every such notice to
`protocol_error`.

## Why
The server now sends two distinct reasons on this one frame —
`client_backpressure` ("your lane filled up") and `frame_over_budget` ("one
frame is larger than the whole lane, no client can receive it") — and a client
that cannot tell them apart cannot tell a slow reader from an undeliverable
frame. Today the client sees neither.

This is a latent defect, not one a user is hitting: nothing in production Swift
constructs a `TransportInput.frame` yet (`grep -rn "\.frame(socketEpoch"
desktop/inherent-swift/` has no hit outside `InherentCardTests/`), so the
socket-to-transport wiring is unbuilt and no live socket reaches this decode
path — which is both why it is worth fixing now, before that wiring lands on
top of it, and why the acceptance below asserts on the transport's event sink
rather than on an end-to-end path that does not exist.

## Current behavior
- The hub builds the notice as a `delivery_class="protocol"` envelope whose
  payload is exactly `{"reason": reason}` — no `kind` field
  (`jarvis/runtime/inherent_hub.py:551` calls `_protocol_frame`, defined at
  `jarvis/runtime/inherent_hub.py:394`, which sets `payload=dict(payload)`).
- Exactly two reasons ever ride this frame. `_fail_now(..., notify=True)` is
  the only path that queues it, and it has exactly two call sites:
  `_OVER_BUDGET_REASON` = `"frame_over_budget"`
  (`jarvis/runtime/inherent_hub.py:95`, raised at `:313`) and
  `"client_backpressure"` (`:320`). Every other failure reason
  (`protocol_error`, `resync_required`, `ack_stalled`, `catch_up_budget`,
  `internal_error`, `send_failed`, `control_overflow`) closes with
  `notify=False` and never produces this frame.
- **The reason is on the wire twice and read zero times.** The close that
  follows carries the same string: `code = _CLOSE_PROTOCOL_ERROR if reason ==
  "protocol_error" else _CLOSE_RESYNC_REQUIRED` (`:553`), then
  `_close_after_flush(code, reason)` (`:565-571`), so the wire close is
  `(1008, "client_backpressure")` / `(1008, "frame_over_budget")` — asserted
  today at `tests/integration/test_inherent_flow_control.py:139` and `:291`.
  The client discards that copy too: `TransportInput.closed(socketEpoch:code:
  reason:)` becomes `.socketClosed`, and the reducer pattern-matches both
  associated values away — `case .socketClosed(let epoch, _, _)`
  (`RealtimeReducer.swift:31`). So the server states the reason twice, in two
  independent places, and the client currently learns it from neither. That
  also means the acceptance below has **two** server-side observables to assert
  on, not one: the notice payload and the close pair.
- Swift's frame router has no case for this message type. Its `default:` branch
  decodes any unrecognized `message_type` as `ServerEnvelope<EphemeralUpdate>`
  (`desktop/inherent-swift/InherentRealtime/RealtimeTransport.swift:271-278`).
  `EphemeralUpdate.init(from:)` calls `decodedKind`
  (`desktop/inherent-swift/InherentRealtime/RealtimeViewDTOs.swift:558`, helper
  at `:155`), which requires a `kind` string key. The payload has none, the
  decode throws, and the `catch` at `RealtimeTransport.swift:279-281` emits
  `.socketFailed(socketEpoch:reason: "protocol_error")`
  (`RealtimeTransport.swift:181`).
- Swift genuinely never reads this reason under any name.
  `grep -rn "resync_required\|resyncRequired" desktop/inherent-swift/` exits 1
  with no output — verified in this worktree, not taken on report. The
  `resyncRequested` flag and the `ResyncReason` enum
  (`desktop/inherent-swift/InherentRealtime/RealtimeEffects.swift:10-22`) are
  the D17 *client-initiated* resync vocabulary (`gap`, `hash_conflict`, …); they
  are a different concept from the server's close reasons and share no value.

## Target behavior
- The wire contract, in one sentence: **`server.resync_required` is a
  `delivery_class="protocol"` frame identified by its `message_type` alone,
  whose payload is exactly `{"reason": "<server reason string>"}` with no `kind`
  discriminator, and is followed by a `1008` close carrying the same reason
  string.**
- The Swift transport routes `server.resync_required` by `message_type`, like
  every other protocol-class frame, and emits
  `.socketFailed(socketEpoch:reason:)` carrying the server's `reason` verbatim.
- The reason travels as a `String`, not a Swift enum: a third server reason
  added later reaches the caller with no client change.
- A `server.resync_required` frame that is malformed (no `reason` key) still
  fails the socket with `protocol_error` — an unreadable control frame is a
  protocol violation, not a skip (ADR-0014 D6).
- The server side is unchanged, byte for byte.

## Affected contracts and files
- L5 surface `desktop/inherent-swift/InherentRealtime/RealtimeTransport.swift`
  — a `case "server.resync_required"` in `events(fromFrame:epoch:)` ahead of
  `default:`, decoding a small payload type with `reason: String`.
- L5 surface `desktop/inherent-swift/InherentRealtime/RealtimeViewDTOs.swift`
  — the one-field payload type belongs beside the other protocol-frame payload
  structs the transport decodes: `SnapshotBegin` (`:659`), `SnapshotPage`
  (`:686`), `SnapshotEnd` (`:720`). (`ServerHelloPayload` lives in
  `RealtimeProtocol.swift:437` instead, because the handshake is decoded before
  the frame router exists.) The implementation session confirms the file.
- Tests `desktop/inherent-swift/InherentCardTests/Realtime/RealtimeTransportTests.swift`
  — the new case; `RealtimeTestFixtures.swift` has builders only for `durable`
  and `ephemeral` frames (`:163`, `:183`), so a protocol-class frame is built
  inline or a builder is added.
- Tests `tests/integration/test_inherent_flow_control.py` — one added
  assertion on the exact notice payload in each of the two existing wire tests.

## Boundaries and non-goals
- Layers that may change: L5 surface (Swift client) and tests.
- Must not change: the `1008` close code (`_CLOSE_RESYNC_REQUIRED`,
  `jarvis/runtime/inherent_hub.py:91`); the server's reason vocabulary or *when*
  each reason is sent; the shape of the notice payload; the best-effort
  control-lane queuing and eviction in `_fail_now`. This card makes the reason
  **readable**, it does not redefine flow control — another lane owns that.
- Must not change: the reducer's response to `.socketFailed`
  (`RealtimeReducer.swift:37-40`, sets `state.connection = .disconnected`).
  What the client *does* differently per reason is a separate decision; this
  card only ensures the reason arrives.
- Non-goal, and here is why so the next reader need not re-derive it:
  **hardening the `default:` branch is out of scope because that branch is
  load-bearing.** It is not a dead catch-all; it is how the entire ephemeral
  lane is decoded. The full server-to-client `message_type` inventory is five
  values, established by grepping every `message_type=` assignment under
  `jarvis/` (`grep -rn "message_type=" jarvis/`, four hits, plus the three
  `MESSAGE_TYPE: Final` constants in `inherent_hub.py:92-94`):
  `view.delta` (`jarvis/runtime/inherent_view_sequencer.py:376`),
  `snapshot.begin` / `snapshot.page` / `snapshot.end`
  (`jarvis/surface/inherent_presenter.py:367`, `:275`, `:378`, all through the
  hub's `_protocol_frame`), `ephemeral`
  (`jarvis/runtime/inherent_hub.py:383`), `server.resync_required` (`:551`),
  and `server.hello` (`jarvis/surface/inherent_server.py:497`).
  Of those, `view.delta` and the three snapshot types have explicit cases;
  `server.hello` never reaches the frame router at all — it is consumed and
  validated by the handshake (`RealtimeTransportV2.swift:89-101`). That leaves
  exactly one legitimate frame type on `default:` — `message_type:
  "ephemeral"` — and it carries all seven D10 ephemeral kinds, discriminated by
  `payload.kind` rather than by message type: `input.state`, `input.partial`,
  `playback.progress`, `action.progress_hint`, `connection.notice`,
  `ephemeral.clear`, `ephemeral.baseline`. Replacing `default:` with an
  explicit `case "ephemeral":` plus a new unknown-message outcome is therefore
  a change to how live ephemeral traffic is routed, and it needs a rule this
  ADR does not yet have: D6
  (`docs/adr/0014-inherent-realtime-ux.md:457-463`) rules on unknown
  *ephemeral* messages and unknown *durable* mutations, and is silent on
  unknown *protocol* frames. That is a separate card with its own D6 ruling,
  not a rider on this one.
- **Named follow-up: harden the Swift `default:` branch.** An unknown
  `message_type` should surface as its own unknown-message outcome instead of a
  `protocol_error` manufactured by a failed `EphemeralUpdate` decode. Two facts
  that card needs, so nobody re-derives them: (a) it requires an ADR-0014 D6
  ruling for unknown *protocol* frames — D6
  (`docs/adr/0014-inherent-realtime-ux.md:457-463`) today rules only on unknown
  ephemeral messages and unknown durable mutations and is silent on protocol
  frames, so there is no rule to implement against yet; (b) `case "ephemeral":`
  has to be made explicit first, because the whole ephemeral lane rides the
  `default:` branch today (one `message_type`, seven `payload.kind` values) and
  would otherwise be re-routed into the new unknown outcome.
- Non-goal: mapping the reason onto a Swift enum, or adding a new
  `InherentClientEvent` case.

## Rejected approaches
- **Add `kind` to the server payload so the existing Swift ephemeral decoder
  accepts it.** Rejected under clause 3 of the decision procedure. The
  specification (ADR-0014 D11 rule 3,
  `docs/adr/0014-inherent-realtime-ux.md:802-803`) names the frame and its
  `reason` payload but never mentions `kind`; `docs/spec.html` does not mention
  the frame at all (`grep -n "resync_required" docs/spec.html` → no output). So
  the siblings decide, and they are unanimous: `kind` in this protocol exists
  only as the union discriminator inside `view.delta` durable mutations and
  inside `ephemeral` payloads (the two D10 catalog tables,
  `docs/adr/0014-inherent-realtime-ux.md:644-700`). Every other
  `delivery_class="protocol"` frame — `server.hello`, `snapshot.begin`,
  `snapshot.page`, `snapshot.end` — carries no `kind` and is decoded in Swift by
  `message_type` into a dedicated payload type
  (`RealtimeTransport.swift:250-270`). The closest sibling is `server.hello`:
  built through the same `_protocol_frame` helper, `kind`-free, decoded into
  `ServerHelloPayload`. Adding `kind` to one protocol frame would make it the
  only one, and would move the discriminator into a family where it has no
  union to discriminate.
- **Read the reason from the WebSocket close frame instead of the notice.**
  The close does carry it (`(1008, "client_backpressure")`), and `TransportInput`
  already has `.closed(socketEpoch:code:reason:)`
  (`RealtimeTransport.swift:16`). Rejected: it leaves the notice still decoding
  as a hard `protocol_error` that reaches the caller *first*, so the corrected
  reason would arrive behind a wrong one. It also makes the client depend on a
  close reason string surviving the WebSocket stack, when the server sends the
  notice precisely so the client learns before the socket dies.

## Acceptance evidence
Establish both baselines **before the first edit** and report them, then report
baseline + k. Do not trust any number written here.

- Baseline (before any edit): `bash scripts/test_inherent_swift.sh` — record the
  pass total from its printed tail. `PYTHONPATH=. .venv/bin/python -m pytest -q
  -m "not live_llm and not live_codex"` — record its pass/deselect counts.
- Positive, Swift, one test, one distinct behavior: driving the transport with a
  `server.resync_required` frame whose payload is `{"reason":
  "client_backpressure"}`, then one whose payload is `{"reason":
  "frame_over_budget"}`, then one with no `reason` key. **Observable:** the
  `InherentClientEvent` values the transport hands its sink — expected
  `.socketFailed(socketEpoch: N, reason: "client_backpressure")`,
  `.socketFailed(socketEpoch: N, reason: "frame_over_budget")`,
  `.socketFailed(socketEpoch: N, reason: "protocol_error")`. Two different
  reasons arrive distinguishably; neither is `protocol_error`; the malformed one
  still is. The three input frames are JSON literals whose payload objects match
  the Python assertion below byte for byte.
- Positive, Python, no new test: extend the two existing wire tests in
  `tests/integration/test_inherent_flow_control.py` (`:136-140` and `:288-292`)
  with an exact-payload assertion. **Observable:** the parsed JSON of the frame
  the server put on the socket — `notice[0]["payload"] == {"reason":
  "client_backpressure"}` and `== {"reason": "frame_over_budget"}`, i.e. the key
  set is exactly `{"reason"}` and no `kind` appeared. That is the first of the
  two server-side observables; the second is the close pair, and the assertions
  `(1008, "client_backpressure")` / `(1008, "frame_over_budget")` already in
  those tests stay untouched and supply it. Both must appear in the run output,
  so the transcript shows the reason on the wire in both places it is stated.
- Regression, Swift: `test_anUndecodableFrameFailsTheSocket`
  (`RealtimeTransportTests.swift:242-263`) still passes with its three
  `.socketFailed(reason: "protocol_error")` events unchanged — a truncated
  frame, a wrong `protocol_version`, and an unknown durable `kind` must not be
  softened by this change. Full suite: `bash scripts/test_inherent_swift.sh`
  ends at baseline + k.
- Regression, Python: `PYTHONPATH=. .venv/bin/python -m pytest -q -m "not
  live_llm and not live_codex"` ends at or above the recorded baseline.
- Gates: `lint-imports`, `ruff check .`, `mypy --strict jarvis tests scripts
  tools` each exit 0.
- Live run: **not required.** Nothing here depends on an LLM, on audio, or on a
  real device — the change is a frame-decode contract, and both sides of the
  wire are observable hermetically: the server's exact bytes in the integration
  test, the client's emitted events in the Swift test. No audio-output route is
  touched, so the audio save/restore rule does not apply to this card.

## Docs to sync
- `docs/adr/0014-inherent-realtime-ux.md` — **none.** D11 rule 3 at `:802-803`
  already states both reasons on this frame with the same close code, and that
  server behavior is unchanged by this card. `grep -n
  "resync_required\|frame_over_budget\|client_backpressure"
  docs/adr/0014-inherent-realtime-ux.md` returns only `:802` and `:803`.
- `docs/spec.html` — **none.** `grep -n "resync_required" docs/spec.html`
  returns nothing; the spec does not own this frame.
- The implementation session still runs both greps and states the judgement in
  the transcript rather than assuming it.

## Open questions

## /goal condition
Implement docs/goals/swift-resync-reason.md on the current branch. Read it fully before touching code. The goal is met when all of the following appear in the transcript:

(1) Before the first edit, the raw tail of `bash scripts/test_inherent_swift.sh` and the raw summary line of `PYTHONPATH=. .venv/bin/python -m pytest -q -m "not live_llm and not live_codex"` are shown as pre-change baselines, and both numbers are stated in words.

(2) The diff routes `server.resync_required` by `message_type` in desktop/inherent-swift/InherentRealtime/RealtimeTransport.swift into a payload type carrying `reason`, and the transport emits `.socketFailed(socketEpoch:reason:)` with the server's `reason` string passed through verbatim; the reason stays a Swift `String` and is not mapped onto `ResyncReason` or any other enum; no new `InherentClientEvent` case is added; and no file under `jarvis/` changes the bytes the server sends.

(3) Raw Swift test output is shown ending in a pass line whose total equals the stated baseline plus the stated number of added tests, and it includes a case proving that a `server.resync_required` frame with payload `{"reason": "client_backpressure"}` and then one with payload `{"reason": "frame_over_budget"}` produce, at the transport's event sink, `.socketFailed` with reason `client_backpressure` and then `.socketFailed` with reason `frame_over_budget` — two different values, neither of them `protocol_error` — while a `server.resync_required` frame with no `reason` key still produces `.socketFailed` with reason `protocol_error`.

(4) Regression is shown: the raw Swift output includes `test_anUndecodableFrameFailsTheSocket` still passing with its three `protocol_error` events unchanged, and raw output of `PYTHONPATH=. .venv/bin/python -m pytest -q -m "not live_llm and not live_codex"` ends in a pass line at or above the stated Python baseline.

(5) Raw pytest output for tests/integration/test_inherent_flow_control.py is shown, including the added assertion that the `server.resync_required` frame's `payload` equals exactly `{"reason": "client_backpressure"}` in one test and exactly `{"reason": "frame_over_budget"}` in the other — no `kind` key — with the existing close assertions `(1008, "client_backpressure")` and `(1008, "frame_over_budget")` still passing.

(6) Raw output of `lint-imports`, `ruff check .`, and `mypy --strict jarvis tests scripts tools`, each exiting 0.

(7) An explicit statement that no live run is required, with the reason, and therefore that no audio output route was changed or restored.

(8) Every entry under Docs to sync is updated or explicitly judged unchanged, following the rule: when the change alters a documented contract, invariant, ownership boundary, or externally relevant behavior, update the canonical document that owns that fact; do not document what the code already makes clear; do not duplicate a fact across documents. For this card that means the transcript shows the output of `grep -n "resync_required" docs/spec.html` and of `grep -n "resync_required" docs/adr/0014-inherent-realtime-ux.md`, and states that ADR-0014 D11 rule 3 already specifies this frame's shape so no documentation edit is made.

(9) Each slice is committed with the project commit skill and `git status` shows a clean tree.

(10) One Progress line per slice is appended to the card.

If the repository contradicts the card, stop and report instead of redesigning. Or stop after 30 turns.

## Progress
- Swift decode path — e1327be — `server.resync_required` routed by
  `message_type` into `ResyncRequired`, reason passed to `.socketFailed`
  verbatim; `test_aResyncRequiredFrameCarriesTheServersReason` passed
  (`client_backpressure`, `frame_over_budget`, then `protocol_error` for the
  reason-less frame), `test_anUndecodableFrameFailsTheSocket` still passed;
  Swift 178/178, 0 failures, baseline 177 + 1 added test.
- Server payload pinned — f0be959 — both wire tests now compare the whole
  parsed payload against `{"reason": "client_backpressure"}` and `{"reason":
  "frame_over_budget"}`, so no `kind` key can appear unnoticed; the
  `(1008, ...)` close assertions untouched. 13/13 in
  test_inherent_flow_control.py; full suite 1040 passed, 64 deselected
  (baseline 1040); lint-imports KEPT (1/1), ruff clean, mypy strict clean
  (244 files). No live run: the change is a frame-decode contract with both
  sides observable hermetically, so no audio route was touched.
- Docs to sync — judged unchanged. `grep -n "resync_required" docs/spec.html`
  exits 1 with no output; the same grep over
  `docs/adr/0014-inherent-realtime-ux.md` returns only `:802` and `:803`,
  where D11 rule 3 already specifies this frame and both reasons. The server
  is unchanged byte for byte, so no documentation edit is made.
- Verifier pass — opus, fresh context, range `realtime-integration..HEAD` —
  no blocking defect. It proved the Swift test non-vacuous by mutation in a
  throwaway copy of `desktop/`: with `case "server.resync_required":`
  deleted, the new test fails with three `protocol_error` events, exactly
  the old behavior. Its one low finding was a false causal claim in the
  second commit's body (a `kind` key would decode fine, not break the
  decode, since D6 ignores unknown fields); the unpushed commit was reworded
  to state the real reason, tree unchanged, which is why that Progress line
  names f0be959. Known, out of scope: `?? .venv` is a pre-existing worktree
  symlink that `.gitignore:7` misses because the pattern matches directories
  only.
