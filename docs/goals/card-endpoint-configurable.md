# Goal: inherent-endpoint-override

## Goal
The InherentCard's daemon endpoint becomes settable from the environment, defaulting byte-identically to today's `127.0.0.1:8006`, so pointing the card at a second daemon never again requires editing Swift source.

## Why
Tonight the owner needed the card to talk to a second daemon on port 8009. There is no override of any kind, so he edited the Swift constants. Those edits were later reverted; his running card still carries the 8009 change **only inside an already-compiled binary**, because `desktop/inherent-swift/launcher.py:34-35` (`ensure_app_built`: `if APP_BIN.exists(): return`) builds only when the `.app` is missing. A one-variable configuration change became a source edit, then a lost source edit, then a binary nobody dares rebuild.

The asymmetry is the tell: the daemon side has been configurable all along — `jarvis/cli/__init__.py:672` (`parser.add_argument("--port", type=int, default=8006)`). Only the client hardcodes it.

## Current behavior
- Five endpoint literals, all sharing the authority `127.0.0.1:8006`, all `static let`, none overridable:
  - `desktop/inherent-swift/InherentCard/BridgeBackend.swift:50` — `BridgeBackend.WS_URL = ws://127.0.0.1:8006/inherent/ws`
  - `desktop/inherent-swift/InherentCard/BridgeBackend.swift:252` — `SubmitRequest.endpoint = http://.../inherent/submit`
  - `desktop/inherent-swift/InherentCard/BridgeBackend.swift:271` — `ImageSubmitRequest.endpoint = http://.../inherent/image-submit`
  - `desktop/inherent-swift/InherentCard/BridgeBackend.swift:313` — `VoiceSubmitRequest.endpoint = http://.../inherent/asr-submit`
  - `desktop/inherent-swift/InherentCard/RealtimeTransportV2.swift:27` — `RealtimeTransportV2.url = ws://127.0.0.1:8006/inherent/ws/v2`
- Consumption is two-shaped. The three HTTP literals reach the wire through builders — `BridgeBackend.swift:355,369,383` call `SubmitRequest.build` / `ImageSubmitRequest.build` / `VoiceSubmitRequest.build`, whose returned `URLRequest` is what is sent. The two WebSocket literals are consumed inline: `BridgeBackend.swift:119` (`session.webSocketTask(with: BridgeBackend.WS_URL)`) and `RealtimeTransportV2.swift:147` (`URLRequest(url: RealtimeTransportV2.url)`), so no function today returns the socket request for inspection.
- The app already reads configuration from the environment, in two distinct styles:
  - debug-only, read inline, no injection point: `NativeCardController.swift:84` `INHERENT_DEBUG_FAKE_TURNS` (drives `runFakeTurns()` at `:399-400`), plus `INHERENT_DEBUG_SNAPSHOT_PATH/_DELAY_MS/_DIR/_START_MS/_INTERVAL_MS/_COUNT` at `:95-101,354-357`, and `NativeCardModel.swift:142`.
  - launcher-supplied runtime configuration, `JARVIS_INHERENT_*`, exported by `launcher.py:53-58`: `JARVIS_INHERENT_PARENT_LIFETIME` (`ParentWatchdog.swift:12-14`) and `JARVIS_INHERENT_V2_TOKEN_PATH` (`RealtimeTransportV2.swift:28,44-47`). The latter is the only one with a testable shape: a `static let ...EnvironmentKey` constant plus `static func tokenPath(environment: [String: String] = ProcessInfo.processInfo.environment)`.
- `launcher.py:52-58` builds the child environment as `{**os.environ, ...}`, so any variable already set in the owner's shell reaches the app with no launcher change.
- `InherentCardTests/SubmitRequestTests.swift:7,52,79` pins all three HTTP URLs to the `8006` literal; those assertions fail the moment the default moves.

## Target behavior
- One environment variable, `JARVIS_INHERENT_BRIDGE_PORT`, holding a decimal port. Host stays `127.0.0.1`; see Rejected approaches.
- Unset → every one of the five URLs is byte-identical to today's string, including scheme and path.
- Set to a valid port (1-65535) → all five URLs carry that port and nothing else changes.
- Malformed (non-numeric, empty, out of range) → the app keeps `8006` **and** emits one `NSLog` line tagged `[bridge]` naming the environment key and the rejected value. The fallback is announced, never silent. This matches how the codebase already handles unusable input on a path with no error channel: `ParentWatchdog.swift:16-19` refuses and NSLogs its reason rather than crashing, and `BridgeBackend.swift:230-231` NSLogs dropped frames. Throwing is not available here — `RealtimeTransportV2.loadToken` can throw only because `connect()` carries the error into `state = .failed`; a URL constant has no such consumer.
- All five sites resolve through **one** function. The literal `8006` survives in exactly one place in `InherentCard/` afterwards: that function's default.
- The resolver and every URL-producing function it feeds take `environment: [String: String] = ProcessInfo.processInfo.environment`, mirroring `RealtimeTransportV2.tokenPath(environment:)`. This is not decoration: on Darwin `ProcessInfo.processInfo.environment` is snapshotted, so a test that calls `setenv` mid-process cannot reliably drive the real code path. Injection is what makes the acceptance honest.
- The two WebSocket URLs gain a function that returns what is handed to `URLSession` — the URL for v1, the `URLRequest` for v2 — so the socket target is inspectable the way the HTTP requests already are.
- Nothing changes for anyone who sets nothing.

## Affected contracts and files
- L5 surface `desktop/inherent-swift/InherentCard/BridgeBackend.swift` — the four literals at `:50,252,271,313` route through the shared resolver; `:119` calls the new v1 socket-URL function instead of the constant.
- L5 surface `desktop/inherent-swift/InherentCard/RealtimeTransportV2.swift` — `:27` routes through the resolver; `:147` builds its `URLRequest` through a function the test can call.
- L5 surface `desktop/inherent-swift/InherentCardTests/SubmitRequestTests.swift` — the existing URL assertions become the unset-case control and gain their set-case twins. Extend this file; do not create a parallel suite.
- `desktop/inherent-swift/README.md:18` — the only prose that tells a human which port the card talks to.

## Boundaries and non-goals
- Layers that may change: L5 surface only, inside `desktop/inherent-swift/`.
- **Must not change: `launcher.py`'s missing staleness check.** `ensure_app_built` returns early whenever the `.app` exists, and that bug is currently the only thing protecting the owner's running card: it holds the sole surviving copy of his 8009 edit, which exists in no source file. Teaching the launcher to rebuild on newer sources would rebuild that app and destroy the change. This is a safety exclusion, not tidiness. It may be fixed only after this override ships and the owner has adopted it. Do not bundle it.
- **Must not touch `.claude/worktrees/realtime-live-test` in any way** — no build, no rebuild, no launch, no delete, no write, not even a "harmless" `xcodegen`. Its build output is the surviving 8009 binary. Same reason as above.
- Must not change: the default endpoint, any URL path, the `ws`/`http` schemes, `RealtimeTransportV2.enabled` (stays `false`), or any wire payload.
- Non-goals: a settings UI; a config file or plist; a command-line parser; a new configuration type; a host override; making the daemon's own port configurable (it already is); changing `launcher.py` at all — `{**os.environ, ...}` at `:53` already forwards the variable.
- Python is not touched. If the implementation finds it must be, stop and say why before editing.

## Rejected approaches
- **One variable holding a whole URL or `host:port` authority** — rejected as speculative generality. The only change ever needed was the port; the host has never moved. A host override is also not the cheap string it looks like: `InherentCard/Info.plist` declares no `NSAppTransportSecurity`, so `http://` to a non-IP-literal host is blocked by ATS at runtime while the unit-level URL assertion still passes. That is precisely the silent-failure mode R2 forbids. A port is also totally validatable (an integer in 1-65535); a host is not.
- **Two variables, host and port** — same objection, plus twice the surface to validate and document for a host nobody has changed.
- **Patching the five literals independently** — five places to keep in sync, and the next reader cannot tell whether `8006` is a default or a leftover. One resolver, one literal.
- **A settings UI, plist, or config file** — a mechanism for a value that changes once a year, in an app with no settings surface at all.
- **Following the `INHERENT_DEBUG_*` naming** — this is runtime configuration the launcher forwards, not debug instrumentation, and that family has no injection point, so it cannot be tested. `JARVIS_INHERENT_*` is the right sibling family.
- **Crashing on a malformed value** — nothing in this app crashes on bad environment input, and a card that refuses to launch because of a typo is worse than one that logs and connects to the default.
- **Reading the port from the token file or the runtime root** — invents a coupling and a file format for one integer.

## Acceptance evidence
Run only in this lane's own worktree. Every command's raw output is pasted into the transcript.

- Positive: `bash scripts/test_inherent_swift.sh` — raw tail shown, `0 failures`, executed count strictly greater than the 178 baseline. New assertions, each landing on the request or URL the production call site actually uses, called through the same function production calls with an injected environment (never a re-typed string):
  - `SubmitRequest.build` with an empty environment → `req.url?.absoluteString == "http://127.0.0.1:8006/inherent/submit"` (the control).
  - `SubmitRequest.build` with `["JARVIS_INHERENT_BRIDGE_PORT": "8009"]` → `"http://127.0.0.1:8009/inherent/submit"`.
  - The same unset/set pair for `ImageSubmitRequest.build` (`/inherent/image-submit`) and `VoiceSubmitRequest.build` (`/inherent/asr-submit`).
  - The same unset/set pair for the v1 socket URL handed to `webSocketTask` → `ws://127.0.0.1:8006/inherent/ws` and `ws://127.0.0.1:8009/inherent/ws`.
  - The same unset/set pair for the v2 connect `URLRequest`'s URL → `ws://127.0.0.1:8006/inherent/ws/v2` and `ws://127.0.0.1:8009/inherent/ws/v2`.
  - Malformed: `["JARVIS_INHERENT_BRIDGE_PORT": "abc"]` → still `http://127.0.0.1:8006/inherent/submit`, proving the fallback is defined rather than accidental. Add `"0"` or `"70000"` if the range check is implemented as stated.
- Regression: the three pre-existing assertions at `SubmitRequestTests.swift:7,52,79` still assert `8006` and still pass unmodified in substance — they are the proof that a user who sets nothing is unaffected. A suite that only proved the override would pass while breaking every existing user.
- Regression: `git grep -n "8006" desktop/inherent-swift/InherentCard/` returns exactly one line — the resolver's default. Raw output shown.
- Regression: `git grep -n "127\.0\.0\.1" desktop/inherent-swift/InherentCard/` returns only lines belonging to the shared resolver. Raw output shown.
- Python: not touched by this card, so the Python gates are not this card's evidence. If any `.py` file changes, the hermetic baseline (the lane reports it as roughly 1046 passed / 64 deselected, to be confirmed from the run's own printed counts, never inferred) must be shown green and the reason for touching Python stated.
- Live run: **not required**. A visual or screenshot check is **forbidden** — obtaining one would require building and launching the app, which is the exact operation this card exists to keep away from the owner's binary.

## Docs to sync
- `desktop/inherent-swift/README.md:18` — "The web backend (`ui/web/server.py`) must be running separately on port 8006." Add the one sentence naming `JARVIS_INHERENT_BRIDGE_PORT`, its default, and its malformed-value behavior. This is the doc a human reads before running the card, and it is the only place the fact does not already appear in code.
- `docs/adr/0003-inherent-text.md:40-42` — documents the wire contract as the literals `http://127.0.0.1:8006/inherent/submit` and `ws://127.0.0.1:8006/inherent/ws`. The default is unchanged, so the documented contract still holds. Judge it unchanged and say so; do not restate the env var here.
- `docs/adr/0014-inherent-realtime-ux.md:348-351` — owns what the launcher passes to Swift in the environment ("passes only the runtime-root/token-file path to Swift, never the token value in an environment variable"). That sentence is about the token specifically and is not contradicted. Judge unchanged unless the implementation ends up changing `launcher.py`, which it should not.
- `docs/spec.html` — **none**. Evidence: `grep -rn "8006\|127\.0\.0\.1" docs/spec.html` returns zero lines, and `grep -on "JARVIS_INHERENT[A-Z_]*\|INHERENT_DEBUG[A-Z_]*" docs/spec.html` returns zero lines. The spec does not own the card-to-daemon endpoint or any client environment key.

## Open questions
(none)

## /goal condition
Done when the transcript shows all of the following as raw, pasted command output.

1. `bash scripts/test_inherent_swift.sh` was run in this lane's own worktree and its raw tail is shown, reporting 0 failures and an executed-test count strictly greater than the 178 baseline. Both counts appear in the transcript.
2. Both cases are shown for the same code path. With no `JARVIS_INHERENT_BRIDGE_PORT` set, the transcript shows an assertion that the submit request URL is exactly `http://127.0.0.1:8006/inherent/submit`. With it set to `8009`, the transcript shows an assertion that the same builder yields `http://127.0.0.1:8009/inherent/submit`. An override-only proof is a failure.
3. The unset/set pair is shown for all five endpoints: `/inherent/submit`, `/inherent/image-submit`, `/inherent/asr-submit`, `ws://.../inherent/ws`, and `ws://.../inherent/ws/v2`. Every assertion calls the same function the production call site calls, with the environment injected as a parameter; no test re-types a URL string that production does not build.
4. A malformed value (`"abc"`) is shown to still resolve to `127.0.0.1:8006`, and the code emits an `NSLog` line naming the rejected value.
5. Regression: the three pre-existing `8006` assertions in `InherentCardTests/SubmitRequestTests.swift` are shown still asserting `8006` and still passing.
6. Regression: the raw output of `git grep -n "8006" desktop/inherent-swift/InherentCard/` is shown and contains exactly one line, the shared resolver's default.
7. `git status` is shown clean apart from untracked `.venv`, and the diff touches nothing outside `desktop/inherent-swift/` — in particular `launcher.py` is unchanged and `.claude/worktrees/realtime-live-test` was never built, launched, written, or deleted. The transcript states this explicitly.
8. No screenshot, no app launch, no `xcodebuild` outside `scripts/test_inherent_swift.sh`.
9. Documentation rule: every entry under "Docs to sync" was either updated or explicitly judged unchanged, with the judgement stated. Where the change alters a documented contract, invariant, ownership boundary, or externally relevant behavior, update the canonical document that owns that fact; do not document what the code already makes clear; do not duplicate a fact across documents.
10. Each committed slice appended a line to Progress with its sha and evidence.

Stop and report instead of redesigning if the repository contradicts this card. Or stop after 12 turns.

## Progress
- (implementation session appends here)
- Slice 1, resolver + five call sites + tests — 709d90e — `bash scripts/test_inherent_swift.sh`: `Executed 189 tests, with 0 failures (0 unexpected) in 11.046 (11.064) seconds` (178 baseline + 11 new); `git grep -n 8006 desktop/inherent-swift/InherentCard/` returns one line, `BridgeBackend.swift:56:  static let defaultPort = 8006`; `git grep -n 127\.0\.0\.1` there returns one line, the resolver's URL builder; the malformed values `abc`, ``, `0`, `70000`, `80 09` each logged `[bridge] JARVIS_INHERENT_BRIDGE_PORT=<value> is not a port in 1-65535; using 8006` and still resolved to `http://127.0.0.1:8006/inherent/submit`.
- Slice 2, docs sync — 23a67c2 — README's port sentence names `JARVIS_INHERENT_BRIDGE_PORT`; ADR-0003, ADR-0014 and spec.html judged unchanged with reasons in that commit message (`grep -rn "8006\|127\.0\.0\.1" docs/spec.html` and `grep -on "JARVIS_INHERENT[A-Z_]*\|INHERENT_DEBUG[A-Z_]*" docs/spec.html` both return zero lines).
- Slice 3, verifier round — 7798094 — the opus verifier confirmed the five endpoints share one resolver, the default is byte-identical and the three pre-existing assertions are untouched. One finding fixed: the README sentence claimed the unset case logs, which it does not. Final suite `env -u JARVIS_INHERENT_BRIDGE_PORT bash scripts/test_inherent_swift.sh`: `Executed 189 tests, with 0 failures (0 unexpected) in 11.106 (11.128) seconds`.
- Owner decision needed, not fixable inside this card: the three pre-existing assertions at `SubmitRequestTests.swift:7,52,79` call the builders without `environment:`, so they read the real process environment. Reproduced here — `JARVIS_INHERENT_BRIDGE_PORT=8009 bash scripts/test_inherent_swift.sh` gives `Executed 189 tests, with 3 failures (0 unexpected)`, those three lines. Once Allen exports the variable per the follow-up below, anyone running the Swift suite from that shell gets three reds. Both fixes are out of bounds: passing them `environment: [:]` contradicts /goal condition 5's "unmodified", and unsetting the key in `scripts/test_inherent_swift.sh` contradicts condition 7's diff scope. Left as is; the unset case is already proven deterministically by the new `environment: [:]` control tests, and these three now double as a canary that the runner's shell is clean.
- Not fixed, pre-existing and outside this card: `README.md:18` still names `ui/web/server.py` as the backend, while `/inherent/*` is served by `jarvis/surface/inherent_server.py`.
- Owner follow-up, not blocking acceptance: after this lands, Allen sets `JARVIS_INHERENT_BRIDGE_PORT=8009` in his shell, relaunches the card from a fresh build, and confirms it reaches the 8009 daemon. Only after he confirms may the `launcher.py` staleness check be revisited.

