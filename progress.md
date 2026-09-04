# Realtime completion checkpoint

Worktree: /Users/alllllenshi/.codex/worktrees/9746/jarvis
Branch: codex/realtime-completion
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
running. Swift sources not imported yet.

## Evidence and boundaries

Historical non-live: 442 passed / 63 deselected, static gates passed; not current
revision acceptance. Historical live failure and exact endpoint pause:
/tmp/jarvis-takeover-live-22e363a-r4/acceptance.md.
No production DB, profile, defaults, deployment or push. No provider calls made.
Latest hub message authorizes goal-required live tests with synthetic payloads.
Inspect exact endpoint/payload and prior refusal evidence before resuming; do not
carry real history, profile or production state to providers.
Physical microphone/playback, quality and latency acceptance still outstanding.
