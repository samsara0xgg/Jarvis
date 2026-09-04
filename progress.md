# Realtime completion checkpoint

Worktree: /Users/alllllenshi/.codex/worktrees/9746/jarvis
Branch: codex/realtime-completion
Input-capacity checkpoint: d2e3e631c17c467ee2ddf5b89950836149813b5e
Git HEAD identifies the complete current revision.
Baseline: 86958ad2aa6c193743fb78d1181a6dc2ccfbcf30
Verified ancestor code: 3a6856a6bb4aa7d7a612c4a98d963513a6f93eab
Contract: GOAL.md; architecture: docs/spec.html and docs/adr.

## Current work

Implemented resumable action waits: the intent pump releases both configured
worker slots while L4 runs; readiness returns the same response/turn through a
fresh per-step SQLite connection. Existing serial callers remain serial.
Checkpoint ownership survives transient poll/connection errors; shutdown waits
for an in-flight step and requests cleanup only after it settles. L4 retains
physical-resource cleanup ownership. No action threads or pool limits enlarged.

New integration: tests/integration/test_realtime_waiting_turns.py. Real pump,
driver, Event Log and ActionRunner; only L3 decision/tool body are synthetic.
Scenarios: two blocked actions plus third answer, same-ID exactly-once resume,
response cancellation, timeout, shutdown, transient poll/connection failures,
and shutdown while a decision step is still on its OS thread.

Current code self-check: 449 passed / 63 deselected in 29.11 s; seven new
barrier scenarios passed. Ruff clean, strict mypy (179 files), six-layer contract
(72 files / 200 dependencies), and git diff --check passed.
Evidence log: /tmp/jarvis-realtime-completion-nonlive.log.
Interpreter: /Users/alllllenshi/Projects/jarvis/.venv/bin/python; explicit
PYTHONPATH points here; temporary .venv link removed after checks.

Next: isolated live provider/runtime acceptance of input capacity, then safe
streaming/foreground arbitration/result recovery and Inherent v2. This checkpoint
is only in-process continuation: foreground delivery and durable restart
continuation still need work. All overall Done conditions remain open.

Swift read-only inventory: canonical /Users/alllllenshi/Projects/jarvis-codex
HEAD 65c39c79c35d30e75b777284696cb824ca3845fa, tracked desktop/inherent-swift
subtree 66e248c88873313f28760ee15820048817b0d6f8 (25 files); last subtree edit
5fa923c242fff587f532553cf829adf0443214da. jarvis-cc matches. Legacy has six
uncommitted resize changes; do not copy those files. Preserve/reimplement its
300–900 persisted left-edge width, fixed right edge and double-click reset
behavior when migrating. Xcode 26.6, Swift 6.3.3, XcodeGen 2.45.3 available.
Baseline Swift test host currently autolaunches app/controller; isolate before
running. Swift sources imported; original hashes and honest baseline/test-host limitations
are in desktop/inherent-swift/UPSTREAM_BASELINE.md.

## Evidence and boundaries

Historical non-live: 442 passed / 63 deselected, static gates passed; not current
revision acceptance. Historical live failure and exact endpoint pause:
/tmp/jarvis-takeover-live-22e363a-r4/acceptance.md.
No production DB, profile, defaults, deployment or push.
One current-revision live provider probe made (zero retries):
/tmp/jarvis-realtime-provider-probe-d2e3e63/request.json and result.json.
Endpoint https://openrouter.icu/v1/chat/completions, model gpt-5.5; only a
synthetic Chinese question about melting ice, no tools/history/profile.
APIConnectionError caused by DNS ConnectError errno 8, wall 437.56 ms; no
response/audio/charge evidence. Direct DNS check: openrouter.icu fails while
api.openai.com resolves. Hub has requested corrected target/credential mapping
from Allen; latest explicit live authorization exists, no current approval denial.
Do not send the proxy credential to a guessed host.

Swift canonical clean archive baseline is under
/tmp/jarvis-inherent-baseline-65c39c7/desktop/inherent-swift.
First build compiled but no tests started; overridden bundle ID conflicted with
hardcoded Info.plist ID. Stopped only that test host/xcodebuild (not production
PID 5126). Log xcodebuild.log, partial baseline.xcresult; not a passed baseline.
Second xcodebuild test with generated Info.plist also stalled and was interrupted.
Log: /tmp/jarvis-inherent-baseline-65c39c7/xcodebuild-isolated.log. Exact archive
XCTest bundle then ran directly with its compiled dylib search path: 82 tests,
zero failures, 10.943 s. Evidence direct-xctest-r2.log. This does not pass the
xcodebuild host-launch or UI/live v2 gates. Source baseline is now imported. Repository-owned runner:
`bash scripts/test_inherent_swift.sh`; uses build-for-testing then direct XCTest
without invoking app @main. Imported/normalized source passed 82 tests in
10.965 s; evidence desktop/inherent-swift/build/xctest.log. One fixture keeps
its two runtime Markdown spaces via Unicode escapes (upstream bytes listed
separately in manifest). Python import checkpoint: 449 passed / 63 deselected,
29.27 s; /tmp/jarvis-realtime-swift-import-nonlive.log. Static gates passed.
Next Swift work: test-host/launcher isolation and invalidation, then typed v2
transport/reducer; legacy resize behavior still needs reimplementation.

L3 streaming orientation: complete SituationPacket is first available in decide
before _handle_utterance. Prepare route after confirmation/Tier0 branches and
before _run_tool_use_loop model call. Current ResponseRun opens earlier with
immutable full_text policy; move preparation before policy minting instead of
loosening it later. Missing typed async tool/usage/error/cancel stream,
ResponseRiskContext and actual packet snapshot hash, permit classifier/gate,
syntax-aware assembler and immutable-prefix finalizer. Keep L3 cost/admission
fences; no full-response retry or renderer replay after first permit. Details
in ADR-0008 D2–D5; no new code for streaming yet.
Latest hub message authorizes goal-required live tests with synthetic payloads.
Inspect exact endpoint/payload and prior refusal evidence before resuming; do not
carry real history, profile or production state to providers.
Physical microphone/playback, quality and latency acceptance still outstanding.
