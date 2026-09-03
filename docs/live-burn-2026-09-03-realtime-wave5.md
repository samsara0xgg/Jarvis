# Live burn — 2026-09-03 (ADR-0008 Wave 5: background workers + intent pump)

Tier-2 acceptance for ADR-0008 §8 Step 4, after the wave landed on
`worktree-wave5-realtime` at `def6060`. Real cloud LLM (`deep` preset →
`gpt-5.5` through the OpenRouter proxy), real on-disk Event Log, real
`drive_turn`, real Codex CLI 0.142.5 subprocess, real git working tree, real
`_start_intent_pump`.

Every burn ran with **the Wave-4 and Wave-5 switches ON**, which is the
configuration the hermetic suite can only approximate — the shipped
`config/jarvis.yaml` keeps `realtime.actions.action_runner`,
`realtime.actions.true_async_workers` and `realtime.input.intent_pump` at
`false`, and the burn builds a temporary overlay rather than flipping the
committed default.

## Invocation

```
set -a; source ~/.jarvis/env; set +a
pytest tests/scenarios/test_live_wave5_realtime.py --live-llm --live-codex
```

Trace: `~/.jarvis/realtime-run/wave5-trace.jsonl` (via
`JARVIS_REALTIME_TRACE_JSONL`, the production seam — 70 points across the
three burns).
Measurements: `~/.jarvis/realtime-run/wave5-burn.json`, written by the tests
themselves so this document quotes measured values rather than impressions.

## Numbers

| Burn | What ran | Wall | Result |
|---|---|---:|---|
| 1 background + second utterance | real Codex `spawn_worker`, second utterance answered mid-flight | 106.8 s | **PASS** |
| 2 cancel a live process | real `codex app-server` reaped, repo freed, stash restored | 4.8 s | **PASS** |
| 3 crash after claim | durable claim abandoned, real pump restarted | 12.1 s | **PASS** |

### Burn 1 — the long action does not block the next utterance

| Metric | Value |
|---|---:|
| worker turn total | 106 747 ms |
| worker `action.running` → cleanup (repo held) | 103 084 ms |
| **second utterance → first output, worker alive** | **6 040 ms** |
| second turn total | 6 044 ms |
| worker still un-quiesced through the second turn | `true` |
| worker decide iterations / second-turn iterations | 2 / 1 |
| worker canonical terminal | `action.result_observed` |
| `worker.quiesced` rows for the worker action | 1 |
| cleanup `verification_outcome` | `verified` |

The whole row is in the ratio: the repository stayed leased for 103.1 s while
a second real turn opened, called the provider and emitted an 87-character
answer in 6.0 s. On the Wave-4 build the second turn could not have started —
`dispatch` did not return until the worker finished.

"First output" is the `cost.recorded` timestamp: this route is non-streaming
(token streaming is ADR-0008 Step 8), so the first model output *is* the
provider call returning. Reporting a fabricated first-token number would be
dishonest about what this build measures.

### Burn 2 — cancel really reaps the process, and gives the tree back

| Metric | Value |
|---|---:|
| **cancel request → OS process gone** | **1 232 ms** |
| cancel request → canonical terminal | 1 270 ms |
| terminal → `action.cleanup_completed` | 74 ms |
| cancel request → turn unwound | 1 344 ms |
| canonical terminals written | **1** (`action.cancelled`) |
| `worker.quiesced` rows | 1 |
| pre-task stash restored | **`true`** |
| live lease scopes after cleanup | **0** |
| cleanup `verification_outcome` | `verification_skipped` |
| Codex pid reaped | 60097 |

"Process gone" is measured by polling `os.kill(pid, 0)` until
`ProcessLookupError`, so it is the *reap* moment, not the `terminate()` call —
`CodexAppServerClient.close()` does `terminate()` then `wait()`, and between
the two the pid is a zombie that still answers signal 0. The number above is
therefore the conservative one.

The spoken answer was `任务已停止，未完成` — the turn told the truth about
having been stopped rather than reporting a result it did not have.

### Burn 3 — a crash after the claim recovers exactly once

| Metric | Value |
|---|---:|
| **restart → recovery (claim adopted)** | **12 ms** |
| restart → real answer emitted | 4 018 ms |
| historical intents present before adoption | 2 |
| **historical intents replayed** | **0** |
| `turn.started` rows for the crashed turn | **1** |
| `consumer.adopted` rows | 1 (`adoption_row_id = 2`) |
| `surface.response_emitted` rows | 1 |

The claim was minted, the consumer dropped without doing any work, and the
real `_start_intent_pump` was started against the same Event Log with no
stubbing at all — the recovered turn went through production
`_intent_worker` → `_drive_turn_in_worker_thread` → the real LLM. It re-used
the existing claim rather than minting a second `turn.started`, and the two
pre-adoption utterances stayed history.

## What the first run found

The same three burns were run at `ece1d4c`, before the fix in `def6060`.
Burns 1 and 3 passed; **burn 2 failed**:

```
>       assert dirty.exists(), "the finalizer did not restore the pre-task stash"
E       AssertionError: the finalizer did not restore the pre-task stash
```

Cancelling a real Codex worker left the user's uncommitted changes shelved in
a git stash that nothing would ever pop. `_pop_pending_stashes` walked
`worker.reported` / `action.failed` / `action.timeout_assumed` only, and once
`spawn_worker` runs in the background the ActionRunner — not the handler —
writes the cancel terminal, so `action.cancelled` is the sole durable row
naming that run's stash. Adding the type alone was not enough: restoring also
needs the `task_id` that resolves the repository and the `run_id` that keys the
conflict artifact, and the runner's correlation is the canonical
`{action_id, run_id?, turn_id?}` triple with neither. `def6060` routes both ids
onto the terminal beside the ref. `stash_restored = true` above is that fix
measured live.

First-run numbers, for variance:

| Metric | Burn 1 | Burn 3 |
|---|---:|---:|
| worker turn total | 128 440 ms | — |
| repo held `action.running` → cleanup | 122 996 ms | — |
| second utterance → first output | 2 873 ms | — |
| restart → recovery | — | 12 ms |
| restart → answer | — | 3 362 ms |

Provider latency varies by more than 2× between runs for the same
one-sentence question (2.9 s vs 6.0 s); the lifecycle assertions do not, and
`restart → recovery` was 12 ms both times because it touches no provider.

## What this burn does not cover

**The intent pump cannot drive a background `spawn_worker` turn today.**
`_DEFAULT_TRIGGER_TIMEOUT_S` is 5.0 s (`jarvis/runtime/__init__.py:191`),
`drive_turn` re-raises `TriggerWaitTimeout` when the in-turn wait expires
(`jarvis/runtime/__init__.py:1998-2005`), and the pump's only turn entry point
— `_drive_turn_in_worker_thread` (`jarvis/runtime/inherent_loop.py:485-517`) —
calls `drive_turn` with no `trigger_timeout_s`, so it gets the 5 s default. On
the Wave-4 build a long Codex run blocked *inside* `dispatch` and never
entered that bounded wait; with `true_async_workers` on, dispatch returns an
ack immediately and the driver waits for `worker.reported` — which arrived at
103 s in burn 1. Through the pump that turn would have raised at 5 s and been
recorded as `turn.failed` while the worker kept running.

Burn 1 therefore drives its two turns through `drive_turn` directly with an
explicit `trigger_timeout_s=900.0`, the same shape
`_drive_turn_in_worker_thread` uses. It proves the runner/finalizer half of
the row live; it does **not** prove the daemon-driven path. Burn 3 is the one
that exercises the real pump, and it does so on a conversational turn with no
background action. Choosing the trigger budget is a design call — a 900 s wait
pins one of `max_concurrent_turns: 2` slots for fifteen minutes — so it is
recorded here rather than guessed at. Both switches ship `false`, so nothing
in today's daemon is affected.

**Live-action release still happens on the driver's thread.** The Step-4 row
says to move "quiescence/verify/stash/live-action cleanup and quarantine
release" to the owned finalizer. The lease, stash and verify halves moved;
`release_turn_actions(effective_turn_id)` is still the last statement of
`drive_turn`'s `finally` (`jarvis/runtime/__init__.py:2192`). A background
worker therefore leaves the L4 live set the instant its turn returns, and
L6's supervisor sweep reads `live_action_ids()` precisely to know what it may
not touch. Moving it collides with `tests/canary/
test_canary_sweep_skips_active_turn.py:134`, which pins ADR-0009 D4's rule
that *every* release site sit inside a `finally:`; resolving that is an
ADR-level decision. No burn here exercises the sweep, so this is a reasoned
gap, not a measured failure.

## Verdict

**PASS on three of the five Step-4 acceptance properties, live.**

- **long fake action + second utterance** — burn 1: 103.1 s of held
  repository, a second real answer in 6.0 s inside that window.
- **cancel/timeout live-process cleanup** — burn 2: the real `codex
  app-server` reaped in 1.2 s, exactly one canonical terminal, lease released,
  quarantine clear, and the pre-task stash restored.
- **crash after claim** and **historical inputs not replayed** and **no double
  dispatch** — burn 3: adopted once in 12 ms, 2 historical intents untouched,
  one `turn.started`.

One P1 was found by this burn and fixed (`def6060`). One P0 (the 5 s trigger
budget) and one P1 (live-action release ownership) remain open, both recorded
above with the reasoning for leaving them to a decision rather than a guess.
Neither affects the shipped default, because all three switches are `false`.
