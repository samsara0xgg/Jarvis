# Live burn — 2026-09-04 (realtime line after `6ed7280`)

Regression burn for everything that landed on the realtime line after the
last accepted Wave-5 burn (`6ed7280`): the Codex takeover fixes
(`84d9a06`, `3a6856a`), the hub continuation (`d2e3e63` … `603a5ed`), and the
model-preset commits (`4b69f7c` … `b298be8`). None of those commits had a
live run behind them; the provider host they targeted was unreachable at the
time. Tree under test: `b298be8` plus the four fixes this burn produced
(uncommitted while the burn ran; the `git_head_sha` the tests stamp into
`~/.jarvis/realtime-run/wave*-burn.json` therefore still reads `b298be8`).

Real cloud LLM, real on-disk Event Log, real `drive_turn`, real Codex CLI
0.142.5 subprocess (`gpt-6-astra` per `~/.codex/config.toml`), real git
working tree. The decision model is the shipped `default_preset`, which the
live overlays copy byte-for-byte:

| Role | Model | Endpoint |
|---|---|---|
| decision LLM (`fast`) | `deepseek-v4-flash`, `thinking.type=disabled` | `api.deepseek.com` direct |
| `deep` (probe only) | `deepseek-v4-pro` | same |
| Codex worker | `gpt-6-astra` | Codex CLI account |

The 2026-09-03 burns ran `gpt-5.5` through the OpenRouter proxy. The faster
model changed two premises the old burns relied on (see findings 2 and 3).

## Invocation

```
set -a; source ~/.jarvis/env; set +a
PYTHONPATH=. pytest tests/scenarios/test_live_wave4_realtime.py --live-llm --live-codex
PYTHONPATH=. pytest tests/scenarios/test_live_wave5_realtime.py --live-llm --live-codex
```

`PYTHONPATH=.` is required inside a worktree: the shared `.venv` editable
install resolves `jarvis` to the main checkout otherwise.

A direct probe of `LLMClient.chat` and `LLMClient.stream_events` against the
shipped presets ran before the suites (script kept outside the repository;
numbers below).

## Numbers

| Burn | What ran | Wall | Result |
|---|---|---:|---|
| probe: chat `fast` | one real answer, `thinking.type=disabled` sent | 1.23 s | **PASS** |
| probe: chat `deep` | one real answer | 1.51 s | **PASS** |
| probe: `stream_events` `fast` | typed SSE stream, 7 text deltas | TTFT 0.51 s, total 0.61 s | **FAIL → PASS** (finding 1) |
| W4-1 conversational | one real answer | 1.8 s | **PASS** |
| W4-2 cancelled mid-response | cancel into a real generation | 9.7–12.0 s | **FAIL → PASS** (finding 2) |
| W4-3 action through the runner | Tier-0 `get_current_time` | 6 ms | **PASS** |
| W4-4 real Codex worker | `spawn_worker` + `verify_diff` on a throwaway repo | 133.5 s | **FAIL → PASS** (finding 3) |
| W5-1 background + second utterance | worker alive, second turn answered mid-flight | 122.4 s | **ERROR → PASS** (finding 4) |
| W5-2 cancel a live process | `codex app-server` reaped, repo freed, stash restored | 14.2 s | **ERROR → PASS** (finding 4) |
| W5-3 crash after claim | claim adopted once, no replay | 10.0 s | **ERROR → PASS** (finding 4) |

Final state: W4 4/4, W5 3/3, W4-2 repeated 3/3 at the new settle window.
Gates at the same tree: lint-imports 1/1, ruff clean (7 files), mypy strict
clean (7 files), 714 passed / 63 skipped hermetic in 30.23 s; after finding 5: lint-imports 1/1, 715 passed / 63 skipped in 29.33 s.

### Wave 4, final run

| Metric | Value |
|---|---:|
| conversational: utterance → first model output | 1 820 ms |
| cancelled: settle before cancel | 2.0 s (was 8.0 s) |
| cancelled: cancel request → terminal | 1–3 ms (3 runs) |
| cancelled: terminal → turn unwound | 7 667 / 10 027 / 7 673 ms |
| cancelled: action terminals written | 0 |
| codex worker: `action.running` → cleanup | 126 131 ms |
| codex worker: canonical terminal | `action.result_observed`, exactly 1 |
| codex worker: cleanup `verification_outcome` | `verified` |

`terminal → turn unwound` is the provider call finishing after its response
terminal is already durable. That gap is ADR-0008 Step 6 (provider-call
cancellation), unchanged by this burn.

### Wave 5, final run

| Metric | Value |
|---|---:|
| worker `action.running` → cleanup (repo held) | 108 175 ms |
| **second utterance → first output, worker alive** | **2 687 ms** |
| worker canonical terminal | `action.result_observed` |
| cleanup `verification_outcome` | `verified` |
| **cancel request → OS process gone** | **621 ms** |
| cancel request → canonical terminal | 658 ms |
| canonical terminals written | 1 (`action.cancelled`) |
| pre-task stash restored | `true` |
| live lease scopes after cleanup | 0 |
| spoken answer on cancel | `任务已停止，未完成` |
| **restart → recovery (claim adopted)** | **10 ms** |
| restart → real answer | 1 933 ms |
| historical intents present / replayed | 2 / 0 |
| `turn.started` rows for the crashed turn | 1 |

Against the 2026-09-03 `62aa743` run: second-utterance-to-first-output
3 709 → 2 687 ms, cancel-to-process-gone 694 → 621 ms, restart-to-recovery
12 → 10 ms. The lifecycle assertions are identical.

## Findings

### 1. `stream_events` could not reach any provider with a DeepSeek preset

`097f548` merged the preset's `extra_body` into the top-level request body
for the typed stream path. The OpenAI SDK rejects unknown keyword arguments,
so `stream_events` settled with `error_code="TypeError"` 7 ms after the call,
before any network I/O. The non-streaming `chat` paths were correct (they pass
`extra_body=`). Fixed in `jarvis/decision/llm.py`; pinned by
`test_openai_stream_sends_preset_extra_body_on_the_wire`
(`tests/integration/test_typed_llm_stream.py`), which fails on the old code
with `LLMResponseFailed` and passes with `thinking` present in the SSE peer's
received body. Live after the fix: TTFT 0.51 s, `LLMResponseCompleted`,
provider usage 24/7, one `on_settled` call.

The production streaming route is still unwired, so this defect had no user
impact; it would have surfaced the moment Wave 5C is switched on.

### 2. The cancel burn's 8 s settle window no longer measures "mid-response"

`deepseek-v4-flash` finished the three-paragraph answer in about 5.4 s, so
`assert not future.done()` fired before the cancel was issued. The second run
passed at 8 s on provider variance alone, which makes the constant a flake,
not a signal. `_CANCEL_SETTLE_S` is now 2.0 s (first token arrives at about
0.5 s), and the burn passed 3/3 with the cancel landing 2 009–2 013 ms into
the generation every time. Test-only change.

### 3. The in-turn waiter woke on a sync tool's old `action.result_observed`

The real regression-class finding, and it predates `6ed7280`.

`deepseek-v4-flash` called `list_tasks` and `get_current_time` before
`spawn_worker` in the same decide() iteration. Both sync tools write an
`action.result_observed` row the turn owns. `_wait_for_next_trigger`
accepted `action.result_observed` as a "defensive" trigger type filtered only
by `action_ids` (ADR-0009 D4), so after `spawn_worker` returned it handed the
`list_tasks` row back as the trigger:

```
action_wait_completed  outcome=trigger_observed  trigger_type=action.result_observed  action_id=Aa36d9fd7   # list_tasks
```

`_handle_result_observed` then ran on that stale row (claim statement
`tool unknown observed state of unknown`), the worker's `worker.reported` was
never handed to L3, no `action.result_observed` was written for the worker,
`verify_diff` was never proposed, and cleanup recorded
`verification_skipped`. The 2026-09-03 runs never hit this because `gpt-5.5`
went straight to `spawn_worker`.

Fix in `jarvis/runtime/__init__.py`: the waiter takes the process-local
`ActionLifecycle` and skips an `action.result_observed` row whose action this
process has already moved past `running` (a sync handler transitions the FSM
inline before `dispatch` returns). Unknown actions are not filtered, so crash
recovery is unaffected; `action.failed` / `action.timeout_assumed` /
`action.cancelled` are untouched. Both call sites pass it
(`drive_turn` and `_poll_waiting_turn` in `inherent_loop.py`). Pinned by
`tests/integration/test_waiter_skips_absorbed_sync_results.py`: with the
filter disabled it returns the sync row, with it the `worker.reported` row.
The D4 canary (`test_canary_waiter_scoped_to_own_actions`) still passes.
Live after the fix: W4-4 wrote exactly one worker terminal, ran
`verify_diff`, cleanup `verified`.

### 4. The Wave-5 live overlay no longer satisfied the intent-pump activation rule

`84d9a06` made `realtime.input.intent_pump` require
`concurrency_safety.confirmation_dispatch_outbox` and
`concurrency_safety.exactly_once_cost_accounting` in addition to the two
Wave-1 primitives. The live overlay only set the original two, the runtime
downgraded the pump (`realtime.input downgraded (concurrency_safety_disabled)`),
and every Wave-5 fixture assertion `input_flags.intent_pump is True` failed at
setup. The overlay now sets all four. Test-only change, but note that
ADR-0008 does not record the new prerequisites; that is a documentation gap
left by `84d9a06`.

### 5. The per-run request client dropped the preset's request-body fields

Found by Allen on the card right after the burn, not by the suites: the
third utterance ("run the simplest hello-world script") came back as an
empty answer after 17 s. The Event Log showed `tokens_out = 2048` — exactly
the `fast` preset's `max_tokens` — and `response_hash` equal to the SHA-256
of the empty string. Replaying the three utterances through a real runtime
with the provider reply logged showed `reasoning_content` on every call
(267 / 95 / 362 / 3 775 characters): DeepSeek was thinking, so
`thinking: {type: disabled}` never reached the wire. A raw SDK call with the
same `extra_body` produced no reasoning, so the loss was in Jarvis.

`LLMPresetSnapshot` (ADR-0008 Wave 4A, older than `097f548` / `4b69f7c`)
carried model, base URL, max_tokens, key env, timeout and retries, but not
`extra_body` or `reasoning_effort`. With `response_run_lifecycle` on, every
decision request goes through the `LLMRequestClient` built from that
snapshot, so neither field ever applied inside the daemon; the W4/W5 suites
did not notice because their prompts never made the model reason past
2 048 tokens. Fixed in `jarvis/decision/llm_session.py`: both fields are
part of the snapshot and its hash, and the run client passes them into its
preset. Pinned by `test_run_client_keeps_the_presets_request_body_identity`
(`tests/integration/test_wave4a_response_run.py`). Replay after the fix:
no `reasoning_content` on any call, 28–421 output tokens, every turn
answered in 1–4 s (the daemon was restarted on the fix; DeepSeek non-thinking
mode occasionally omits the `<voice>` / `<document>` tags, which the card
then shows raw — a known Wave 6 item, not a regression).

This also means `4b69f7c` (`reasoning_effort` passthrough) had never been
effective inside the daemon either.

## Not covered by this burn

- `reasoning_effort` passthrough (`4b69f7c`) — only the xAI presets use it and
  no xAI credit is available; no live evidence.
- The production streaming route, incremental TTS, and the Swift v2 protocol
  remain unwired; nothing here exercises them.
- Physical microphone / playback rows.
