# Live-Run Bug Log — ADR-0002 Definition-of-Done Verification

End-to-end bugs found and fixed during the post-Step-21 live-run pass on Allen's
Mac (worktree `claude-adr0001`, starting from commit `a66c24b`). Each entry
records the failure mode, root cause, fix scope, and Tier-1 regression status.

Ordered by discovery time. Open bugs at the bottom.

---

## B-0001 · `~/.jarvis/mac_events.db` schema incompatible with claude-adr0001

**Discovered:** 2026-05-18 during Deliverable A1 (CLI bootstrap smoke).

**Symptom:** `python -m jarvis --no-detach "hello"` crashed in
`jarvis.state.event_log.open_event_log` with
`sqlite3.OperationalError: no such column: type` while attempting
`CREATE INDEX IF NOT EXISTS idx_events_type ON events(type)`.

**Root cause:** The DB on disk at `~/.jarvis/mac_events.db` was created by a
sibling worktree (codex or hermes), whose Day-1 schema is richer
(`event_type`, `event_id`, `sequence`, `actor`, `turn_id`, `run_id`,
`action_id`). claude-adr0001's ADR-0001 schema is leaner
(`type`, `event_uid`, `id`, `correlation_json`). The mismatch is not in
production code — it is an environment-state collision.

**Resolution:** Not a code fix. Per Allen, redirect `JARVIS_RUNTIME_ROOT` to a
worktree-local path (`~/.jarvis-claude-adr0001`) so the existing
`~/.jarvis/mac_events.db` stays untouched. Day-1 honored
`JARVIS_RUNTIME_ROOT` already (`jarvis/deployment/__init__.py:91`), so no
code change required.

**Tier-1 status:** N/A (no source change).

---

## B-0002 · `thread/start` response parse miss for nested `thread.id`

**Discovered:** 2026-05-18 during Deliverable A2 (D-1 task seed) — L3
auto-dispatched a `spawn_worker` because the user utterance carried execute
intent. The Codex JSON-RPC client failed before turn/start ran.

**Symptom:** `action.failed` event with
`error="codex_thread_start_failed: thread/start response missing threadId:
{'thread': {'id': '019e3d1b-...', ...}, 'model': 'gpt-5.5', ...}"`.

**Root cause:** `jarvis/execution/codex_action.py::_extract_thread_id`
(line 230) only reads `result["threadId"]` / `result["thread_id"]`. Codex CLI
0.130's actual response shape is
`{"thread": {"id": "...", "sessionId": "...", ...}, "model": ..., ...}` —
threadId is nested under `thread.id`. The unit test mock at
`tests/unit/test_codex_action.py:99` was written against a flat shape that
Codex never actually returns, so the regression slipped through Tier-1.

**Fix scope:**

- `jarvis/execution/codex_action.py:230-246` — rewrote `_extract_thread_id`
  to prefer the real nested `result["thread"]["id"]` shape, with legacy flat
  `threadId` / `thread_id` fallback for forward-compat. Raises
  `_ProtocolError` with the original message when both forms are absent.
  Type signature unchanged.
- `tests/unit/test_codex_action.py:99` — `FakeCodexClient` `thread/start`
  mock now returns `{"thread": {"id": self.thread_id}}`, the real Codex
  0.130 shape (was the invented flat `{"threadId": ...}`).
- `tests/unit/test_codex_action.py:399-422` — three new direct unit tests
  pinning the new semantics: nested preference, flat fallback, raise on
  empty / missing-id.

Verified no other call sites parse a `thread/start` response. The two
remaining `"threadId"` usages at `codex_action.py:411` and `:431` are
**outgoing** request params for `turn/start` / `turn/interrupt` — protocol
direction is request, not response, so out of scope.

**Tier-1 status:** 577 passed (574 baseline + 3 new), wall 3.17s.

---

# Process & Test-fidelity Issues — RECORD-ONLY (do not fix yet)

Allen flag (2026-05-18 ~15:13 PDT): "发现过程中存在巨大问题 你的 test 可能也有
问题 有问题先别急着修 先记录下来". Section is append-only log of suspect
behaviors / suspect tests / process gaps spotted during DoD verification.
No fixes here — fix decisions get made AFTER the log is complete and
Allen has reviewed it.

## P-0001 · Unit-test mock shapes were invented, not captured

**Where:** `tests/unit/test_codex_action.py` (FakeCodexClient) — flat
`{"threadId": ...}` for `thread/start` instead of real Codex 0.130 nested
shape `{"thread": {"id": ...}}`. B-0002 only caught the response-parse
side of this; we have not yet audited whether the same module's mock
shapes for `turn/start`, `turn/completed`, `item/tool_call`,
`turn/interrupt` etc. are equally invented and equally divergent from
real Codex behavior.

**Implication:** Tier-1 green is not load-bearing for protocol fidelity
to Codex 0.130. The 577/577 number is structurally meaningful (lifecycle,
event-emit shape, registry validation) but does not certify that the
client wire-format matches Codex's actual JSON-RPC output. Each `live-codex`
run is potentially a new protocol-fidelity bug surface.

**Mitigation candidates (NOT YET CHOSEN):** capture real Codex 0.130
notification stream once during the first happy-path live run and pin
the fixture shapes to that capture; OR drop the FakeCodexClient unit
class entirely and rely exclusively on canaries + Tier-2 live-Codex to
exercise protocol code (Karpathy: prefer one true test over two
diverging ones).

## P-0002 · Tier-2 J/K/L scaffolds were written without ever running against real Codex

**Where:** `tests/scenarios/test_real_codex_*.py` — 41 named test functions,
every body is `pytest.skip(_SKELETON_SKIP)`. Each scaffold's docstring
asserts a specific invariant ("thread/start.cwd matches task.created.
payload['repo_path'] byte-for-byte" etc.), but the invariants were
written FROM the ADR without a live capture to corroborate them. Many
invariants will be wrong as stated:

- **J3 (byte-for-byte path match):** real Codex may normalize the cwd
  (resolve symlinks, drop trailing slash, expand `~`). Byte-equality
  may need to be relaxed to `Path.resolve()` equality.
- **J7 (cost.recorded.kind == "codex"):** the actual `kind` value emitted
  by Step 12 is the literal we picked. The ADR may have a different
  literal. Need to grep `cost.recorded` emit sites to pin reality.
- **J11 (item/tool_call captured once):** the production code may emit
  multiple `item/tool_call` events (one per tool, including unrelated
  tools the worker calls). "Exactly once" may be wrong.
- **K3 (parent PID never opens SQLite):** PID inspection on macOS is
  not free of false negatives. Need to check the actual open-audit
  hook semantics.
- **K7 / K8 (sleep mid-Codex-turn):** ADR says `install_power_observer`
  accepts an injectable factory. Whether the factory is reachable
  from a Tier-2 test (vs. only from a unit test) is uncertain.

**Implication:** Deliverable B is not a mechanical body-fill against the
docstrings — it is an empirical capture-and-pin against whatever the
first live A3 run actually emits. Some "invariants" in the scaffolds
may need to be relaxed or moved between J/K/L groups.

## P-0003 · `conftest.seed_one_open_task` seeds a task missing repo_path / verify_command

**Where:** `tests/scenarios/conftest.py:208-238`. The fixture emits
`task.created` with only `task_id` / `goal` / `source`. Day-2's
`spawn_worker` reads `repo_path` from `ActionRequest.payload`, which
the L3 builder derives from the Task Ledger snapshot (which folds
`task.created.optional_payload["repo_path"]`). If a Tier-2 test uses
this fixture and triggers spawn_worker, the spawn will crash on
missing `repo_path` (or worse, silently use cwd).

**Implication:** Every Tier-2 J/K/L test that exercises spawn_worker
needs a different fixture (or `seed_one_open_task` needs to be
extended to take repo_path / verify_command). My A3 fixture script
(`scripts/seed_yesterday_task.py`) already supplies both; the
conftest fixture lags.

## P-0004 · L3 LLM auto-spawned on "今天给我做" — semantic ambiguity, no Pre-action-Gate guard

**Where:** A2 live trace, events 19-32. User uttered "今天给我做
implement-rate-limiter, repo 是 /tmp/jarvis-day2-fixture" — Allen's
goal text framed this as "decision LLM only ~$0.10". L3 LLM
interpreted "做" as execute-intent and chained spawn_worker after
create_task.

**Implication:** The deterministic boundary between "create task" and
"execute task" is being delegated to the LLM. Two interpretations:

  (a) **This is correct behavior.** "今天给我做 X" in Chinese carries
      execute intent; the LLM honoring that is faithful. A2 was a
      verification fixture, not the production contract.
  (b) **This is a missing guard.** Production should not silently
      spend ~$1-3 on an unsolicited Codex run; the Pre-action Gate
      should refuse spawn_worker without a separate explicit user
      authorization for the first execution.

Per Allen's standing rule [feedback_no_prompt_patches], the fix (if
needed) lives in the Pre-action Gate or the L3 → action.proposed
shape, NOT in the L3 system prompt.

## P-0005 · A2 trace produced `claim.created` with `statement="tool unknown acknowledged dispatch for unknown"`

**Where:** A2 event id 31 (`claim.created`) and id 32 (`evidence.attached`).
After action.failed for spawn_worker, the result_interpreter fell into
its catch-all branch and emitted a degraded Execution Claim with the
literal text "tool unknown acknowledged dispatch for unknown".

**Implication:** The interpreter's fail-path string is fixture-level
debug text reaching the claim record. This will round-trip into
projections and may pollute task-ledger derived_status logic. The
correct path is probably to emit `claim.created` with an explicit
failure verbiage (e.g. statement="codex_thread_start_failed for
spawn_worker", level=failed, relation=refutes) — but that is a
behavior question, not a syntax fix.

## P-0006 · `_extract_thread_id` flat-fallback may be wrong direction

**Where:** B-0002 fix in `jarvis/execution/codex_action.py:230-246`.

The B-0002 agent added a "flat fallback" (`result.get("threadId")` etc.)
for forward-compat. But if the flat shape is the one we INVENTED
(Codex never emitted it), keeping the flat fallback is dead-code that
encodes a hallucination as a permitted code path. Karpathy: delete
fallbacks for shapes that don't exist in production. Decision pending.

## P-0007 · A3 fixture bypasses normal L3 → L4 → task.created event chain

**Where:** `scripts/seed_yesterday_task.py` directly calls `emit_event`
to insert `task.created` at yesterday 21:00. No corresponding
`action.proposed` / `action.authorized` / `action.dispatched` /
`action.result_observed` events were emitted (which is what a real
Day-1 task creation would produce).

**Implication:** The Task Ledger projection works (it only consults
`task.created`), so the resolver finds the task. But downstream
projections that count "executed actions for this task" or
"prior reviewer claims" will see zero history, which is **NOT** what
a real D-1 → D-2 transition looks like. The flagship may pass for
the wrong reason (e.g. Pre-emit Gate refusing because of "no prior
evidence" instead of refusing because of "claim exceeds evidence").

Mitigation candidate: extend the seed script to emit the full A2-style
chain at backdated timestamps (or accept that the live-run is a
synthetic D-2 with no D-1 actions, and document that limitation).

## P-0008 · run_turn timeout / 600s budget never live-tested

**Where:** `jarvis/execution/codex_action.py:310` — default `timeout_s=600.0`.
Combined with `_HEARTBEAT_INTERVAL_S=30.0`, a runaway Codex turn could
emit up to 20 heartbeats before the deadline kills it. Heartbeat fanout
may flood the event log if the turn hangs in a notification-poll loop
that never returns `turn/completed`.

**Implication:** The first live A3 may surface heartbeat emission rate
or DB-write rate issues that the synthetic Tier-1 tests would not.

**Confirmed in A3 live run:** Codex hit the 600s timeout exactly. 19
heartbeats fired in succession (events 13-31, 30s apart). Heartbeat
emission rate was fine (no DB-write contention observed) — but the
hang itself is the bigger issue, see B-0003 / P-0009 below.

---

## B-0003 · A3 live run — Codex 10-minute timeout, no surface response, no claim chain

**Discovered:** 2026-05-18 from A3 live run (event log
`~/.jarvis-claude-adr0001/mac_events.db`, runtime exit code 0 with
silent `trigger wait timed out` message).

**Symptom:** After 19 heartbeats (events 13-31, 9.5 minutes of activity),
events landed in this order:

```
32 action.timeout_assumed   2026-05-18 15:21:02
33 task.executor_reported   2026-05-18 15:21:02
34 cost.recorded            2026-05-18 15:21:02
[ runtime exits with stderr: "jarvis: trigger wait timed out: runtime: ]
[ no trigger event of types ('worker.reported', 'action.result_observed') ]
[ arrived within 5.0s (after_id=2)." ]
```

**What did NOT emit (should have):**
- `claim.created` (Limitation, level=executed, relation=limits, source=codex timeout)
- `evidence.attached` (referencing the timeout artifact)
- `surface.response_emitted`
- No `say` / `osascript` invocation reached the user

Allen received zero surface notification. The runtime silently exited
after the trigger wait timed out. Per ADR § Negative-path appendix
("Codex 超时，未完成"), the timeout path is supposed to deliver a
Limitation utterance via voice + banner.

**Three distinct sub-bugs nested here:**

1. **B-0003a** — L3 does not emit a Limitation Claim on `action.timeout_assumed`.
   The result_interpreter doesn't have a code path for the
   `action.timeout_assumed` event kind (or has one but it isn't
   reaching the post-emit pipeline).
2. **B-0003b** — Runtime trigger waiter expects `worker.reported` /
   `action.result_observed` but the spawn_worker timeout path fires
   `task.executor_reported` instead. The synonym is missing from the
   waiter's expected-types set. Waiter times out at 5s, runtime
   exits without emitting the surface response.
3. **B-0003c** — Even when the waiter times out, the surface pipeline
   should still emit a final "Codex 超时" response via the existing
   surface render path. Currently the code path drops the turn
   entirely on waiter timeout.

**Fix scope:** TBD — all three are real bugs and at least one
(B-0003b) is the surface-of-no-emission Allen explicitly tested for.

---

## B-0004 · P-0009 isolation strips `auth.json` → Codex websocket transport 401s on every request

**Discovered:** 2026-05-18 from A3 retry at 16:40 PDT (session
`/tmp/jarvis-codex-debug/sessions/2026/05/18/rollout-2026-05-18T16-40-25-*.jsonl`,
Codex logs DB `/tmp/jarvis-codex-debug/logs_2.sqlite`).

**Symptom:** Codex spawned, ran 17s, emitted `turn/completed` with
zero `function_calls`. `diff.txt` was 0 bytes (sha256 of empty
string), `/tmp/jarvis-day2-fixture/demo/rate_limiter.py` unchanged.
`worker.report_missing` fired (P-0010 reproduced).

**Smoking gun in `logs_2.sqlite`:** 7 sequential `codex_api::endpoint::responses_websocket`
ERRORs (ids 170, 243, 298, 361, 417, 473, 529), each with body
`failed to connect to websocket: HTTP error: 401 Unauthorized, url:
wss://api.openai.com/v1/responses`. The matching `feedback_tags`
INFO line (id=173) is decisive:

```
endpoint="/responses" auth_header_attached=false auth_header_name=""
auth_mode="" auth_env_openai_api_key_present=true
auth_env_codex_api_key_present=false auth_env_codex_api_key_enabled=false
```

`OPENAI_API_KEY` is in the spawned process's env (`auth_env_openai_api_key_present=true`),
but Codex chose `auth_mode=""` (none) and attached no Authorization
header (`auth_header_attached=false`). The auth-recovery path then
declined to retry: `auth.mode="managed"` `auth.outcome="recovery_not_run"`
`auth.recovery_reason="not_chatgpt_auth"` (id=174). All 7 retries
hit the same 401, then `turn/completed` fired with empty output.

**Root cause:** Codex 0.130's `responses_websocket` transport reads
credentials from `$CODEX_HOME/auth.json`, **not** from the
`OPENAI_API_KEY` env var. The P-0009 fix (commit 2841ad8) created
a per-spawn empty `CODEX_HOME` tmpdir to isolate `AGENTS.md` /
`config.toml`, which also removed the only auth path the websocket
transport reads. Hermes works because it does not isolate
`CODEX_HOME` at all — Codex inherits the user's default `~/.codex/`
which has `auth.json`.

**Fix:** In `jarvis/execution/codex_action.py:run_codex_action`,
after creating the per-spawn `CODEX_HOME` tmpdir, copy
`~/.codex/auth.json` into it. P-0009's isolation intent
(`AGENTS.md` / `config.toml`) is preserved — only the credentials
file is seeded. Source-missing case is a silent skip (the worker
will 401, but the driver itself returns cleanly — same shape as
the pre-fix failure mode on a fresh machine).

**Files touched:**
- `jarvis/execution/codex_action.py` — promote `Path` from
  `TYPE_CHECKING` to runtime, add 4-line seed step after the
  `mkdtemp` in the isolation branch.
- `tests/unit/test_codex_action.py` — 3 new tests: seed copies
  when source exists, silent skip when source absent, skip when
  caller pre-sets `CODEX_HOME`.

**Status:** Fixed (this commit). Unit tests structurally verify
the seed; live A3 retry pending to confirm the websocket connects
with the seeded auth.

---

## P-0009 · Cross-contamination: ~/.codex/AGENTS.md instructs the spawned Codex worker to use RTK

**Where:** `/Users/alllllenshi/.codex/AGENTS.md` line 1:
`@/Users/alllllenshi/.codex/RTK.md`. This is Allen's personal Codex
config telling his interactive Codex sessions to route every shell
command through `rtk` (the Rust Token Killer proxy). When jarvis's
`spawn_worker_handler` spawns the Codex CLI subprocess, Codex reads
this AGENTS.md and applies the same RTK instruction.

**Symptom (A3 session log, `~/.codex/sessions/2026/05/18/rollout-2026-05-18T15-11-03-*.jsonl`):**
Every `exec_command` issued by Codex was wrapped in `rtk`:

```
rtk pytest -q
rtk git diff -- demo/__init__.py demo/rate_limiter.py ...
rtk proxy pytest -q
rtk git status --short
rtk sed -n '1,220p' demo/rate_limiter.py
rtk ls -la demo
rtk python -c '...'
rtk which pytest
rtk python -m pytest -q
```

Codex's reasoning then references "the `rtk` summary" and gets
confused when RTK's reformatted output disagrees with what Codex
expects from raw pytest. The session log shows Codex spent the
last 7 minutes in a verification-loop:

  1. Applied a patch (`patch_apply_end` at 22:12:45 UTC — actual
     rate_limiter implementation landed in `/tmp/jarvis-day2-fixture`).
  2. Ran `rtk pytest -q`, got "no tests collected" per RTK summary.
  3. Tried `rtk proxy pytest -q`, then `rtk python -m pytest -q`, then
     cleared `__pycache__`, etc. — never resolved.
  4. Never called the `submit_report` MCP tool (0 invocations).
  5. Hit the 600s timeout.

**Implication:** jarvis's `spawn_worker` is not in fact running a
"vanilla Codex" — it inherits a polluted instruction context from
Allen's personal Codex setup. The DoD assumption (Codex is a
self-contained worker) is false on this machine.

**Mitigation candidates (NOT YET CHOSEN):**

  (a) Pass `-c base_instructions=""` (or `instructions_path=/dev/null`)
      via the 8-flag `_build_extra_args` slice to shut off
      `~/.codex/AGENTS.md` for jarvis-spawned Codex children.
  (b) Spawn Codex with `HOME=<jarvis-private-home>` so it reads a
      different AGENTS.md.
  (c) Live with the contamination but document it as
      "user-config-dependent" in the ADR (worst option — bakes
      machine state into the test).

Per [feedback_no_prompt_patches]: this is exactly the kind of
"environment-leakage" fix that should not go into Codex's prompt
or into Codex's natural-language instructions. The fix is at the
spawn-flag level.

---

## P-0010 · Codex never called `submit_report` — the MCP injection invariant (J11) cannot be observed

**Where:** A3 session log function_call inventory: 17 ×
`exec_command`, 2 × `update_plan`, **0 × submit_report**.

**Implication:** Two possible causes:
  (a) The MCP server `jarvis-tools` failed to start, so `submit_report`
      was not listed in Codex's `tools/list` reply — Codex literally
      cannot call a tool it cannot see.
  (b) The MCP server started fine, `submit_report` was listed, but
      Codex chose not to call it (stuck in pytest-verification loop
      per P-0009).

Without P-0009 fixed, we cannot distinguish (a) from (b) — even if
the MCP server is broken, RTK contamination would mask the symptom
because Codex stops short of "I'm done, let me call submit_report"
regardless.

**Fix scope:** TBD. After P-0009 is mitigated, re-run A3 and check
whether Codex emits a `function_call` with `name == "submit_report"`.

---

## P-0011 · 17 of 19 function_calls were `exec_command`, only 2 were `update_plan` — none of jarvis's MCP tools were invoked

**Where:** A3 session log function_call inventory.

**Implication:** Even ignoring `submit_report`, NO jarvis-side MCP
tool was called. Either:
  (a) The MCP server was never reachable from the Codex thread
      (P-0010 sub-case).
  (b) The MCP server was reachable but the worker prompt didn't
      describe `submit_report` (and any other tools) as appropriate
      affordances for "implement-rate-limiter".

Combined with P-0009, the picture is: Codex hyperfocused on local
shell verification (RTK-wrapped pytest) and never tried the MCP
surface at all.

---

## P-0012 · Tier-1 unit tests never exercise the timeout path's surface contract

**Where:** `tests/unit/test_codex_action.py` covers thread/start failure,
initialize failure, turn/start failure, turn/interrupt, and happy-path
turn/completed — but not the deadline-exceeded path that fires
`action.timeout_assumed`. The interpreter's behavior on
`action.timeout_assumed` is therefore uncovered.

**Implication:** B-0003a (no Limitation Claim on timeout) and B-0003b
(no waiter synonym for `task.executor_reported`) both slipped through
Tier-1 because no test exercises the bug surface. The 577 green is
real but incomplete — it tests the synchronous failure paths, not
the deadline path.

**Fix scope:** Two new Tier-1 unit tests — one driving the
deadline-exceeded path through `result_interpreter`, one driving
the runtime waiter against a `task.executor_reported` event.

---

## P-0013 · Default `timeout_s=600.0` is too long for DoD verification

**Where:** `jarvis/execution/codex_action.py:310`. 10 minutes
per turn × 5 J-sweep scenarios × 1-3 verification runs each = 30-150
minutes of wall-clock if every turn hangs.

**Implication:** Live verification is too expensive in wall time
with current default. Should be lowered to 90-180s for the
verification fixture (real production task budget can stay at 600s).
Probably exposed via a runtime config / fixture knob, not via a
prompt instruction.

---


