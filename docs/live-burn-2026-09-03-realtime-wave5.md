# Live burn — 2026-09-03 (ADR-0008 Wave 5: background workers + intent pump)

Tier-2 acceptance for ADR-0008 §8 Step 4. First run at `ece1d4c`, second at
`def6060`, and re-burned at **`62aa743`** after the fix cycle closed the two
findings the second run left open — the numbers below are the `62aa743` run.
Real cloud LLM (`deep` preset →
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
`JARVIS_REALTIME_TRACE_JSONL`, the production seam — 70 points per run, appended
across runs; the `62aa743` run is the last 70).
Measurements: `~/.jarvis/realtime-run/wave5-burn.json`, written by the tests
themselves so this document quotes measured values rather than impressions.

## Numbers

| Burn | What ran | Wall | Result |
|---|---|---:|---|
| 1 background + second utterance | real Codex `spawn_worker`, second utterance answered mid-flight | 116.0 s | **PASS** |
| 2 cancel a live process | real `codex app-server` reaped, repo freed, stash restored | 8.0 s | **PASS** |
| 3 crash after claim | durable claim abandoned, real pump restarted | 11.1 s | **PASS** |

Suite wall clock 135.3 s (`~/.jarvis/realtime-run/wave5c-burn.txt`), gates at
the same HEAD 399 passed / 63 deselected in 33.1 s
(`~/.jarvis/realtime-run/wave5c-gates.txt`).

### Burn 1 — the long action does not block the next utterance

| Metric | Value |
|---|---:|
| worker turn total | 115 949 ms |
| worker `action.running` → cleanup (repo held) | 109 924 ms |
| **second utterance → first output, worker alive** | **3 709 ms** |
| second turn total | 3 711 ms |
| worker still un-quiesced through the second turn | `true` |
| worker decide iterations / second-turn iterations | 2 / 1 |
| worker canonical terminal | `action.result_observed` |
| `worker.quiesced` rows for the worker action | 1 |
| cleanup `verification_outcome` | `verified` |
| resolved in-turn wait budget | **900.0 s, from production** |
| actual in-turn wait for `worker.reported` | 96 663 ms |

The whole row is in the ratio: the repository stayed leased for 109.9 s while
a second real turn opened, called the provider and emitted a 76-character
answer in 3.7 s. On the Wave-4 build the second turn could not have started —
`dispatch` did not return until the worker finished.

**This burn now runs on the production timeout path.** Earlier runs passed
`trigger_timeout_s=900.0` explicitly, a value that existed nowhere in
`jarvis/`; that constant and both of its uses are deleted, and the burn calls
`drive_turn` with exactly the arguments `_drive_turn_in_worker_thread` uses.
The 900.0 in the table above was resolved by `jarvis.runtime`
`_trigger_wait_budget` from the runner's `realtime.actions.lease_timeout_s`,
read out of the trace point `action_wait_started` — the same trace shows the
second, conversational turn (`T-w5-bg-chat`) emitting no wait point at all, so
the 5 s default it would have taken is untouched for ordinary turns.

"First output" is the `cost.recorded` timestamp: this route is non-streaming
(token streaming is ADR-0008 Step 8), so the first model output *is* the
provider call returning. Reporting a fabricated first-token number would be
dishonest about what this build measures.

### Burn 2 — cancel really reaps the process, and gives the tree back

| Metric | Value |
|---|---:|
| **cancel request → OS process gone** | **694 ms** |
| cancel request → canonical terminal | 719 ms |
| terminal → `action.cleanup_completed` | 90 ms |
| cancel request → turn unwound | 809 ms |
| canonical terminals written | **1** (`action.cancelled`) |
| `worker.quiesced` rows | 1 |
| pre-task stash restored | **`true`** |
| live lease scopes after cleanup | **0** |
| cleanup `verification_outcome` | `verification_skipped` |
| Codex pid reaped | 66016 |

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
| restart → real answer emitted | 3 050 ms |
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

The same three burns were first run at `ece1d4c`, before the fix in `def6060`.
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

All three runs, for variance:

| Metric | `ece1d4c` | `def6060` | `62aa743` |
|---|---:|---:|---:|
| worker turn total | 128 440 ms | 106 747 ms | 115 949 ms |
| repo held `action.running` → cleanup | 122 996 ms | 103 084 ms | 109 924 ms |
| second utterance → first output | 2 873 ms | 6 040 ms | 3 709 ms |
| cancel → OS process gone | FAIL | 1 232 ms | 694 ms |
| restart → recovery | 12 ms | 12 ms | 12 ms |
| restart → answer | 3 362 ms | 4 018 ms | 3 050 ms |

Provider latency varies by more than 2× between runs for the same
one-sentence question (2.9 / 6.0 / 3.7 s); the lifecycle assertions do not,
and `restart → recovery` was 12 ms all three times because it touches no
provider.

## What the fix cycle closed

Both gaps the `def6060` run recorded here as open are fixed, and this burn is
the evidence for the first of them.

**The intent pump can now drive a background `spawn_worker` turn.**
`_DEFAULT_TRIGGER_TIMEOUT_S` is still 5 s, but it is now the *conversational*
budget rather than the only one. `jarvis.runtime._trigger_wait_budget` resolves
each in-turn wait: an explicit caller value wins; otherwise, while the
ActionRunner still owns one of this turn's actions, the budget is that runner's
`realtime.actions.lease_timeout_s`; otherwise the 5 s default. That is one
timeout authority — a turn's wait can neither expire before the action it waits
on nor outlive it — and an ordinary turn still cannot pin one of
`max_concurrent_turns` for a quarter of an hour. Burn 1 above resolved 900.0 s
with no override anywhere and waited 96.7 s for `worker.reported`; the
conversational turn beside it never entered a wait at all.

**Live-action release is unchanged, and L6 no longer needs it to move.**
`release_turn_actions(effective_turn_id)` stays the last statement of
`drive_turn`'s `finally`, where ADR-0009 D4 requires it, so
`tests/canary/test_canary_sweep_skips_active_turn.py` is untouched and green.
What changed is what "the L4-owned live set" means: `live_action_ids` now
answers for every action L4 owns, unioning the per-turn table with a second
claim the dispatch path publishes per action and drops through the runner's
`on_finished` hook once the job — cleanup included — is done. The supervisor
sweep's call site is byte-identical and now spares a background worker still
in flight; the system-trigger watcher holds such a row for a later pass
instead of skipping it, because only the live-turn half of the union implies
somebody will consume it.

No burn here exercises the sweep — that property is pinned hermetically by
`test_the_sweep_spares_a_worker_the_runner_still_owns`, which closes a genuine
orphan in the same pass so it cannot pass by doing nothing.

## Verdict

**PASS on three of the five Step-4 acceptance properties, live.**

- **long fake action + second utterance** — burn 1: 109.9 s of held
  repository, a second real answer in 3.7 s inside that window, on a wait
  budget production resolved rather than the test.
- **cancel/timeout live-process cleanup** — burn 2: the real `codex
  app-server` reaped in 0.7 s, exactly one canonical terminal, lease released,
  quarantine clear, and the pre-task stash restored.
- **crash after claim** and **historical inputs not replayed** and **no double
  dispatch** — burn 3: adopted once in 12 ms, 2 historical intents untouched,
  one `turn.started`.

One P1 was found by the first burn and fixed (`def6060`: the cancelled run's
pre-task stash). The P0 (trigger budget) and the remaining P1 (liveness
ownership) that the second burn left open are both fixed at `62aa743`, and
burn 1 above is the first run of this suite with no test-only timeout in it.
None of this affects the shipped default, because all three switches are
`false`.
