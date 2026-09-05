# Goal: swift-reducer-core

## Goal
The Swift client has a strict-concurrency v2 state model, a pure keyed reducer with deterministic duplicate, gap, stale, and resync behavior, a single MainActor store, and a single-mailbox transport actor, all verified by the §15.3 reducer tests, with no production socket wired yet.

## Why
Every v2 UI behavior (snapshot adoption, controls, status lanes) reduces over this core; ADR-0014 D3, D4, D17, D18, D19 define it and none of it exists.

## Current behavior
- UI truth is one flat enum `NativeTurnPhase` (idle/input/submitting/streaming/done/listening/transcribing/transition/error) written directly by bridge, voice, and submit handlers (desktop/inherent-swift/InherentCard/NativeCardModel.swift:6-16 and the assignment sites listed there, e.g. :1144-1320).
- No response, group, playback-generation, turn, or action identity exists anywhere in the Swift tree; state is one in-flight question/answer pair plus a flat history array (NativeCardModel.swift:73-88, :1030-1044).
- State mutation and effects are interleaved in the same MainActor methods (NativeCardModel.swift:323-384 submitInputText; :1111-1205 siriOpen/siriAppend/siriDone), each `BridgeDispatcher` method re-entering MainActor through `Task { @MainActor in }` after a `DispatchQueue.main.async` hop in `WSClient` (BridgeBackend.swift:126, :155, :211).
- `WSClient` is a plain class with a private serial queue, no actor, no mailbox, no receive index (BridgeBackend.swift:96-247); `BridgeTurnGate` is a single boolean with no identity (BridgeBackend.swift:64-94).
- No `Sendable` conformance and no strict-concurrency build setting exist; Project.yml sets only `SWIFT_VERSION: "5.9"` (desktop/inherent-swift/Project.yml:8).
- Hiding the window discards the open turn (`ws?.discardOpenTurn()` in NativeCardController.swift:145-157 and :683-694), which D19 forbids for v2.
- Tests: `NativeCardModelTests` construct a real model on MainActor and settle with sleeps; `ReconnectBackoffTests` are pure value-type tests, the style the reducer tests should follow (InherentCardTests/ReconnectBackoffTests.swift). Runner: scripts/test_inherent_swift.sh (88 tests baseline).
- Card 1 (v2-envelope-and-identity) adds `RealtimeProtocol.swift` (Codable envelopes, hello) and a v2 transport that only completes hello; reuse its types, do not fork them.

## Target behavior
- ADR-0014 D3 (state families and the `InherentUXState` / `ResponseState` shapes), D4 (pure `InherentReducer.reduce(state:event:now:) -> [InherentEffect]`, `RealtimeTransport` actor with one receive task and one `AsyncStream` mailbox with monotonic `receiveIndex`, `RealtimeStore` as the sole `@MainActor ObservableObject` and sole mutation entry, `RealtimeEffectRunner` outside the reducer), D17 (the twelve ordering and dedup rules, gap buffer of 32, snapshot resets baselines), D18 (foreground/background selection rules), and D19 (local presentation state list; hide only flips `presentation.isVisible`) are the contract. The reducer's input vocabulary is D10's message catalog (durable `view.delta` mutations, ephemeral updates, snapshot pages) and D8's snapshot handoff; add the Codable DTOs those messages need next to card 1's envelope types so the reducer consumes decoded DTOs, not dictionaries.
- The v2 code lives in a new Swift library target `InherentRealtime` (xcodegen target in Project.yml) built with `SWIFT_STRICT_CONCURRENCY: complete` and Swift 6 language mode, depended on by `InherentCard`; v1 files and the `InherentCard` target's own settings are untouched (D32: never mix v1 and v2).
- The reducer file imports only Swift standard library and Foundation value types; no AppKit, URLSession, AVFoundation, Dispatch, Date(), or animation APIs; time arrives as the injected `ContinuousClock.Instant`.
- Effects are values; the effect runner in this card implements only `sendAck` and `requestResync` as recorded intents (no socket), plus `scheduleLocalFade` and `announceAccessibilityMilestone` as no-op recorders; later cards attach real runners.
- `RealtimeStore` applies reducer output on MainActor and exposes `InherentUXState` and `LocalPresentationState`; the transport actor awaits the store's apply before pulling the next mailbox item; DTOs, state, and effects are `Sendable`.
- Nothing in `NativeCardModel`, `NativeCardController`, or `BridgeBackend` changes behavior; the v1 path and the 88 existing tests are untouched.

## Affected contracts and files
- Swift new (names per ADR §12 hint): InherentRealtime/RealtimeState.swift, RealtimeReducer.swift, RealtimeStore.swift, RealtimeTransport.swift, RealtimeEffects.swift, and the DTO additions beside RealtimeProtocol.swift.
- desktop/inherent-swift/Project.yml — new library target with strict concurrency complete; test target coverage for it; no change to the InherentCard target settings.
- InherentCardTests (or a sibling test target for the library) — the §15.3 tests below.
- scripts/test_inherent_swift.sh — only if the runner must build the new target; keep the direct-xctest style.

## Boundaries and non-goals
- Layers that may change: Swift only (L5 physical client), Project.yml, Swift tests. No Python changes.
- Must not change: any v1 file's behavior; UPSTREAM_BASELINE.md behavior decisions (resize, hotkey, window, history, voice, shutdown watchdog); the `InherentCard` target's build settings; card 1's envelope types except additively.
- Non-goals: wiring the reducer to a real socket or to the views; snapshot/ACK/catch-up server side; control messages (stop, PTT, cancel, confirmation) and their §15.3 "Controls" tests; accessibility runners; status lanes UI; removing the v1 hide-discards-turn behavior (recorded as v1 adapter behavior only).

## Rejected approaches
- Retrofitting `NativeCardModel` into the reducer — D32 forbids mixing v1 and v2 in one reducer and §12 says the model shrinks to a v1 adapter.
- Enabling strict concurrency on the whole `InherentCard` target now — would force rewriting v1 code this card must not touch.
- Reducing over raw `[String: Any]` — the reducer must validate typed identities and revisions; dictionaries hide the invariants D17 requires.

## Acceptance evidence
- Positive (Swift, hermetic): `bash scripts/test_inherent_swift.sh` output shows the new reducer tests passing, covering every §15.3 "Identity/order" bullet (duplicate event identical state; segments 0,2,1 render only 0 then 0/1/2; gap overflow requests exactly one resync and never displays a corrupt suffix; terminal before a missing segment does not fabricate completion; late panel segment cannot alter a terminal or new response and playback generation N cannot alter N+1; boot A delta after boot B snapshot ignored; old socket epoch failure cannot alter the new connection), every "State combinations" bullet (listening plus speaking/ducked coexist; speech interruption leaves the linked action running; completed response stays completed after playback interrupt; interrupted/CAS ACK stays pending until matching-boot/generation silenced and stale silence cannot change it; TTS failure plus document completion is text-only success; commentary completion plus action running stays waiting-action; action result creates or updates the final sibling without overwriting commentary; old action stays in the background shelf during a new foreground turn), the reducer-level "Offline/restart" bullets (disconnect preserves content and draft; snapshot cursor filters all older deltas; restart never emits a speech effect; capability downgrade selects the text/voice UI state), plus D18 rules (new committed turn becomes foreground; pending confirmation pinned regardless of foreground; reconnect never changes foreground identity) and D19 (hide flips only `presentation.isVisible`). A property test feeds legal duplicates, stale playback generations, reconnect boundaries, batched mutations, and permitted permutations and asserts the final state equals the canonical snapshot state.
- Positive (transport): a test drives `RealtimeTransport` with an injected `AsyncStream` and shows frames apply in `receiveIndex` order, one at a time, with no second reader.
- Positive (purity): the transcript shows `grep -nE "import (AppKit|SwiftUI|AVFoundation|Dispatch)|URLSession|Date\(\)|DispatchQueue" desktop/inherent-swift/InherentRealtime/RealtimeReducer.swift desktop/inherent-swift/InherentRealtime/RealtimeState.swift` returning nothing, and the build log shows the library target compiled with strict concurrency complete and zero concurrency diagnostics.
- Regression: the same run shows the existing 88 tests still passing; `git diff --stat` shows no change to NativeCardModel.swift, NativeCardController.swift, BridgeBackend.swift behavior beyond wiring the new library dependency if required; Python hermetic suite and gates unchanged (`PYTHONPATH=. .venv/bin/python -m pytest -q -m "not live_llm and not live_codex"`, `lint-imports`).
- Live run: not required (no socket wiring); state so.

## Docs to sync
- docs/adr/0014-inherent-realtime-ux.md D3, D4, D17-D19 — judged unchanged unless a shape deviates; if the library-target split is adopted, add one sentence to §13 D32 recording that v2 lives in its own module.
- desktop/inherent-swift/UPSTREAM_BASELINE.md — judged unchanged (v1 preserved) or one line noting the new module.

## Open questions
(none)

## /goal condition
Implement docs/goals/swift-reducer-core.md on the current branch. The goal is met when all of the following appear in the transcript: (1) the diff adds an `InherentRealtime` library target with strict concurrency complete containing the D3 state types, the pure `InherentReducer`, `RealtimeStore`, `RealtimeTransport` actor, effect values and a recording effect runner, and DTOs for D10 durable mutations, ephemeral updates, and D8 snapshot pages, with no behavior change to v1 files; (2) raw output of `bash scripts/test_inherent_swift.sh` showing the new tests for every §15.3 Identity/order and State combinations bullet, the reducer-level Offline/restart bullets, the D18 and D19 rules, the property test, and the transport ordering test, all passing, alongside the existing 88 with 0 failures; (3) the purity grep over RealtimeReducer.swift and RealtimeState.swift printing nothing, and the build log line showing strict concurrency complete for the new target; (4) raw output of the Python hermetic suite with live tests excluded and `lint-imports` unchanged from baseline; (5) an explicit statement that no live run is required; (6) ADR-0014 D3/D4/D17-D19 and D32 and UPSTREAM_BASELINE.md each updated or explicitly judged unchanged, following the rule: update the canonical document that owns a changed contract, do not document what the code makes clear, do not duplicate a fact across documents; (7) each slice committed with the project commit skill and `git status` clean; (8) a Progress line per slice in the card. Or stop after 80 turns.

## Progress
