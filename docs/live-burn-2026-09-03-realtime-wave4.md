# Live burn — 2026-09-03 (ADR-0008 Wave 4: ResponseRun + ActionRunner)

Tier-2 acceptance for ADR-0008 §8 Steps 2 and 3, after both waves landed on
`worktree-wave4-realtime` at `809463b`. Real cloud LLM (`deep` preset →
`gpt-5.5` through the OpenRouter proxy), real on-disk Event Log, real
`drive_turn`, real Codex CLI 0.142.5 subprocess, real git working tree.

Every burn ran with **both Wave-4 switches ON**, which is the configuration
the hermetic suite can only approximate — the shipped `config/jarvis.yaml`
keeps `realtime.response.*` and `realtime.actions.action_runner` at `false`,
and the burn builds a temporary overlay rather than flipping the committed
default.

## Invocation

```
set -a; source ~/.jarvis/env; set +a
pytest tests/scenarios/test_live_wave4_realtime.py --live-llm              # burns 1-3
pytest tests/scenarios/test_live_wave4_realtime.py --live-llm --live-codex # burn 4
```

Trace: `~/.jarvis/realtime-run/wave4-trace.jsonl` (via `JARVIS_REALTIME_TRACE_JSONL`,
the production seam — 23 points on the Codex turn alone).
Measurements: `~/.jarvis/realtime-run/wave4-burn.json`, written by the tests
themselves so this document quotes measured values rather than impressions.

## Numbers

| Burn | What ran | Wall | Result |
|---|---|---:|---|
| 1 conversational | one real answer | 16.9 s | **PASS** |
| 2 cancelled mid-response | cancel 8 s into a real generation | 13.4 s | **PASS** |
| 3 action through the runner | Tier-0 `get_current_time` | 5 ms | **PASS** |
| 4 real Codex worker | `spawn_worker` + `verify_diff` on a throwaway repo | 453.4 s | **PASS** |

### Per-turn measurements

| Metric | Burn 1 | Burn 2 | Burn 3 | Burn 4 |
|---|---:|---:|---:|---:|
| time to first model output | 16 848 ms | — | n/a (Tier 0) | 347 648 ms |
| total turn latency | 16 853 ms | 13 447 ms | 5 ms | 453 384 ms |
| `response.started` → terminal | 16 850 ms | 8 018 ms | 3 ms | 453 381 ms |
| cancel request → terminal | — | **3 ms** | — | — |
| decide iterations | 1 | — | 1 | 2 |

"Time to first model output" is the `cost.recorded` timestamp: this route is
non-streaming (token streaming is ADR-0008 Step 8), so the first model output
*is* the provider call returning. Reporting a fabricated first-token number
would be dishonest about what this build measures.

Burn 4's 347.6 s "first model output" is one deep-model round; the trace's
`llm_sdk_call_start_to_batch_complete_ms = 347 433` confirms it is provider
time, not Jarvis overhead. `response_started_to_llm_request_ms = 214.3 ms` is
the local overhead between opening the run and issuing the request, against
ADR §9.2's ≤ 75 ms p95 target — see "What missed a target" below.

## Event rows

### Burn 1 — conversational

| assertion | observed |
|---|---|
| exactly one `response.started` | 1 |
| exactly one response terminal | 1 (`response.completed`) |
| terminal names the same `response_id` | yes |
| `response_hash` matches the approved plan | yes |
| `surface.response_open` / `_emitted` carry the L3 `response_id` | yes |

### Burn 2 — cancelled mid-response

| assertion | observed |
|---|---|
| cancel returned | `"cancelled"` |
| exactly one response terminal | 1 (`response.cancelled`) |
| `cancel_scope` / `reason` on the row | `generation` / `user_stop` |
| `response.completed` rows | 0 |
| **`action.*` terminals (D10)** | **0** |
| `surface.response_emitted` rows | 0 — nothing was delivered for a stopped response |

The trace also caught the exactly-one-terminal mechanism refusing a second
writer live, on an earlier attempt of this burn that timed out at the test's
own wait:

```
response_run_terminalized {"terminal_type": "response.cancelled", "won_cas": true}
response_run_terminalized {"terminal_type": "response.failed",    "won_cas": false}
```

The cancel won the CAS; the later failure path got `AlreadyTerminal` and
appended nothing. That is F20 observed rather than argued.

### Burn 3 — action through the ActionRunner

| assertion | observed |
|---|---|
| an action was dispatched | 1 |
| `action.running` carries `resource_keys` | yes (runner arm, not the inline arm) |
| `resource_mode` | `read_shared` |
| `worker.quiesced` rows | 1 |
| canonical terminals per action | exactly 1 |
| leases live after the turn | 0 — a read-shared action carries no cleanup debt |

Note this turn was answered by the **Tier-0** router, so no LLM call was made
(5 ms wall). That is still a real dispatch through the real ActionRunner with
a real resolved lease — but it does not exercise the write-exclusive path,
which is why burn 4 exists.

### Burn 4 — real Codex worker under a write-exclusive repo lease

| assertion | observed |
|---|---|
| a write-exclusive action ran | yes, `A74c9bf78` |
| resource key | `repo:/…/pytest-630/…/fixture-repo` (canonical realpath) |
| canonical terminals for that action | exactly 1 (`action.result_observed`) |
| `worker.quiesced` rows | 2 (`spawn_worker` + `verify_diff`) |
| `action.cleanup_completed` for the worker | exactly 1 |
| `verification_outcome` on that row | `verified` |
| **repo held from `action.running` to cleanup** | **105 727 ms** |
| leases live after the turn | 0 |
| response terminals | exactly 1 |

105.7 s of quarantine is the property F18/F23 exist for: the repository was
unavailable to any unrelated same-repo action for the whole Codex run *and*
its verification *and* its stash restore, and only the turn's cleanup
finalizer released it. `verification_outcome: verified` is derived from the
turn's own rows — `verify_diff` really did return a `verification` slot — not
asserted optimistically.

The child `verify_diff` borrowed the parent's scope instead of blocking on it
(F26): the turn completed in 2 decide iterations with a single lease, and
there is exactly one `action.cleanup_completed`, not two.

## What missed a target

**ADR §9.2 "utterance committed → LLM request sent, local p95 ≤ 75 ms".**
Burn 4 measured **214.3 ms**. This build does not chase that target: the
number includes the full `SituationPacket` build and prompt assembly on the
batch route, and ADR-0008's latency work for it lands in Steps 7-8. Recorded,
not fixed.

**Cancel does not stop the provider call.** Burn 2's response terminal was
durable 3 ms after the request, but the turn kept running a further
**5 432 ms** until the model finished, because `LLMClient.chat()` has no
cancellation seam until ADR-0008 Step 6 (typed cancellable adapters). The
guarantee this wave makes — "the response is terminally cancelled, exactly
once, and no action is touched" — holds; "the provider stops billing
immediately" is not claimed and is not true yet.

**Burn 3 did not exercise the LLM.** Tier 0 answered it. The row is honest
about being a Tier-0 dispatch; burn 4 covers the LLM-driven action path.

## Verdict

**PASS.** Both ADR-0008 Step 2 and Step 3 acceptance rows are met live:

- Step 2 — concurrent preset snapshots isolated (per-run `LLMRequestClient`,
  hermetic), **exactly one response terminal** (burns 1, 2, 4 + the observed
  losing CAS), **legacy output unchanged** (flag-off parity, hermetic).
- Step 3 — **result/cancel/timeout races** (one canonical terminal per action
  in every burn), **parent→verify no self-wait** (burn 4, one lease, two
  quiesced workers), **same-repo serialization through cleanup** (burn 4,
  105.7 s of quarantine), **different-repo parallel** and
  **resolver/scope escalation locked out** (hermetic), **DB/stash/live-action
  canaries** green throughout.

No P0 or P1 remains open. The two limitations above are ADR-scheduled work
for Steps 6-8, recorded here so a later wave does not rediscover them as
surprises.
