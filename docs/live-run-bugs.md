# Live-Run Bug Log — ADR-0002 Definition-of-Done Verification

End-to-end bugs found and fixed during the post-Step-21 live-run pass on Allen's
Mac (worktree `claude-adr0001`, starting from commit `a66c24b`). Each entry
records the failure mode, root cause, fix scope, and Tier-1 regression status.

Ordered by discovery time. Open bugs at the bottom.

---

## Reconciliation snapshot (2026-05-28, post Increment-2 route-A burn)

Merge-gate status of every tracked code bug. No code bug remains open.

| ID | Disposition | Evidence |
|---|---|---|
| B-0001 | env collision, no code fix | `JARVIS_RUNTIME_ROOT` redirect |
| B-0002 | FIXED | commit (nested `thread.id` parse) |
| B-0003 a/b/c | **FIXED** | `_RUNTIME_TRIGGER_TYPES` includes `action.timeout_assumed`+`action.failed` (runtime/__init__.py:90-95); `decision/__init__.py:652` timeout/failed branch; surface emits per B-0005 trace |
| B-0004 | FIXED, live-verified | commit `e006ed6` (seed `auth.json`) |
| B-0005 / B-0006 | **RESOLVED** (2026-08-10) | ADR-0002 Limitation-routing amendment: `worker.reported`+Limitation → `voice_notify` (B-0006 fix, K5 burn); timeout/failed → `queue_review` pinned as-decided (B-0005) |
| B-0007 | FIXED | commit `b62fd13` (`inputSchema` + protocolVersion) |
| B-0008 | FIXED | commit `373c0fb` (`approval_policy=never`) |
| B-0013 | **FIXED, live-confirmed** | commit `dc0abb4` (elicitation-drain); 4 Increment burns all `worker.report_missing=0` / `worker.reported.status=ok` → submit_report dispatched |
| B-0014 | FIXED, live-covered | commit `e599b8e`; Increment-2 burns exercise both untracked-capture (NOTES.md) and empty-diff (route A) paths |
| C23 / stash_ref gap | CLOSED | `stash_ref` forwarded on `worker.reported` payload (tools.py:897) |

Remaining non-bug work: Tier-2 skeleton skips (capture-seam +
explicit-defer scenarios, see `docs/progress.md`). B-0005/B-0006 were
resolved 2026-08-10 by the ADR-0002 Limitation-routing amendment (see
their entries below).

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

**Fix scope:** all three RESOLVED (verified in code 2026-05-28):

- **B-0003a** (no Limitation Claim on timeout): `decision/__init__.py:652`
  routes `action.timeout_assumed` / `action.failed` re-entry into a
  dedicated branch (`decision/__init__.py:1631-1743`) that emits the
  Limitation claim chain.
- **B-0003b** (waiter missing the `task.executor_reported` synonym):
  `_RUNTIME_TRIGGER_TYPES` (runtime/__init__.py:90-95) now lists
  `worker.reported`, `action.result_observed`, `action.timeout_assumed`
  and `action.failed`, so the waiter wakes on the timeout path instead
  of timing out at 5s.
- **B-0003c** (surface drops the turn on waiter timeout): with the
  waiter now triggering on `action.timeout_assumed`, the turn no longer
  drops — the B-0005 entry below records the timeout path emitting a
  full claim chain + `surface.response_emitted` ("Codex 超时，未完成").
  The residual question there (which Attention channel) is the
  B-0005/B-0006 spec gap, not this bug.

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

**Status:** Fixed (commit `e006ed6`) — **verified live 2026-05-18
17:23-17:33 PDT** via second A3 retry under the same
`/tmp/jarvis-codex-debug` `CODEX_HOME` debug override (manually
pre-seeded with `~/.codex/auth.json` before the run).

Live verification numbers from `/tmp/jarvis-codex-debug/logs_2.sqlite`:

| | Before fix (16:40 run) | After fix (17:23 run) |
|---|---|---|
| `auth_header_attached` | `false` | `true` |
| `auth_mode` | `""` | `"Chatgpt"` |
| 401 `Unauthorized` count | 7 | 0 |
| `function_call` items | 0 | 9 |
| Codex turn duration | 17 s (crash) | 600 s (timeout) |
| Fixture `pytest` result | n/a (no diff) | **3/3 pass** |

The websocket transport now attaches the Authorization header from
the seeded `auth.json`. Codex actually completed the TokenBucket
implementation (`/tmp/jarvis-day2-fixture/demo/rate_limiter.py`,
+41 LOC) before jarvis's 600 s budget tripped. P-0010 reproduces
on the same run (next entry); B-0005 surfaced from this run.

---

## B-0005 · Limitation surface emission delivers via `stdout` only — voice + banner missing

**Discovered:** 2026-05-18 17:33 PDT from second A3 retry (the
post-B-0004 run that produced the verification numbers above).

**Symptom:** After Codex hit the 600 s budget, the L3 → claim chain
fired correctly (B-0003 fix held), then:

```
35 claim.created          type=Limitation
                          statement="tool spawn_worker reported limitation: codex_turn_timeout"
36 evidence.attached      level=reported relation=limits
39 surface.response_emitted attention_channel=queue_review
                            delivered_via=["stdout"]
                            text="Codex 超时，未完成"
                            voice_text="Codex 超时，未完成"
```

`attention_channel="queue_review"` and `delivered_via=["stdout"]` —
the response *did* emit (so this is NOT a B-0006-style total
silence), and the `voice_text` field is populated, but the actual
delivery picked only `stdout` (the runtime's log file). Allen
received zero audio and zero banner notification — and stdout in
`--no-detach` mode is just the terminal where jarvis was launched.

**Re-classification (2026-05-18 evening after re-reading ADR
§ "Attention channel → physical surface mapping" line 1017-1044):**
this is **NOT an ADR violation** — it is a **spec/ADR gap**.

ADR line 1027-1028 defines `queue_review → cli_stdout` only (no
banner, no voice) as the channel's correct physical surface set.
So `delivered_via=["stdout"]` is literally what ADR specifies for
`queue_review`. The mismatch with Allen's expectation isn't at the
channel-delivery layer — it's that the **Attention Policy chose
`queue_review` for a Limitation claim** when arguably `voice_notify`
(ADR line 1031, "the flagship channel for the scenario") would
match the user's mental model better. ADR does not say which
channel a `Limitation` claim type must route to.

**Distinct from B-0006** (same family — see entry below): both are
the same gap in L3 Attention Policy routing for Limitation claims;
B-0005 is the timeout-path observation, B-0006 is the verify-fail
path. Both `queue_review` and `silent_log` channels themselves are
behaving per ADR-0002 spec; the open question is which channel
Limitation should target.

**Resolution (2026-08-10):** pinned **as-decided** by the ADR-0002
Limitation-routing amendment: `action.timeout_assumed` /
`action.failed` Limitation turns stay `queue_review` — after a
worker timeout Allen has typically walked away, and quiet-first
(spec §1.5 principle 4) queues the limitation for review instead of
speaking to an empty room. Escalation to a badge/banner surface is
deferred until an Inherent cockpit panel exists. No code change on
this path; the decision is enforced normatively by
`test_attention_queue_review_on_timeout_limitation` (Tier-1).

**Suspect surface:** L3 Attention Policy table — the mapping from
`claim_type=Limitation × source=spawn_worker` to attention channel
is not pinned by ADR.

**Disposition (under Allen X decision, see P-0010 update below):**
**spec/ADR gap, not a bug.** Resolution requires either an ADR-0002
amendment that pins Limitation→voice_notify (or another channel
with voice+banner physical surfaces), or a spec.html edit clarifying
Attention Policy defaults for Limitation claims. Out of scope for
this session.

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

**2026-05-18 evening update (after B-0004 fix + second A3 retry):**

P-0009 was implicitly mitigated by the per-spawn `CODEX_HOME`
isolation (commit `2841ad8`), so RTK is no longer wrapping Codex's
shell calls. We re-ran A3 (17:23-17:33 PDT) and got hard data:

- 9 × `function_call` total
- 9 × `exec_command`, **0 × `submit_report`**
- MCP `tools/list` handshake confirmed submit_report was advertised
  (Codex KNEW the tool existed)
- Codex completed the implementation (rate_limiter.py +41, 3/3
  pytest pass) but chose `exec_command` throughout, never invoked
  the MCP tool, never signalled completion

This eliminated cause (a) (server-not-reachable) and confirmed
cause (b): Codex with no completion-signalling instruction in its
base_instructions + a sparse task prompt does not autonomously
call submit_report.

**Allen X decision (2026-05-18 ~19:30 PDT):** **By-design under
ADR-0002 Negative-path appendix line 1596-1600.** When Codex
doesn't call submit_report, J11 (exactly-once call) is not
applicable; J12 fires (`worker.report_missing` + Limitation Claim
at `level=reported`). Current behavior is ADR-aligned. No code fix.

**Latent issue deferred:** Tier-2 J flagship-happy test
(`test_real_codex_flagship.py::happy_path`, currently skipped)
asserts J11 unconditionally. When that test is un-stubbed, real
Codex's actual behavior will keep failing it. Three forward paths
(all deferred to a future session):
  (1) Re-state J11 as conditional ("when submit_report IS called,
      it must be called exactly once").
  (2) Find a way to make real Codex call submit_report (e.g.
      tighter task prompt, structured spec injection — bounded by
      `feedback_no_prompt_patches`).
  (3) Accept Tier-2 J:happy cannot be fully validated with real
      Codex 0.130 + sparse prompts.

**Status:** **By-design (closed for this session)**, with J11
un-stub problem deferred.

---

## B-0006 · Verify-fail path routes to `silent_log` — total surface silence

**Discovered:** 2026-05-18 from prior session work
(observation 11005/11016). Not reproduced live in this session;
recorded here as the verify-fail-path companion to B-0005 above.

**Symptom (recorded, not live-reproduced):** On the verify-fail
path (Codex completed, diff exists, but `verify_command` returned
non-zero), `surface.response_emitted` payload had
`attention_channel="silent_log"` and `delivered_via=[]`. Allen
received zero notification despite the full claim chain being
present (claim.created + evidence.attached + gate.evaluated).

**Re-classification (2026-05-18 evening, same analysis as B-0005):**
ADR line 1027 defines `silent_log → (none)` — event appended, no
user-visible side-effect. So `delivered_via=[]` is literally the
correct behavior for `silent_log`. The mismatch is again at the
Attention Policy routing level: `Limitation` claims on verify-fail
paths are routed to `silent_log` when arguably they should go to
`voice_notify`.

**Distinct from B-0005:** different terminal state of `spawn_worker`
action — B-0005 is `action.timeout_assumed`, B-0006 is `verify_diff`
with non-zero exit code. Both produce Limitation Claims but the
Attention Policy picks different channels. The underlying spec gap
(which channel does Limitation belong to?) is identical.

**Disposition (under Allen X decision):** **spec/ADR gap, not a
bug.** Same resolution path as B-0005: would require an ADR-0002
amendment or spec.html edit. Out of scope for this session.

**Resolution (2026-08-10):** **fixed** by the ADR-0002
Limitation-routing amendment: a `worker.reported` turn that emits
`claim.created(type=Limitation)` (verify-fail / reviewer-fail) now
routes `voice_notify` — the limitation utterance reaches `say` +
banner per the K5 acceptance row. Implementation:
`attention_policy(..., limitation_emitted=)` in
`jarvis/decision/gates.py` + the `_finalize_response` scan in
`jarvis/decision/__init__.py` (current-turn events only, so
historical Limitations never re-trigger voice). Acceptance:
`test_real_codex_verify_fail.py::test_k5_limitation_routes_voice_notify_and_reaches_say`
live burn + three Tier-1 unit tests in
`tests/unit/test_attention_policy.py`.

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

## B-0007 · jarvis-tools MCP wire schema field naming + protocol version

**Where:** `jarvis/execution/codex_mcp_tools.py:54` (`_PROTOCOL_VERSION`)
and `jarvis/execution/codex_mcp_tools.py:77` (`SUBMIT_REPORT_TOOL`
schema key).

**Symptom (A4 r1-r6):** Codex 0.130 app-server reported the
jarvis-tools MCP server `status=failed` with `error="MCP startup
failed: Unexpected response type"`. Codex never offered
`submit_report` as a callable tool; the worker either ignored the
goal or fell back to shell, producing `worker.report_missing` every
run. Misdiagnosed for two sessions as "codex app-server ignores
external MCP servers from config.toml" until a probe (2026-05-25)
captured the full `mcpServer/startupStatus` stream and surfaced the
real error string.

**Root cause:** jarvis-tools advertised the older MCP wire format —
`protocolVersion: "2024-11-05"` and the snake_case key
`input_schema` on `SUBMIT_REPORT_TOOL`. Codex 0.130 negotiates
MCP `protocolVersion: 2025-06-18` and expects the camelCase
`inputSchema` key per the 2025-06-18 spec; the snake_case key
caused Codex's MCP client to reject the `tools/list` payload as
"Unexpected response type" and abort startup.

**Fix landed (commit b62fd13):** Rename schema key
`input_schema` → `inputSchema`; bump `_PROTOCOL_VERSION` to
`"2025-06-18"`. Post-fix isolated probe shows `jarvis-tools` going
`starting → ready` and Codex emitting an `item/tool_call` for
`submit_report` during the turn. The downstream dispatch path
still hangs — see B-0013.

**Related:** ADR-0002 § Step 6 / Step 7 code example and test
contract were aligned to the camelCase key in the same commit so
the canonical doc matches what Codex actually consumes.

---

## B-0008 · Codex pycache cleanup deadlocks under approval_policy=on-request

**Where:** `jarvis/execution/codex_action.py:_build_extra_args`.
Before commit 373c0fb the driver did not pass `approval_policy`,
so Codex 0.130 used its default value `on-request`.

**Symptom (A4 r7-r8, live-verified from preserved rollout JSONL):**
After Codex completed the TokenBucket implementation and the
visible tests passed, the model attempted
`rm -r demo/__pycache__ tests/__pycache__` to keep the diff
minimal. Codex 0.130 classifies `rm -r` as escalation-required and
emitted `function_call` with
`sandbox_permissions="require_escalated"` and a `justification`
prompt asking the operator to approve. In a non-interactive jarvis
spawn no operator exists, the function_call stalled, and jarvis's
600s turn budget timed out the run 540s later. r8 rollout item
[56] is the deadlocked exec_command; item [58]
function_call_output reads `"aborted by user after 542.7s"`.

**Fix landed (commit 373c0fb):** Add `-c approval_policy=never` to
the spawn flag set. With `never`, Codex auto-denies escalation
requests instead of blocking; the model receives the denial, skips
the destructive command, and continues to `submit_report`. r9-r11
rollout JSONL confirms `0` `require_escalated` function_calls.

**Note:** B-0008 is independent of B-0007. The two stacked because
r7 was the first run where the inputSchema fix actually let Codex
*find* submit_report — which made the model behave as a "real
worker" (clean diff, run tests, clean up), which surfaced the
escalation path that the pre-fix runs never reached.

---

## B-0013 · Codex 0.130 receives MCP function_call but never dispatches to subprocess (FIXED — commit dc0abb4, live-confirmed 2026-05-28)

**Where:** Codex 0.130 internal MCP dispatch path. Reproduced
deterministically in A4 r9, r10, r11 (after B-0007 + B-0008 fixes).
Not a jarvis bug per se — the jarvis-tools subprocess is correctly
spawned, reaches `status=ready`, and responds to direct
`tools/call` requests in 0.2s (verified by isolated stdio probe).

**Symptom:** OpenAI returns a `response.output_item.done` for a
`function_call` with `name=submit_report`,
`namespace="mcp__jarvis_tools__"`, and a fully-formed
`status=ok` + `summary=...` argument payload. Codex's app-server
emits exactly one `item/started` notification for this call and
then sits idle for 521-573s — no `item/completed`, no second
OpenAI websocket round, no `turn/completed`. The 600s jarvis
turn budget eventually fires `turn/interrupt`; Codex synthesizes
a `function_call_output` reading `"aborted by user after 521.7s"`
and the conversation never resumes. `codex_otel.trace_safe`
reports `model_needs_follow_up=true` and `needs_follow_up=true`,
confirming Codex knows the turn is not done — yet no MCP
dispatch ever happens. `logs_2.sqlite` has 0 ERROR rows and the
24 WARN rows in r11 are unrelated skill-icon noise.

**Ruled out:**

  (a) jarvis-tools server bug — direct stdio probe round-trips
      `initialize` + `tools/call submit_report` in 0.2s.
  (b) MCP protocolVersion mismatch — bumped 2024-11-05 →
      2025-06-18 in B-0007; behaviour unchanged in r10.
  (c) approval-policy block — `approval_policy=never` (B-0008 fix)
      eliminates `require_escalated`; behaviour unchanged in r9.
  (d) Missing Codex feature flags — enabling
      `features.enable_mcp_apps=true` and
      `features.builtin_mcp=true` (Codex 0.130 "under development"
      MCP feature gates) in r11 did NOT change the dispatch
      behaviour. We left both flags on as defensible alignment.

**Remaining hypotheses to test next session (cheap, no LLM burn):**

  1. Hermes uses the same `codex_app_server` transport — does
     Hermes hit this wall too, or does it have configuration we
     haven't replicated? Compare `agent/transports/codex_app_server.py`
     and the spawn-flag set.
  2. The `apps_mcp_path_override` feature flag (still under
     development) might be the third needed gate.
  3. `dangerously-bypass-approvals-and-sandbox` (or its
     `-c`-equivalent) might lift a deeper gate that
     `approval_policy=never` doesn't reach.
  4. Codex source code (locally available via `codex --version`
     binary path) might show the exact dispatch precondition.

**Impact (at discovery):** Belt-and-suspenders was degraded to
suspenders only. The ADR-0002 §3.5.8 evidence ladder still worked (r6
demonstrated this end-to-end with `task.verified` via subprocess
exit-0), so the production happy path stayed intact via fallback.
submit_report was the desired ideal but not blocking for DoD.

**Resolution (commit `dc0abb4` "drain MCP elicitation + capture
item/completed", live-confirmed 2026-05-28):**

Root cause was hypothesis (1)-adjacent but more specific: Codex 0.130
sends a `mcpServer/elicitation/request` JSON-RPC for *every*
external-MCP tool call. `approval_policy=never` (the B-0008 fix)
suppresses *exec* approvals but does NOT cover the MCP elicitation
gate, so the `submit_report` dispatch blocked waiting on an elicitation
reply that never came — exactly the "one `item/started`, then 521-573s
silence, no `item/completed`" symptom.

The fix drains `CodexAppServerClient.take_server_request` on every poll
iteration and auto-accepts elicitations/approvals
(`codex_action.py:29-31`, `325-351`, `704`, `763`), so the tool call
actually round-trips. Belt-and-suspenders is restored.

**Live confirmation:** all four Increment-1/Increment-2 burns
(happy / no_verify route-B / reviewer_fail K5 / route-A empty-diff)
froze traces with `worker.report_missing == 0` and
`worker.reported.status == "ok"`. Since `worker.report_missing` fires
iff `codex_result.submit_report is None` (tools.py:833) and the `ok`
status comes from the submit_report payload itself
(`submit_report.get("status", "report_missing")`, tools.py:850),
both signals together prove `submit_report` was called AND its
`item/completed` was captured on every run — the dispatch no longer
stalls.

---

## B-0014 · `_capture_diff` omits untracked files → lossy diff artifact induces false reviewer refutes + latent `task.no_op` false-negative

**Where:** `jarvis/execution/codex_action.py::_capture_diff` — the live
producer of `CodexActionResult.diff_text`, written to the diff artifact
and later read by `verify_diff` / the reviewer. Surfaced by the
Increment-1 Tier-2 happy-path live burn (2026-05-28).

**Symptom:** The happy-path task asked Codex to add a docstring AND
create a top-level `NOTES.md`. Codex did both — `NOTES.md` exists on
disk (untracked, 307 B). Yet the reviewer attached a `refutes`/Limitation
row "NOTES.md was not created". The diff artifact (`diff.txt`) held only
the tracked docstring hunk; the new untracked `NOTES.md` was absent, so
the reviewer — which reads only the artifact — hallucinated under-
delivery. `task.verified` still fired correctly (verify_command exit 0 +
non-empty diff; the ladder ignores a reported-level refute by design —
ADR § Evidence ladder rows 283-285), so the OUTCOME was a true positive.
The defect is the false audit row, not the verdict.

**Root cause:** `_capture_diff` ran plain `git -C cwd diff`, which
reports tracked-file changes only and omits untracked (newly created)
files. Asymmetric with `diff_capture.isolate_pretask_changes`, which
already uses `git stash push -u` (untracked-aware). Two consequences:
  1. the reviewer reviews a lossy artifact → deterministic false
     "file-not-created" refutes for any new-file deliverable (the root
     cause behind the reviewer-hallucination class tracked as B-0010 in
     session memory);
  2. `diff_nonempty` is derived from the same diff → a Codex run that
     creates ONLY untracked files would register `diff_nonempty=False`
     → Fix-2 Option A short-circuits to `task.no_op` despite real work
     (latent false-negative).

**Spec alignment:** spec.html §8.9 lists "artifact changed (intended
files touched)" as a minimum code-task claim, and §8.5 maps
`git diff exists → file changed`; a newly created file is an intended
touch, so the capture must include it.

**Fix:** `_capture_diff` now appends each untracked file (enumerated via
`git ls-files --others --exclude-standard -z`) as a proper new-file
unified diff (`git diff --no-index -- /dev/null <file>`). Read-only — no
index mutation, so the surrounding stash/restore machinery is untouched;
`--exclude-standard` keeps gitignored build junk (e.g. `__pycache__`)
out. Regression tests in `tests/unit/test_codex_action.py`:
`test_capture_diff_includes_untracked_new_file` (RED→GREEN) plus a
real-git tracked-modification guard that replaces the former single-call
`subprocess.run` mock.

**Follow-up (not on the live path):** `diff_capture.capture_diff` is a
dead duplicate (no production caller in `jarvis/`) carrying the same
tracked-only defect — left untouched to keep this fix surgical; consider
consolidating onto a single untracked-aware capture later.

**Status:** Fixed (commit `e599b8e`). Unit-proven (RED→GREEN) + Tier-1
green (876 unit/canary, ruff/mypy `jarvis/`, lint-imports).

**Live-covered (2026-05-28, Increment-2 burns):** the fix is now
exercised live in both directions. The no_verify (route B) and K5
reviewer_fail burns ask Codex to create an untracked top-level NOTES.md
and both froze an Artifact claim over a non-empty diff — proving the
untracked-capture half works against real Codex. The route-A empty-diff
burn (strictly read-only goal) froze `diff_nonempty=False` with NO
spurious untracked capture — proving the fix does not over-report.
Together these close the latent `diff_nonempty` false-negative concern
without a dedicated re-burn.

---


