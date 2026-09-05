# Realtime completion checkpoint

Worktree: /Users/alllllenshi/.codex/worktrees/9746/jarvis
Branch: codex/realtime-completion
Input-capacity checkpoint: d2e3e631c17c467ee2ddf5b89950836149813b5e
Git HEAD identifies the complete current revision.
Baseline: 86958ad2aa6c193743fb78d1181a6dc2ccfbcf30
Verified ancestor code: 3a6856a6bb4aa7d7a612c4a98d963513a6f93eab
Contract: GOAL.md; architecture: docs/spec.html and docs/adr.

## Current work

Incremental SemanticAssembler now preserves the exact text prefix, uses a
60-code-point speech cap, and splits only at stable sentence/subclause
boundaries. Advancing one common character prefix makes results independent
of provider chunking. Decimal/grouping punctuation, English abbreviations,
quotations, code/Markdown, XML, URLs and email local-part punctuation are
covered; unsupported syntax and unbounded fragments buffer instead of
producing arbitrary cuts. Candidates are NOT permits and emit no surface event.

Current checks: 41 boundary tables plus 31 actual SDK/SSE/SQLite scenarios
passed in 1.06 s. Two SDK scenarios prove a candidate exists before withheld
provider completion, and cancellation stops both the socket and future
candidates. Full regression: 528 passed / 63 deselected in 28.82 s, exit 0;
ruff clean, strict mypy 183 files, layers 74 files / 203 dependencies kept.
Evidence: /tmp/jarvis-realtime-semantic-assembly-nonlive-r2.log.
Prior run: 527 passed / 63 deselected in 28.96 s, before the adjacent !? email
counterexample was repaired. Evidence: same log name without -r2.

Next: complete L3 risk context and actual SituationPacket snapshot hash,
versioned candidate classifier, durable source-bound emission permits and
prefix-preserving finalizer; then connect the no-tool route to streaming TTS.
The existing ResponseRun is opened before decide with immutable full_text:
prepare the L3 route/packet before policy minting, never loosen it afterward.
Reuse the prepared packet in decide so prompts and policy hash cannot read
different snapshots. Keep confirmation/Tier0 precedence, request admission,
L3 cost ownership, and separate legacy full-text fallback before the first
permit. No whole-answer restart/replay after a committed prefix.
Ordinary safe first-sentence/TTS production streaming remains unimplemented.

## Typed transport checkpoint (aed2acf)

Typed async LLM transport is implemented in jarvis/decision/llm_stream.py
and LLMClient.stream_events: stable request identity before I/O, text/tool
DTOs, bounded argument assembly, all-proposal validation at protocol EOF,
explicit partial/unavailable usage, owning-loop cancellation that closes the
actual SDK/TCP read. CostRecorder binds one immutable settlement before I/O;
cost commit failure retries the same disposition without another request.
Same-chunk tool signals precede text; Anthropic initial usage is never promoted
to provider-final without an actual terminal usage count. The legacy path is
unchanged and the typed path is not yet selected by production routing.

Actual OpenAI/Anthropic SDKs against localhost SSE and real SQLite accounting:
29 integration scenarios, plus 8 cost canaries, passed in 1.23 s. Malformed
JSON, duplicate keys/IDs, nonfinite numbers, protocol tails/errors, mixed
tool/text, cancel before I/O and stalled socket reads, consumer cancellation,
and accounting rollback/retry are covered. This is transport evidence, not
cloud-model, safe segment, TTS or physical playback acceptance.

Typed transport self-check: 485 passed / 63 deselected, 29.03 s, exit 0;
ruff clean, strict mypy 181 files, layers 73 files / 203 dependencies kept.
Evidence: /tmp/jarvis-realtime-typed-stream-nonlive-r3.log. The first run
passed all assertions in 30.21 s but aborted at native teardown; it is NOT a
passing gate. /tmp/jarvis-realtime-typed-stream-nonlive.log and macOS report
~/Library/Logs/DiagnosticReports/python3.12-2026-09-04-172018.ips show ONNX
telemetry HTTP callback/static-destructor mutex failure. R2 exited 0 in 31.43 s
but still had the isolation defect. Commit 123dff6 patches three wake wiring
tests' eager WakeEngine.start seam; the nine checks pass with an import-audit
blocker and zero openwakeword/onnxruntime import attempts. R3 is after that fix.

## Retained checkpoints

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
loosening it later. Typed async tool/usage/error/cancel transport now exists.
Missing ResponseRiskContext and actual packet snapshot hash, permit classifier/gate,
and immutable-prefix finalizer. Semantic assembler now exists. Keep L3 cost/admission
fences; no full-response retry or renderer replay after first permit. Details
in ADR-0008 D2–D5; production streaming route remains disabled/unimplemented.
Latest hub message authorizes goal-required live tests with synthetic payloads.
Inspect exact endpoint/payload and prior refusal evidence before resuming; do not
carry real history, profile or production state to providers.
Physical microphone/playback, quality and latency acceptance still outstanding.

Hub workflow update: no periodic queries or milestone messages. Contact only
on final clean candidate after all implementation/self/live checks, or a
specific external blocker requiring a hub/user decision. Final independent
verification remains required. Current provider endpoint blocker was already
reported; await corrected endpoint/credential mapping, do not repeat the ask.
