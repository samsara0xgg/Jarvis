# ADR 0002 — One Scenario, No Simplification on Critical Path

## Status

Proposed — 2026-05-18. Builds on ADR 0001 (Day-1 MVP, approved 2026-05-17).

## Context

ADR 0001 shipped the Day-1 happy path end-to-end against real OpenRouter +
gpt-5.5: 24-event canonical trace, 6-layer architecture, 13 build steps,
three-gate enforcement, ActionLifecycle 8-state. The architecture is real;
two L4 doers on the critical path are not:

- `spawn_worker_handler` → `threading.Timer(50ms)` writing a hardcoded
  `{"status":"ok"}` JSON artifact
- `verify_diff_handler` → literal `data["status"] == "ok"` predicate
- L3 resolver picks "latest open task" because no time-window query exists
- CLI blocks on synchronous turn; no detach, no banner, no TTS

This ADR takes the **same single scenario** ("昨天那个 task 给 Codex 跑一下，
做完审核了再告诉我。") and removes every critical-path simplification.
No new scenarios. No new architecture. Day-1 invariants A–I remain in
force; this ADR adds tiers J / K / L.

---

## Scope clarification

This ADR is **not** "complete all of ADR-0001's Stage 2." It is the same
single scenario MVP, with **every critical-path simplification removed**
(per Allen's P1: "完整版跑通，不做任何简化").

Stage 2 items **not** on this scenario's critical path remain deferred per
ADR-0001 line 1054 (Stop after Step 13):

- Multi-task disambiguation (ConfirmationRequest path) — scenario has
  single task in window
- Streaming CLI output (`chat_stream`)
- Tier 0 regex shortcuts (the `_LONG_RUN_RE` fork-classifier is L6, not
  L3 Tier 0 — see deviation V2)
- Memory system / preference learning
- Health tracker wiring
- LLM cost ceilings / budget enforcement (Day-2 records cost, doesn't cap)
- Push-to-phone, voice input, push notifications, multi-tool ecosystem,
  OBSERVER twin-verify principal, LLM/Codex provider failover

**On the critical path and therefore implemented in Day-2** (these
were `Stage 2` per ADR-0001 but cannot be simplified per P1):

- Sleep/wake protocol (spec §3.7.8) — Codex turns span sleeps; deviation
  V4 limits scope to Mac-only minimum but the protocol is real.
- Codex `submit_report` tool injection (spec §3.5.8) — worker report
  enforcement is jarvis's distinguishing feature; no shortcut.

If a feature is not load-bearing for the flagship utterance, it is **out
of scope for ADR-0002 even if mentioned in spec.html**.

---

## Conflict resolution policy

When sources of truth disagree, the order of precedence is:

1. **`docs/spec.html` §3 Six-Layer Architecture** is THE primary source
   of truth for: layer ownership (§3.1), L3 invocation flow (§3.4.3),
   trigger taxonomy (§3.4.1), Result Interpreter semantics (§3.4.11),
   ResponsePlan canonical shape (§3.4.13), worker report enforcement
   (§3.5.8), unified async lifecycle (§3.5.9), and Mac sleep/wake
   protocol (§3.7.8). When §5 / §8 / §13 conflict with §3, §3 wins —
   they elaborate §3, they do not override it.
   Subordinate canonical sections: event taxonomy (§5.4), evidence
   semantics ladder (§8.4: reported → observed → executed → verified →
   accepted), the 7-rule Claim/Evidence model (§8.5), and the 12
   numbered invariants (§13.2).
2. **Hermes Agent** at `~/Projects/hermes-agent/repo/` is THE final
   reference for Codex integration specifics. Its
   `agent/transports/codex_app_server.py` (399 LOC, MIT) and
   `agent/transports/codex_app_server_session.py` orchestration are the
   definitive implementation patterns. **If spec.html and Hermes conflict
   on Codex specifics, Hermes wins** — spec.html doesn't dictate Codex
   implementation; it specifies the L4 contract Codex must fit.
3. **jarvis-legacy** at `~/Projects/jarvis-legacy/` is reference-only for
   non-Codex modules: pricing module (verbatim lift), `osascript`
   shape, `tool_result/tool_error` helpers.

---

## Deviations from spec.html (explicit)

Per the conflict-resolution policy above, deviations from spec must be
called out, not silent. Day-2 carries the following known deviations:

| # | Spec section | Deviation | Reason |
|---|---|---|---|
| V1 | §5.4 | New event type `surface.response_emitted` not in spec §5.4 canonical list | Day-2 audit field for `delivered_via`; back-spec to §5.4 in Day-N |
| V2 | §3.4.6 | A pre-L3 LLM-free regex classifier (`_LONG_RUN_RE` in CLI/L6) makes the fork-detach decision before L3 Intent Routing runs | Required because SQLite cannot cross fork; classifier is not a general Intent Router — it only routes long-run vs synchronous |
| V3 | §13.1 Pre-action Gate | L4 (Codex sandbox `workspace-write`) does the workdir-scope enforcement, not jarvis's own L4 SandboxPolicy | Acceptable Day-2 because Codex enforces it via `-c sandbox_workspace_write.writable_roots`; jarvis SandboxPolicy stays minimal Day-2 |
| V4 | §3.7.8 (sleep/wake) | Day-2 implements the *minimum* sleep/wake protocol (mac.sleeping / mac.awake / worker.suspended_by_sleep / wake reconciliation); cross-domain mac.sleeping publication is deferred (Mac-only, no RPi consumer) | Mac-only architecture; sleep/wake is on-critical-path for 3-minute Codex turns and must not be simplified further |

No other deviations are permitted without an ADR amendment.

---

## Definition of Done

When ADR-0002 implementation completes, the utterance "昨天那个 task 给
Codex 跑一下，做完审核了再告诉我。" runs **fully functional, end-to-end,
zero stubs on the critical path** on Allen's Mac.

Every internal piece that was simplified in ADR-0001 (on the critical path
of this scenario) is materialized:

- real OpenAI `codex` CLI subprocess via JSON-RPC app-server (no
  `threading.Timer`)
- real `git -C repo_path diff` capture into an artifact file (no
  hardcoded JSON)
- real reviewer LLM at L3, plus opt-in real `verify_command` subprocess
  at L4 (no `data["status"] == "ok"` literal)
- real macOS notification banner via `osascript` + real `say` TTS
  (no CLI-only output)
- real fork-detached daemon (no synchronous block; CLI returns the ack
  immediately, child finishes the work)
- real time-window resolver against the event log (no "latest open
  task" stub)
- real cost tracking via `cost.recorded` events for every LLM call and
  every Codex turn

**Acceptance**: the scenario passes Tier 2 J + K + L tests against real
OpenRouter + real Codex CLI on Allen's Mac.

---

## Decision

### Scope discipline

ADR-0002 extends ADR-0001 along exactly one axis: **remove every stub on
the critical path of the flagship utterance**. The six-layer architecture
(L1–L6), the event spine, the three gates, ActionLifecycle 8-state, and
all 13 spec invariants remain unchanged.

If a feature is not load-bearing for this exact utterance, it is out of
scope even if spec.html names it.

### Scenario (real, end-to-end)

**D-1 (yesterday's session):**

```
$ jarvis "今天给我做 implement-rate-limiter，repo 是 ~/Projects/foo"
```

L3 calls the new `create_task` L4 tool. A `task.created` event lands in
`~/.jarvis/mac_events.db` with `task_id`, `goal`, `repo_path`,
`source="manual"`, and (if detection fires) `verify_command="pytest"`.

**D-day (the flagship scenario):**

```
$ jarvis "昨天那个 task 给 Codex 跑一下，做完审核了再告诉我。"
```

The flow:

1. **CLI cheap classification (L6, LLM-free)** — utterance hits a
   regex keyword classifier
   (`跑|spawn|给 *codex|帮我做|帮我跑|做一下|审核|run`). If it matches the
   long-run shape, CLI prints a quick ack ("好的，跑起来了") and
   `fork_detach()` returns immediately. Parent calls `os._exit(0)`;
   child re-bootstraps the runtime and continues. SQLite is never opened
   before the fork.
2. **Time-window resolver (L3, real)** — L3 LLM emits structured
   `{since_ts, until_ts}` for "昨天" (anchored to current epoch_ms).
   Resolver runs `SELECT` over `events` for `task.created` rows whose
   ts falls in the window. Single match → bound; multi-match →
   `unknown_subject` fail-fast; zero match → limitation response.
3. **spawn_worker dispatch (L3 → L4)** —
   `task.executor_assigned(executor="codex", model="gpt-5.5")` event,
   then real `spawn_worker_handler`:
   - `CodexAppServerClient` spawns `codex app-server` subprocess with
     explicit `-c` flag overrides (see § Codex contract).
   - JSON-RPC `initialize` → `thread/start({cwd: repo_path})` →
     `turn/start({input: task.goal})`.
   - Poll `take_notification()` loop; emit `worker.heartbeat` every 30s
     with last `item/*` event summary.
   - On `turn/completed`: capture token counts, run
     `git -C repo_path diff` → write to artifact dir.
   - Write `diff.json = {status, run_id, task_id, summary, diff_path,
     exit_reason}` + `diff.txt`.
   - Emit `worker.artifact_observed` + `worker.reported` +
     `task.executor_reported`.
   - Failure paths: timeout → `action.timeout_assumed`; crash →
     `action.failed`; OAuth missing → `worker.report_missing`.
4. **verify_diff (L4 observation, real)** — handler returns
   **raw observations only** (no reviewer call, no verdict). Captures:
   `diff_text` (from artifact file), `diff_nonempty`, `verify_command`
   (from `task.created.payload`, may be None), `verify_command_exit`,
   `verify_command_stdout_tail`, `verify_command_duration_ms`. No
   `git apply --check` — Codex already applied the changes; re-applying
   the captured patch to the same tree fails. The handler returns
   `RawResult` with `result_semantics="observation"` per spec §3.4.11
   (canonical enum: ack | observation | verification | report | error);
   L3 emits `action.result_observed` from that RawResult.
5. **Result interpretation (L3)** — the L3 Result Interpreter consumes
   `action.result_observed` (carrying `result_semantics="observation"`
   from the verify_diff RawResult). It calls the L3 reviewer
   (`jarvis.decision.reviewer.review_diff`) and combines the
   reviewer verdict with the `verify_command` result per the F2 ladder
   (see § Evidence ladder). It emits `claim.created` (Postcondition or
   Limitation), `evidence.attached` with the correct `(relation, level)`
   pair per spec §8.6, and — only when level is `verified` —
   `task.verified`. Per spec §8.5 rule 6, missing evidence (e.g.
   `verify_command` absent) is itself recorded as a Limitation Claim,
   not silently ignored.
6. **Surface delivery (L5)** — voice channel → `say -v Tingting
   <voice_text>`; document channel → `osascript -e 'display notification
   <truncated> with title "Jarvis"'`; both append to event log via
   `surface.response_emitted` carrying a `delivered_via` audit field.
7. **Cost tracking** — every LLM call (L3 decision rounds + L3 reviewer)
   and every Codex turn emit `cost.recorded` with `kind`, `model`,
   `tokens_in`, `tokens_out`, optional cache counters, and `cost_usd`
   computed via the lifted `jarvis/state/pricing.py`.

### Pinned decisions

| # | Topic | Decision |
|---|---|---|
| D1 | Codex binary | OpenAI `codex` CLI, OAuth via `~/.codex/config.toml` |
| D2 | Time-window resolver | L3 LLM emits structured `{since_ts, until_ts}`; jarvis runs `SELECT` over event log |
| D3 | Verify strategy | Composite via spec §3.5.7 `post_action_check`: primary observation slot (diff capture) + post_action_check verification slot (`verify_command`, when present). L4 chains the check inline and returns dual RawResult slots; L3 emits one `action.result_observed` per slot. Reviewer LLM attaches a separate Report-level evidence row to the Postcondition Claim. Evidence ladder per F2 (revised) |
| D4 | Notification | `osascript display notification` banner + `say -v Tingting` TTS + CLI fork-detach (all three) |
| D5 | Workspace | Codex writes directly into target repo's working tree (no worktree isolation); dirty working tree handled per § Dirty-tree policy |
| D6 | TTS voice | `say -v Tingting`, mixed Chinese/English |
| D7 | CLI process model | Double-fork + `setsid` detach; ack precedes fork; SQLite never crosses fork |
| D8 | Codex model | `gpt-5.5` with `model_reasoning_effort=xhigh`, injected via `-c` flags at spawn (closes config-drift) |
| D9 | Reviewer LLM | OpenRouter + gpt-5.5 deep (same backbone as L3 decision; separate `cost.recorded` row, fresh context per review) |
| D10 | Sandbox | `sandbox_mode="workspace-write"` with `sandbox_workspace_write.writable_roots=[repo_path]`, all `-c`-injected |
| D11 | `verify_command` | Stored in `task.created.optional_payload`; default detection at task creation; runs in L4 with timeout 600s |
| D12 | Reviewer layer | L3 (not L4) — reviewer takes `LLMClient` which is L3-owned; locating reviewer in L4 violates `.importlinter` |
| D13 | Cost recording | One `cost.recorded` event per LLM call + per Codex turn; pricing table lifted verbatim from jarvis-legacy |
| D14 | Task creation | Natural-language path through L3 calling the new `create_task` L4 tool. No separate `jarvis task add` CLI |
| D15 | Worktree isolation | **NOT used** Day-2. Per spec.html §3.5 line 1207, workers share Mac filesystem; isolation is enforced by L4 SandboxPolicy + Codex's own `workspace-write` sandbox |

### Evidence ladder (verify outcome → evidence (relation, level))

Per spec.html §8.4 lines 1729–1738 (the canonical level ladder):
`reported → observed → executed → verified → accepted`. Per spec §8.6
line 1750–1763, every Evidence Record also carries a **relation**
(`supports | refutes | limits`) — `level` and `relation` are
independent dimensions and Day-2 emits both.

Per spec §3.4.11 line 714–722 the `result_semantics` of the RawResult
that feeds a claim **bounds the level**:
`observation → level=observed`, `verification → level=verified`,
`report → level=reported`. A reviewer LLM's verdict is a Report-grade
source (§8.5 rule 1: agent / LLM self-report → Report level only); it
**cannot** raise the level. Day-2 therefore composes `verify_diff` per
spec §3.5.7 (`post_action_check`): the primary RawResult slot carries
`result_semantics="observation"` (reading the diff), and — when the
task declares a `verify_command` — a chained slot carries
`result_semantics="verification"` (the `verify_command` exit predicate).
L3 emits one `action.result_observed` per slot (spec §5.4.2) and
interprets each slot under its own `result_semantics` row of §3.4.11.

Per spec §8.5 rule 6, missing evidence is itself recorded as a
Limitation Claim (with `relation=limits`), not silently ignored.

Each row below names the **evidence source** (diff observation /
verify_command verification / reviewer report). A single ladder
condition may emit several rows (one per source); they attach to one
or two claims (Postcondition + optional Limitation):

| Condition | Evidence source | Claim type | `relation` | `level` | `task.verified`? |
|---|---|---|---|---|---|
| `verify_command` present AND exits 0 AND reviewer.verdict=ok | `verify_command` (RawResult slot 2, `result_semantics=verification`) | Postcondition | supports | **verified** | **yes** |
| " " (same condition, second evidence row) | reviewer LLM (separate Report-grade source) | Postcondition | supports | reported | — |
| `verify_command` present AND exits 0 AND reviewer.verdict=fail | `verify_command` (slot 2, `result_semantics=verification`) | Postcondition | supports | verified | yes |
| " " (same condition, second evidence row) | reviewer LLM (Report source) | Postcondition | refutes | reported | — |
| " " (same condition, third row — §8.5 rule 6 contrast Limitation) | reviewer LLM | Limitation | limits | reported | — |
| `verify_command` present AND exits ≠ 0 | `verify_command` (slot 2, `result_semantics=error`) | Limitation | limits | executed | no |
| " " (same condition, second evidence row) | reviewer LLM (whatever verdict) | Limitation | limits | reported | — |
| `verify_command` absent AND `diff_nonempty` AND reviewer.verdict=ok | diff capture (slot 1, `result_semantics=observation`) | Artifact | supports | observed | no |
| " " (same condition, second evidence row) | reviewer LLM (Report source) | Artifact | supports | reported | — |
| " " (same condition, third row — §8.5 rule 6) | absence of `verify_command` | Limitation | limits | reported | — |
| `verify_command` absent AND `diff_nonempty` AND reviewer.verdict=fail | diff capture (slot 1, observation) | Artifact | supports | observed | no |
| " " (same condition, second evidence row) | reviewer LLM | Limitation | limits | reported | — |
| `diff_nonempty == False` (Codex produced nothing) | spawn_worker run (Execution Claim, executed) | Execution | supports | executed | no |
| " " (same condition, second evidence row — §8.5 rule 6) | absence of diff artifact | Limitation | limits | reported | — |

Notes:
- A Postcondition Claim at `level=verified` **only** comes from a
  RawResult whose `result_semantics="verification"` — i.e. the
  `verify_command` exit-code predicate inside the post_action_check
  chain. No other path can lift the level.
- When `verify_command` is absent, no Postcondition Claim is emitted;
  the strongest available evidence is an Artifact Claim at
  `level=observed` from the diff observation slot. `task.verified` is
  **not** derived. Spec §8.5 rule 6 then requires an explicit
  Limitation Claim recording the missing verification source.
- The reviewer LLM produces a **separate** Report-grade evidence row
  on whichever claim is active (Postcondition or Artifact). The
  reviewer's verdict modulates `relation` (supports / refutes) on that
  row; it never modulates `level` on any other row.
- `task.verified` derivation (spec §8.8) is unchanged: it fires only
  when a Postcondition Claim accumulates an Evidence row at
  `level=verified`. Reviewer-fail with `verify_command` passing still
  fires `task.verified` because the canonical proof is the
  `verify_command` exit; the reviewer's `refutes` row stays attached
  for audit.
- "Postcondition" claims always pair with a contrasting Limitation
  Claim whenever the postcondition is not fully met. Day-2 never
  silently promotes a partial result.

This explicitly honors ADR-0001 line 1021's promise: "Real Codex /
pytest integration replacing stubs — Stage 2."

### Canonical limitation phrasing

The Pre-emit Gate's force-limitation language regex set lives in a
single source of truth at `jarvis/decision/pre_emit_phrases.py`:

```python
# jarvis/decision/pre_emit_phrases.py
LIMITATION_REGEXES: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"reported,?\s*not\s+verified", re.IGNORECASE),
    re.compile(r"agent\s+reported"),
    re.compile(r"未验证"),
    re.compile(r"没验证"),
    re.compile(r"测试.{0,4}没过"),
    re.compile(r"还没验"),
)

COMPLETION_REGEXES: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"^完成"),
    re.compile(r"已完成(?!\s*报告)"),
    re.compile(r"\bverified\b", re.IGNORECASE),
    re.compile(r"\bdone\b", re.IGNORECASE),
)
```

ADR-0001 F4 / F5, ADR-0002 K5 / L3 all reference these constants; no
inline regex literals in tests. A canary (Tier 1) AST-scans test files
for inline limitation/completion regexes and fails them — three
divergent regex sets across docs is what created the F4/K5/L3 drift in
the first place.

### Reference sources

**Lifted verbatim (with attribution comment at file head):**

| Source | Target | Lines | License |
|---|---|---|---|
| `hermes-agent/repo/agent/transports/codex_app_server.py` | `jarvis/execution/codex_client.py` | ~399 | MIT (Hermes) |
| `jarvis-legacy/memory/cold/pricing.py` | `jarvis/state/pricing.py` | ~150 | Allen-owned |
| `jarvis-legacy/scripts/refresh_pricing.py` | `scripts/refresh_pricing.py` | ~150 | Allen-owned |
| `jarvis-legacy/tools_v2/helpers.py` | `jarvis/execution/_helpers.py` | ~60 | Allen-owned |

**Architectural references (read, not lifted):**

- `hermes-agent/repo/agent/transports/codex_app_server.py` lines 75–130
  (in particular the `app_server_args.extend([...])` block at
  lines 89–111) — `-c` sandbox + MCP-server flag injection pattern
  (see § Codex contract, § submit_report tool injection).
- `hermes-agent/repo/hermes_cli/codex_runtime_plugin_migration.py:557-605`
  — canonical TOML shape for an `mcp_servers.<name>.*` entry (command /
  args / env / startup_timeout_sec / tool_timeout_sec). Jarvis mirrors
  this shape via `-c` flags rather than writing to the user's
  `~/.codex/config.toml`.
- `hermes-agent/repo/agent/transports/hermes_tools_mcp_server.py` —
  reference implementation of a hand-rolled MCP stdio JSON-RPC server
  (no third-party `mcp` PyPI dependency); `jarvis/execution/codex_mcp_tools.py`
  follows the same framing.
- `hermes-agent/repo/agent/transports/codex_app_server_session.py` —
  one-shot driver shape, take_notification loop semantics.
- `hermes-agent/repo/hermes_cli/kanban_db.py:4080-4215` — `_default_spawn`
  env-var sandbox pattern + detached-child + zombie-reap.
- `jarvis-legacy/core/media_ducking.py:151-159` — `_run_osascript` 6-line
  shape, single-quote escaping.

**Explicitly NOT lifted (rationale):**

- Hermes `CodexAppServerSession` — multi-turn AIAgent loop, MCP
  elicitation, approval bridge. Jarvis has none of those concerns Day-2.
- Hermes Kanban dispatcher (5k LOC) — priority queues / parent links /
  board namespaces jarvis doesn't need.
- Hermes `codex_event_projector` — projects to OpenAI messages list;
  jarvis has no such surface.
- Legacy `core/tts.py` (MiniMax WS) — overkill for one-shot `say`.
- Legacy `tools_v2/registry.py` — Day-1 already adapted; no further lift.

### What is explicitly discarded (not deferred)

- Multi-tool ecosystem (only `create_task` + `spawn_worker` + `verify_diff`)
- OBSERVER principal twin-verify (reviewer is in-process LLM, not a
  separate principal)
- Multi-task disambiguation (multi-match → `unknown_subject` fail-fast)
- Voice input (Whisper, etc.)
- Push to phone
- LLM / Codex provider failover
- Daemon mode via `launchd` plist (Day-2 uses ad-hoc fork-detach;
  sleep/wake IS implemented via IOPM observer per spec §3.7.8)
- Real-time event UI / dashboard
- Memory / preference learning
- Budget enforcement (only record cost, no cap)
- Caching of LLM responses
- Heartbeat back from detached child to original CLI (child is fully
  detached; parent exited)
- `git apply --check` in verify path (see F3: Codex already applied the
  patch; re-applying it to the same tree fails)
- Manual `claim.accepted` override flow — out of scope; verify_command-less
  tasks **never** automatically reach `verified` evidence (deferred to
  Day-N)
- Codex subprocess sleep-bridging — Day-2 fails closed via
  `worker.terminated_by_sleep` (spec §3.7.8); resuming Codex through a
  sleep is out of scope.

---

### Codex contract (L4)

```python
# jarvis/execution/codex_action.py (sketch)
@dataclass(frozen=True)
class CodexActionResult:
    final_text: str
    diff_text: str          # output of `git -C cwd diff`, may be empty
    diff_path: Path | None  # artifact path; None if diff empty
    turn_id: str | None
    error: str | None
    interrupted: bool
    tokens_in: int          # from turn/completed payload
    tokens_out: int
    elapsed_ms: int

def run_codex_action(
    *,
    task_goal: str,
    cwd: Path,
    timeout_s: float = 600.0,
    on_heartbeat: Callable[[dict], None] | None = None,
) -> CodexActionResult: ...
```

Spawn-time `-c` flag injection (closes config-drift per F9; mirrors
Hermes pattern at `agent/transports/codex_app_server.py:75-130`):

```python
extra_args = [
    "-c", "model=gpt-5.5",
    "-c", "model_reasoning_effort=xhigh",
    "-c", "sandbox_mode=workspace-write",
    "-c", f'sandbox_workspace_write.writable_roots={_toml_list_quote(cwd)}',
    # submit_report MCP server injection — see § submit_report tool
    # injection. The `-c mcp_servers.<name>.X=...` keys layer onto the
    # user's ~/.codex/config.toml in-memory without touching the file.
    "-c", f'mcp_servers.jarvis-tools.command="{_toml_str(sys.executable)}"',
    "-c", 'mcp_servers.jarvis-tools.args=["-m","jarvis.execution.codex_mcp_tools"]',
    "-c", "mcp_servers.jarvis-tools.startup_timeout_sec=30.0",
    "-c", "mcp_servers.jarvis-tools.tool_timeout_sec=600.0",
]
env = os.environ.copy()
env["RUST_LOG"] = "warn"
client = CodexAppServerClient(codex_bin="codex", extra_args=extra_args, env=env)
```

These flags **override** `~/.codex/config.toml`. Allen-managed config
drift no longer affects jarvis behavior. The `mcp_servers.jarvis-tools.*`
keys are *additive*: they layer onto whatever user-defined MCP servers
already exist in `~/.codex/config.toml`, they do not replace them. Per
Hermes' validated pattern at `codex_runtime_plugin_migration.py:557-605`
the canonical TOML shape is `mcp_servers.<server-name>.{command, args,
env, startup_timeout_sec, tool_timeout_sec}`; jarvis uses the same
shape via `-c` flags so nothing on disk is mutated.

`_toml_list_quote(cwd)` produces a TOML-safe list literal — paths with
spaces, quotes, or non-ASCII characters must be properly escaped before
being injected into the `-c` argument. A naive f-string with raw
backslash/quote in cwd will produce malformed TOML and Codex will fail
to start. The helper escapes per TOML spec (basic strings) and is
unit-tested against ~10 weird-path fixtures. `_toml_str(s)` produces a
TOML-safe basic-string literal (an escaped, double-quoted `"..."`) used
for scalar string values such as the MCP server's `command` path; both
helpers share the same TOML basic-string escape table and are unit-tested
against the same weird-path fixture set.

Lifecycle:

1. `CodexAppServerClient(...).initialize()` (timeout 5s). The
   `submit_report` MCP server is **already injected at spawn time via
   the `-c mcp_servers.jarvis-tools.*` flags above** (see § submit_report
   injection below); Codex discovers the `submit_report` tool through
   its standard `tools/list` MCP handshake during initialize.
2. `request("thread/start", {"cwd": str(cwd)})` → `thread_id`.
3. `request("turn/start", {"threadId": ..., "input": [{"type":"text",
   "text": task_goal}]})`.
4. Loop `take_notification(timeout=0.25)` until `method ==
   "turn/completed"` or deadline.
5. Every 30s wall-clock → `on_heartbeat({summary, elapsed_ms,
   last_item_summary})`.
6. On `turn/completed` AND a captured `submit_report` tool call → use
   the worker's structured report payload directly. If no
   `submit_report` was called by Codex during the turn → emit
   `worker.report_missing` + Limitation Claim (per spec §3.5.8).
7. `close(timeout=3.0)`.

Pre-flight check at handler registration: `codex --version` parses to
≥ 0.125.0 or refuse to register the handler (raises
`CodexVersionTooLow`).

### `submit_report` tool injection (spec §3.5.8)

Spec §3.5.8 mandates that the worker wrapper injects a `submit_report`
tool requiring the worker to return a structured `WorkerReport`:

```
WorkerReport:
  status              # ok | partial | failed | blocked
  summary
  changed_files
  commands_run
  tests_run
  evidence_submitted
  remaining_risks
  needs_human_review
  next_recommended_action
```

Hermes does not implement `submit_report` (jarvis-spec-specific
contract); jarvis must add it. Codex's app-server protocol surface is
exactly `initialize`, `thread/start`, `turn/start`, `turn/interrupt` —
there is **no runtime tool-registration JSON-RPC**. Tools are wired in
at spawn time, the same way `~/.codex/config.toml` does it. Day-2
therefore implements the spec §3.5.8 mandate by running `submit_report`
as a **standalone Python MCP stdio server subprocess** and injecting
its config via `-c mcp_servers.jarvis-tools.*` spawn flags — the same
`-c` pattern already used for sandbox mode (see Codex contract above
and Hermes' `codex_app_server.py` lines 89–111 for the validated
shape).

```python
# jarvis/execution/codex_mcp_tools.py — stdio MCP server (~80 lines)
#
# Spawned by Codex (NOT by jarvis directly) per the -c mcp_servers
# spawn flags. Speaks the MCP stdio JSON-RPC dialect: handles
# initialize / tools/list / tools/call. Exposes exactly ONE tool:
# submit_report. Follows the hand-rolled stdio JSON-RPC framing
# pattern used in Hermes' agent/transports/hermes_tools_mcp_server.py
# (no third-party `mcp` PyPI dependency — JSON-RPC over stdio is small
# enough that adding a framework would be net negative).

SUBMIT_REPORT_TOOL = {
    "name": "submit_report",
    "description": (
        "Submit a final structured report. You MUST call this exactly "
        "once before ending the turn. The report is how Jarvis reads "
        "your result; without it the run is marked report_missing."
    ),
    "input_schema": {
        "type": "object",
        "required": ["status", "summary"],
        "properties": {
            "status": {"enum": ["ok", "partial", "failed", "blocked"]},
            "summary": {"type": "string"},
            "changed_files": {"type": "array", "items": {"type": "string"}},
            "commands_run": {"type": "array", "items": {"type": "string"}},
            "tests_run": {"type": "array", "items": {"type": "string"}},
            "evidence_submitted": {"type": "array", "items": {"type": "string"}},
            "remaining_risks": {"type": "string"},
            "needs_human_review": {"type": "boolean"},
            "next_recommended_action": {"type": "string"},
        },
    },
}

# Bound at spawn time via the four -c flags shown in § Codex contract:
#   -c 'mcp_servers.jarvis-tools.command="<sys.executable>"'
#   -c 'mcp_servers.jarvis-tools.args=["-m","jarvis.execution.codex_mcp_tools"]'
#   -c 'mcp_servers.jarvis-tools.startup_timeout_sec=30.0'
#   -c 'mcp_servers.jarvis-tools.tool_timeout_sec=600.0'
# Codex spawns the subprocess on initialize, runs MCP `tools/list`, and
# from that moment on `submit_report` is selectable inside the turn.
```

**Why injection is config-flag, not runtime RPC.** Codex 0.125+'s
app-server has a closed, declarative protocol surface
(`initialize | thread/start | turn/start | turn/interrupt` — confirmed
by reading Hermes' `agent/transports/codex_app_server.py` 1–399 lines
and by `codex --help` on 0.125.0). There is no `mcp/register_tool`
JSON-RPC method, nor any sibling on the `tool/*` namespace. MCP servers
are bound at startup from `~/.codex/config.toml`'s `mcp_servers` table,
and `-c key=value` flags layer onto that table in-memory at spawn time.
Jarvis exploits the latter so the user's `~/.codex/config.toml` stays
untouched (Allen owns that file). This is the same `-c` mechanism used
for `sandbox_mode` and `sandbox_workspace_write.writable_roots` above —
extending it to `mcp_servers.jarvis-tools.*` is one-line per key. The
canonical TOML shape for an MCP server entry comes from Hermes'
`hermes_cli/codex_runtime_plugin_migration.py:557-605`.

**Enforcement model: prompt pressure + post-turn guard.** Codex does
**not** expose a "forced tool-call" primitive — there is no protocol
knob that compels the worker to call `submit_report`. Day-2 therefore
enforces the spec §3.5.8 contract in two layers:

1. *Prompt pressure* — the tool's `description` string (above) carries
   the "you MUST call this exactly once" language. The system prompt
   in `codex_action.py` repeats the rule. This pulls correct behaviour
   in the common case.
2. *Post-turn guard (jarvis-side, deterministic)* — the L4
   `take_notification(...)` loop watches every `item/tool_call`
   notification during the turn. When `tool_call.tool_name ==
   "submit_report"`, it captures the structured arguments as the
   canonical `WorkerReport` payload. After `turn/completed` the
   handler asserts at least one such capture; if zero, it emits
   `worker.report_missing` + a Limitation Claim per spec §3.5.8 last
   bullet ("Worker report 永远只是 Report Claim; Jarvis 还要自己检查
   artifact / test / postcondition"). The worker's natural-language
   `final_text` is **not** used as a substitute.

The `git diff` capture and `verify_command` execution in L4 are
jarvis's independent evidence and run regardless of whether
`submit_report` was called — they are not part of the worker report
path.

### Dirty-tree policy

Codex writes directly into the user's working tree (D5). If the tree
already has uncommitted changes when spawn_worker fires, `git -C cwd
diff` will conflate Allen's prior work with Codex's edits — evidence
pollution. L4 handles this with a deterministic auto-stash:

```python
# jarvis/execution/diff_capture.py — auto-stash sketch
def isolate_pretask_changes(cwd: Path) -> str | None:
    """Stash any uncommitted changes before Codex runs.

    Returns the stash ref (e.g. "stash@{0}") if anything was stashed,
    else None. Spawn_worker stashes via:
        git -C cwd stash push -u -m "jarvis-pre-codex-<run_id>"
    """

def restore_pretask_changes(cwd: Path, stash_ref: str | None) -> None:
    """Pop the stash after Codex completes. On conflict, write the
    stash as a patch artifact (~/.jarvis/artifacts/run_<R>/conflict.patch)
    and emit worker.artifact_observed with kind=stash_conflict."""
```

The stash is the auto-restored after `diff_capture` runs (the diff
already reflects only Codex's changes because the working tree was
clean at spawn-time). If `git stash pop` conflicts, the stash is
preserved as an artifact and surfaced via Limitation Claim — Allen
manually reconciles. **Never silently overwrite Allen's work.**

### Reviewer contract (L3 helper)

```python
# jarvis/decision/reviewer.py (sketch)  -- L3 location per F4
@dataclass(frozen=True)
class ReviewerVerdict:
    verdict: Literal["ok", "fail"]
    reasons: tuple[str, ...]
    tokens_in: int
    tokens_out: int
    model: str

def review_diff(
    *,
    task_goal: str,
    diff_text: str,
    llm_client: LLMClient,
) -> ReviewerVerdict: ...
```

System prompt (≤ 1500 chars, English): asks for structured JSON
`{"verdict": "ok"|"fail", "reasons": [...]}`. Includes the
"claim ≤ evidence" guardrail (spec.html §13 I10). Reviewer **must not**
see jarvis's session history — fresh context per review.

`tool_choice=None`, `temperature` from preset, structured-output mode if
available (else `response_format={"type":"json_object"}`).

Reviewer is invoked from the L3 Result Interpreter, **not** from the L4
`verify_diff_handler`. L4 returns raw observations only; L3 forms
Evidence.

**Reviewer verdict never raises evidence level.** The reviewer is an
LLM, and spec §8.5 rule 1 classifies agent / LLM self-report as a
Report-grade source: `level=reported` is the ceiling for any evidence
row sourced from the reviewer. The reviewer's verdict therefore
contributes a **separate** evidence row, attached to whichever claim
the diff/verify slots already produced, with `level=reported` and
`relation` set to `supports` (verdict=ok) or `refutes` (verdict=fail).
It never modulates the `level` of the verify_command-sourced row, nor
of the diff-observation-sourced row. This aligns with spec §13.2 I8
("evidence sources are typed; agent reports cannot promote tool
verification") and §13.2 I11 ("level is bounded by source class").
See § Evidence ladder for the full per-source mapping.

### Verify_diff contract (L4 observation + post_action_check chain, per spec §3.4.11 + §3.5.7)

`verify_diff`'s ToolDefinition carries a **primary** `result_semantics=
"observation"` plus a **post_action_check** declaration per spec §3.5.7
(only when the bound task has a `verify_command`; otherwise the
post_action_check field is omitted):

```python
# jarvis/execution/tools.py — verify_diff ToolDefinition
VERIFY_DIFF_TOOL_DEF = ToolDefinition(
    name="verify_diff",
    description="Read the worker's diff artifact and (optionally) run a verify command to check postcondition.",
    input_schema={...},
    caller_principals_allowed=("jarvis_llm",),
    read_only=False,                     # verify_command may have side effects
    destructive=False,
    risk_level="L2",
    action_type="verify",
    result_semantics="observation",      # primary slot: diff capture
    timeout_default=620.0,                # 600s verify + 20s slack
    post_action_check=PostActionCheck(   # spec §3.5.7
        mode="inline",                   # chain in the same L4 invocation
        check_tool="verify_command",     # not a tool ref; sentinel name for the inline subprocess
        expected_predicate="exit_code == 0",
        result_semantics_on_match="verification",
        timeout_ms=600_000,
    ),                                    # set to None at runtime when task.verify_command is None
    ...
)
```

The handler returns **two RawResult slots** in a single bundle: slot 1
is the diff observation, slot 2 is the post_action_check verification
(only when `verify_command` is non-None — when None, slot 2 is omitted
and L3 sees only the observation slot).

```python
# jarvis/execution/tools.py — verify_diff_handler sketch
@dataclass(frozen=True)
class VerifyDiffSlots:
    # Slot 1 — primary observation slot (spec §3.4.11 → level=observed).
    observation: "DiffObservationSlot"
    # Slot 2 — post_action_check chained slot, may be None when the
    # task carries no verify_command. When present, its result_semantics
    # is "verification" on match or "error" on non-zero exit (spec
    # §3.5.7 result_semantics_on_match; §3.4.11 error row).
    verification: "VerifyCommandSlot | None"

@dataclass(frozen=True)
class DiffObservationSlot:
    diff_text: str
    diff_nonempty: bool
    diff_artifact_ref: str            # artifact_path
    # result_semantics is constant: "observation"

@dataclass(frozen=True)
class VerifyCommandSlot:
    verify_command: str
    exit_code: int
    stdout_tail: str                  # last ~2KB
    duration_ms: int
    # result_semantics = "verification" iff exit_code == 0, else "error".

def verify_diff_handler(action_request: ActionRequest) -> RawResultBundle:
    """L4 observation handler with inline post_action_check chain.

    1) Read diff.txt artifact → DiffObservationSlot
       (result_semantics="observation"). NO `git apply --check`
       (Codex already applied the patch in-place).
    2) If task.verify_command is non-None, run it INLINE:
         subprocess.run(
             ["/bin/sh", "-c", verify_command],
             cwd=repo_path,
             timeout=600,
             capture_output=True,
             text=True,
             check=False,
         )
       Build VerifyCommandSlot from (exit_code, stdout_tail,
       duration_ms). Set result_semantics on the slot to "verification"
       when exit_code == 0, else "error".
    3) Return RawResultBundle with both slots.

    NO reviewer call. NO verdict. L3 emits ONE action.result_observed
    PER slot (spec §5.4.2), each tagged with its own result_semantics
    (§3.4.11), so the Result Interpreter applies the correct ladder
    row per slot.
    """
```

**Trust model for `/bin/sh -c verify_command`.** `verify_command` is
Allen-authored at `task.created` time via the natural-language
`create_task` path (D-1 session); jarvis treats it as trusted-by-Allen,
the same trust class as the repo's local test command. No untrusted
source can write `verify_command` Day-2 (no remote ingest, no shared
task ledger, no cross-domain import). When Stage 2 adds cross-domain
task import, the trust boundary moves to the importer + Pre-action
Gate's `caller_principal` check, and `verify_command` strings on
imported tasks must be quarantined (refused or re-authored locally)
before the verify_diff handler ever sees them.

L3 Result Interpreter consumes both `action.result_observed` events
for this action — one with `result_semantics="observation"` (diff
slot), one with `result_semantics="verification"` or `"error"`
(post_action_check slot, when present). It calls `review_diff(...)`
once on the captured diff text, then forms claims + evidence per the
F2 ladder (one evidence row per source). The verification-slot row is
the **only** path that can produce a Postcondition Claim at
`level=verified`, in keeping with spec §3.4.11.

Implementation note: jarvis uses `RawResultBundle` (a tuple of
RawResult slots) rather than a single flattened RawResult so that the
two slots can carry distinct `result_semantics` values into distinct
`action.result_observed` events — spec §3.5.4 line 864 explicitly
allows "per-output mapping" and §3.5.7 inline chaining requires it.
The single-RawResult shape used by ack-only tools is unchanged; the
bundle is only used by tools that declare `post_action_check`.

### Notification contract (L5)

```python
# jarvis/surface/notify.py (sketch)
def deliver_voice(text: str, *, voice: str = "Tingting") -> None:
    """Fire-and-forget `say` subprocess. Returns immediately."""
    subprocess.Popen(["say", "-v", voice, text], stdin=subprocess.DEVNULL, ...)

def deliver_banner(title: str, body: str, *, max_body_chars: int = 240) -> None:
    """Synchronous `osascript display notification`. Truncates body."""
    body_truncated = (body[:max_body_chars] + "…") if len(body) > max_body_chars else body
    script = f'display notification {_apple_escape(body_truncated)} with title {_apple_escape(title)}'
    subprocess.run(["/usr/bin/osascript", "-e", script], check=False, timeout=2)
```

### Attention channel → physical surface mapping

Per spec §3.4.7 + §12.4 the **Attention Policy** outputs one of 10
logical channels (Mac-only architecture drops `ambient_act`, leaving
9). Per spec §3.1 + §3.4.7 line 674 ("physical surface mapping 在
Layer 5"), L5 maps those logical channels to physical delivery
surfaces. Day-2 mapping:

| Attention channel (L3 output) | Physical surfaces (L5) | Notes |
|---|---|---|
| `silent_log` | (none) | Event appended, no user-visible side-effect |
| `queue_review` | `cli_stdout` (when attached) | Appended to Recent Trace; no banner, no voice |
| `badge_card` | `osascript_banner` (title-only, no body) | Day-2 has no Inherent panel; banner is the badge surrogate |
| `soft_suggest` | `cli_stdout` | Appended only; no proactive sound |
| `voice_notify` | `say` + `osascript_banner` + `cli_stdout` | The flagship channel for the scenario |
| `interrupt_now` | `say` (with leading bell tone) + `osascript_banner` | Same surfaces as voice_notify Day-2; differentiated only by phrasing |
| `ask_confirm` | `osascript_banner` + `cli_stdout` (prompt) | Confirmation re-entry happens via next CLI invocation Day-2 |
| `delegate_agent` | (none — internal action) | Emits ActionRequest, no user-visible surface |
| `suppress` | (none) | Event appended with reason; no surface call |

Voice-channel text → `deliver_voice`. Document-channel text →
`deliver_banner` (truncated) + appended to CLI stdout if still attached
+ appended to event log via `surface.response_emitted` carrying
`delivered_via=["voice","banner","stdout"]` (or a subset if partial,
e.g. `["banner"]` when CLI parent has detached). The `delivered_via`
list records **physical surfaces actually written to**, not Attention
channels — Attention channel goes in `attention_channel` field of the
same event payload.

### Daemon / CLI contract (L6)

```python
# jarvis/runtime/daemon.py (sketch)
def fork_detach() -> Literal["parent", "child"]:
    """Double-fork + setsid. Parent returns 'parent' immediately; caller
    must `os._exit(0)`. Child returns 'child', stdin/stdout/stderr
    redirected to /dev/null, cwd=/, reparented to init."""

# Cheap LLM-free classifier
_LONG_RUN_RE = re.compile(r"跑|spawn|给 *codex|帮我做|帮我跑|做一下|审核|run", re.IGNORECASE)

def _utterance_implies_long_run(utterance: str) -> bool:
    """Day-2 classifier: regex keyword match only. NO SQLite open.
    NO task-bound check (resolver runs inside run_turn, after fork)."""
    return bool(_LONG_RUN_RE.search(utterance))
```

Caller pattern (CLI entrypoint):

```python
# jarvis/cli/__init__.py (sketch)
def main_with_detach(utterance: str) -> int:
    # 1. Parse utterance — no SQLite open, no runtime bootstrap yet.
    if _utterance_implies_long_run(utterance):
        # 2. Print quick ack (no LLM, regex-templated phrase).
        print(_quick_ack_phrase(utterance))  # e.g. "好的，跑起来了。"
        sys.stdout.flush()
        # 3. Fork-detach.
        if fork_detach() == "parent":
            os._exit(0)
        # Child continues: re-bootstrap runtime in detached process.
        runtime = bootstrap_runtime_app(...)
        result = run_turn(runtime, utterance=utterance)
        _deliver_surface(result)
        return 0
    # Synchronous (non-long-run) path: Day-1 behavior preserved.
    runtime = bootstrap_runtime_app(...)
    result = run_turn(runtime, utterance=utterance)
    _deliver_surface(result)
    return 0
```

**Hard rules:**

- `bootstrap_runtime_app` is **never** called before `fork_detach()`. The
  SQLite connection (and any open file descriptor) must never cross the
  fork. The parent process holds nothing but utterance parsing + the
  regex classifier + the ack print + the fork call.
- The classifier is **not** task-bound. At the fork decision point, L3
  resolver hasn't run yet, so an active task isn't bound. The regex
  alone routes long-run vs. synchronous.
- The ack is **always** printed before the fork (canary H15).

### Sleep / wake protocol (spec §3.7.8)

A 3-minute Codex turn easily spans a Mac sleep event. Per spec §3.7.8
the detached child MUST observe sleep/wake and reconcile in-flight
state. Day-2 implements the Mac-only minimum (cross-domain publication
deferred, see deviation V4):

```python
# jarvis/deployment/sleep_wake.py (sketch)
def install_power_observer(event_log: EventLog) -> PowerObserver:
    """Register macOS IOPM notification handler.

    Before-sleep callback (best-effort, may not fire if power is yanked):
      - emit mac.sleeping(reason, in_progress_actions=[...]) per §3.7.8
      - flush events (event log fsync)
      - for each running spawn_worker run: emit
        worker.suspended_by_sleep(run_id, action_id, last_heartbeat_ts)

    On-wake callback:
      - emit mac.awake(slept_for_ms)
      - run reconcile_after_wake() — scan open actions / runs:
          * if Codex subprocess PID is dead: emit
            worker.terminated_by_sleep(run_id, reason="subprocess_lost_to_sleep")
            then emit action.timeout_assumed(action_id, reason="lost_to_sleep")
            and a Limitation Claim
          * if subprocess somehow survived: resume heartbeat loop
    """

def reconcile_after_wake(event_log: EventLog, projection: Projections) -> int:
    """Idempotent reconciliation. Returns number of closed actions.
    Safe to invoke multiple times — uses (action_id, terminal_event)
    dedup to avoid double-closure."""
```

The Codex subprocess almost always dies through a sleep — macOS power
management doesn't preserve subprocess sockets/pipes across deep sleep.
The reconciliation path is the spec's mandated fail-closed behavior:
emit `worker.terminated_by_sleep` + Limitation Claim, never silently
mark the action complete.

The detached child registers the observer at bootstrap (after fork,
before run_turn). Tests stub the observer with a fake clock to
simulate sleep events deterministically.

### Day-2 EventTypeRegistry extensions

Append to `_REGISTRY_ENTRIES` in `jarvis/state/event_log.py` (12 new
entries):

```python
EventTypeSchema(
    event_type="worker.heartbeat",
    owner_layer="L4",
    required_payload=("run_id", "action_id"),
    optional_payload=("elapsed_ms", "last_log_line", "summary"),
    schema_version=1,
),
EventTypeSchema(
    event_type="worker.artifact_observed",
    owner_layer="L4",
    required_payload=("run_id", "action_id", "artifact_path"),
    optional_payload=("content_hash", "kind"),
    schema_version=1,
),
EventTypeSchema(
    event_type="worker.report_missing",
    owner_layer="L4",
    required_payload=("run_id", "action_id"),
    optional_payload=("reason",),
    schema_version=1,
),
EventTypeSchema(
    event_type="task.executor_assigned",
    owner_layer="L3",
    required_payload=("task_id", "executor", "action_id"),
    optional_payload=("model",),
    schema_version=1,
),
EventTypeSchema(
    event_type="task.executor_reported",
    owner_layer="L4",
    required_payload=("task_id", "run_id", "status"),
    optional_payload=("summary", "diff_path"),
    schema_version=1,
),
EventTypeSchema(
    # owner_layer is L3 per spec §5.4.1 (single-value owner_layer).
    # L4 returns cost data inside RawResult (codex turn tokens come from
    # Codex's turn/completed payload, attached to spawn_worker's
    # RawResult.metadata.cost); L3 reads that and emits cost.recorded.
    # L4 never emits this event directly — see spec §5.4.2 pattern where
    # L4 returns RawResult and L3 emits action.result_observed.
    event_type="cost.recorded",
    owner_layer="L3",
    required_payload=("kind", "model"),
    optional_payload=("tokens_in", "tokens_out", "cache_read_in",
                      "cache_write_in", "cost_usd", "run_id"),
    schema_version=1,
),
# F6: surface.user_intent — spec.html §5.4 line 1442 canonical;
# missing from Day-1 registry, restored here.
EventTypeSchema(
    event_type="surface.user_intent",
    owner_layer="L5",
    required_payload=("transcript", "turn_id"),
    optional_payload=("channel", "language"),
    schema_version=1,
),
# F6: surface.response_emitted — NOT in spec §5.4 canonical list
# (deviation V1 above); Day-2 audit field for delivered_via +
# attention_channel.
EventTypeSchema(
    event_type="surface.response_emitted",
    owner_layer="L5",
    required_payload=("turn_id", "text"),
    optional_payload=("delivered_via", "attention_channel",
                      "voice_text", "document_text", "response_hash"),
    schema_version=1,
),
# Sleep/wake events per spec §3.7.8 — L6 owned (Deployment Domain).
EventTypeSchema(
    event_type="mac.sleeping",
    owner_layer="L6",
    required_payload=("ts_epoch_ms",),
    optional_payload=("reason", "in_progress_action_ids"),
    schema_version=1,
),
EventTypeSchema(
    event_type="mac.awake",
    owner_layer="L6",
    required_payload=("ts_epoch_ms", "slept_for_ms"),
    optional_payload=("reconciliation_summary",),
    schema_version=1,
),
EventTypeSchema(
    event_type="worker.suspended_by_sleep",
    owner_layer="L6",
    required_payload=("run_id", "action_id"),
    optional_payload=("last_heartbeat_ts",),
    schema_version=1,
),
EventTypeSchema(
    event_type="worker.terminated_by_sleep",
    owner_layer="L6",
    required_payload=("run_id", "action_id"),
    optional_payload=("reason",),
    schema_version=1,
),
```

**Amend Day-1 leftover** (ADR-0001 F1 review tail):

```python
# action.result_observed: append error_payload to optional_payload
optional_payload=("tool_output", "error", "run_id", "error_payload"),
```

**Amend `task.created`** (F7 — `goal` stays required; `repo_path` and
`verify_command` join the optional set; `owner_layer` semantically
transitions from "fixture-seeded in Day-1" to "L4-emitted in Day-2" —
in Day-1 the only event source was a hand-seeded test fixture, in
Day-2 the `create_task` L4 tool emits this event in response to a
JARVIS_LLM ActionRequest, so the registry's `owner_layer="L4"`
declaration first becomes load-bearing here):

```python
# task.created (existing entry, amended)
owner_layer="L4",                                              # unchanged on disk; first real emit-site lands in Day-2 Step 4 (create_task)
required_payload=("task_id", "goal"),                          # UNCHANGED from Day-1
optional_payload=("source", "deadline", "repo_path", "verify_command"),  # +repo_path, +verify_command
```

**Amend `claim.created`** (F8 — add `relation` requirement so the
`(relation, level)` pair from spec §8.6 is always carried on the
associated `evidence.attached`):

```python
# evidence.attached (existing entry, amended)
required_payload=("evidence_id", "claim_id", "relation", "level"),  # +relation
optional_payload=("source_type", "source_id", "observed_at",
                  "freshness", "scope", "summary", "artifact_ref",
                  "limitations"),
```

### Module map (delta from Day-1)

**New files:**

| Path | Lines | Source |
|---|---|---|
| `jarvis/execution/codex_client.py` | ~399 | Lift from Hermes |
| `jarvis/execution/codex_action.py` | ~120 | New (incl. submit_report injection via `-c mcp_servers.jarvis-tools.*` spawn flags) |
| `jarvis/execution/codex_mcp_tools.py` | ~80 | New — stdio MCP server subprocess exposing one tool (`submit_report`). Hand-rolled JSON-RPC stdio framing, follows Hermes' `agent/transports/hermes_tools_mcp_server.py` pattern. Spawned by Codex itself per the `-c mcp_servers.jarvis-tools.*` injection (see § submit_report tool injection). No third-party `mcp` PyPI dependency |
| `jarvis/decision/reviewer.py` | ~100 | New (**L3** per F4) |
| `jarvis/decision/pre_emit_phrases.py` | ~30 | New (single source of regexes) |
| `jarvis/execution/diff_capture.py` | ~80 | New (incl. dirty-tree auto-stash) |
| `jarvis/execution/_helpers.py` | ~60 | Lift from legacy |
| `jarvis/shared/pricing.py` | ~150 | Lift from legacy (moved from L2 — pricing is a static reference, not an event-folded projection; lives in `jarvis/shared/` so both L3 and L4 can import without crossing layer DAG) |
| `jarvis/surface/notify.py` | ~30 | New |
| `jarvis/runtime/daemon.py` | ~40 | New |
| `jarvis/deployment/sleep_wake.py` | ~120 | New (spec §3.7.8 power-observer + reconciliation) |
| `data/pricing.json` | (data) | Lift from legacy |
| `scripts/refresh_pricing.py` | ~150 | Lift from legacy |

**Edits to existing files:**

- `jarvis/state/event_log.py` — register 12 new event types + amend
  optional/required_payload tuples for `action.result_observed`,
  `task.created`, `evidence.attached`.
- `jarvis/state/projections.py` — Task Ledger projection gains
  `tasks_in_window(since_ts, until_ts) -> list[TaskId]` method so L3
  resolver can query by time window via the projection API (spec
  §3.4.3 requires L3 to read L2 through projection snapshots only —
  direct `SELECT` over the events table from L3 is a layer violation).
- `jarvis/execution/tools.py` — replace `spawn_worker_handler` body
  (real Codex + submit_report MCP server injection via `-c
  mcp_servers.jarvis-tools.*` flags + heartbeat loop + sandbox
  `-c` injection + dirty-tree auto-stash); replace `verify_diff_handler`
  body with the **dual-slot** observation + post_action_check handler
  (returns `RawResultBundle` with `observation` slot 1 + `verification`
  / `error` slot 2 when `verify_command` is non-None; otherwise slot 1
  only); declare `VERIFY_DIFF_TOOL_DEF.post_action_check` per spec
  §3.5.7; **no reviewer call inside L4**; register new
  `create_task_handler` with `verify_command` detection.
- `jarvis/decision/__init__.py` — emit `cost.recorded` after every LLM
  call in `decide()`; consume RawResult.metadata.cost from L4 and emit
  `cost.recorded(kind=codex)` there too (L3 is the sole emit-site per
  registry owner_layer=L3); wire the **dual-slot** Result Interpreter
  for `verify_diff` — for each `action.result_observed` event tagged
  with the relevant action_id, branch on `result_semantics`:
  `observation` slot triggers the diff-artifact / Artifact-Claim path
  AND the reviewer LLM call (producing a separate Report-grade
  evidence row); `verification` slot produces a Postcondition Claim at
  `level=verified` and emits `task.verified`; `error` slot (verify_command
  non-zero exit) produces a Limitation Claim at `level=executed`,
  never `task.verified`. Form Evidence `(relation, level)` per the F2
  ladder (one row per source); populate ResponsePlan with canonical
  `output_risk_class` + `required_gate_mode` per spec §3.4.13.
- `jarvis/decision/llm.py` — `ChatResult` carries `model_used` + cache
  token counts. New `fresh_context()` context manager guarantees the
  reviewer's `chat()` calls do not share session history with the
  decision LLM (canary `test_canary_reviewer_fresh_context` enforces).
- `jarvis/decision/resolver.py` — calls
  `task_ledger.tasks_in_window(since_ts, until_ts)`; **no direct SQL,
  no `SELECT events`**.
- `jarvis/runtime/__init__.py` — composition root still owns wiring;
  re-bootstrap path for detached child; installs
  `jarvis.deployment.sleep_wake.install_power_observer(...)` in the
  detached child after bootstrap.
- `jarvis/cli/__init__.py` — fork-detach entry point + ack-before-fork
  + child re-bootstrap.
- `jarvis/surface/cli.py` — `emit_utterance_received` is renamed to
  `emit_surface_user_intent` and emits `surface.user_intent` instead
  of `utterance.received` per spec §3.4.1 trigger taxonomy. Day-1's use
  of `utterance.received` for CLI input was a Day-1 deviation now
  corrected. `utterance.received` stays in registry (reserved for
  future ASR/voice surface) but is unused on the CLI path Day-2.
- `jarvis/decision/__init__.py:412 / :430 / :439`,
  `jarvis/decision/intent.py:90`,
  `jarvis/decision/packet.py:44`,
  `tests/unit/test_attention_policy.py:20–23` — all `trigger.type ==
  "utterance.received"` branches and fixtures swap to
  `"surface.user_intent"`.
- `jarvis/surface/cli_render.py` — channel split + `notify.deliver_*` +
  `surface.response_emitted.delivered_via` + `attention_channel`.
- `pyproject.toml` — pin `data/pricing.json` as package data; lint
  exemptions (below).

**Test files:**

| Path | Type |
|---|---|
| `tests/unit/test_codex_action.py` | mocked JSON-RPC (incl. submit_report registration + capture) |
| `tests/unit/test_codex_mcp_tools.py` | subprocess fixture — spawns `python -m jarvis.execution.codex_mcp_tools` and drives `initialize` → `tools/list` → `tools/call` over stdio JSON-RPC (Step-6 server unit; ~5–10s wall) |
| `tests/unit/test_reviewer.py` | mocked LLM (tests **L3** reviewer module + fresh-context guard) |
| `tests/unit/test_diff_capture.py` | tmp git repo fixtures (clean tree + dirty-stash-pop + dirty-stash-conflict paths) |
| `tests/unit/test_pricing.py` | parametrized rate tables (module under `jarvis/shared/`) |
| `tests/unit/test_notify.py` | mocked subprocess + channel-to-surface mapping |
| `tests/unit/test_daemon.py` | platform-skipped (no fork on Windows) |
| `tests/unit/test_sleep_wake.py` | stubbed IOPM observer + fake clock; emits 4-event sleep/wake sequence; reconcile_after_wake is idempotent |
| `tests/unit/test_create_task.py` | tool registration + dispatch + `verify_command` detection (pytest / cargo / jest / make / unknown) |
| `tests/unit/test_resolver_time_window.py` | time-window projection API; canary `test_canary_resolver_uses_projection_api` |
| `tests/unit/test_verify_diff_observation.py` | L4 observation handler |
| `tests/unit/test_result_interpreter_ladder.py` | L3 (relation, level) ladder mapping (LLM-free, given pre-constructed inputs) |
| `tests/unit/test_pre_emit_phrases.py` | single source of regex constants; canary `test_canary_regex_constants_single_source` |
| `tests/scenarios/test_real_codex_flagship.py` | live LLM + live Codex (gated by `--live-codex` flag); covers J1–J13 + K1–K6 + L1–L2 |
| `tests/scenarios/test_real_codex_verify_fail.py` | live LLM + live Codex; `verify_command` fails (L3) |
| `tests/scenarios/test_real_codex_empty_diff.py` | live LLM + live Codex; Codex produces empty diff (L4) |
| `tests/scenarios/test_real_codex_no_submit_report.py` | live LLM + live Codex; Codex fails to call submit_report → worker.report_missing path (J12) |
| `tests/scenarios/test_real_codex_sleep_during_turn.py` | simulated mac.sleeping mid-turn (test power-observer stub); reconciliation closes orphan action (K7) |

**Lint additions to `pyproject.toml`:**

- Allow `subprocess.Popen` in `jarvis/execution/codex_client.py`
  (file-level noqa with attribution comment).
- Allow `os.fork` / `os.setsid` / `os._exit` in
  `jarvis/runtime/daemon.py`.
- Allow `subprocess.run` / `subprocess.Popen` in
  `jarvis/surface/notify.py`.
- Allow `subprocess.run` in `jarvis/execution/diff_capture.py`
  (git stash / diff invocations).
- Allow `ctypes` / `objc` / Cocoa IOPM bindings in
  `jarvis/deployment/sleep_wake.py` (with platform-skip for non-Mac).
- Importlinter: `jarvis.execution.codex_client` is a vendored module —
  exempt from layer rules.
- Importlinter: `jarvis.shared.pricing` may be imported by both L3
  (`jarvis.decision.*`) and L4 (`jarvis.execution.*`). The shared/
  package is the canonical home for cross-layer static reference data.

### Layer trace (Day-2)

- **L1 Constitution** — unchanged.
- **L2 State** — registry +12 event types; Task Ledger projection adds
  `tasks_in_window(...)` API. `evidence.attached` payload now requires
  `(relation, level)`.
- **L3 Decision** — resolver time-window query **via projection API
  (not direct SQL)**; **reviewer** at `jarvis/decision/reviewer.py`
  (F4 fix); Result Interpreter consumes **two** `action.result_observed`
  events per `verify_diff` action — one with `result_semantics=
  "observation"` (diff capture slot) and one with `result_semantics=
  "verification"` or `"error"` (post_action_check slot, when the task
  has a `verify_command`). For the observation slot, the interpreter
  calls the reviewer; for the verification slot it forms a
  Postcondition Claim at `level=verified` and emits `task.verified`.
  Reviewer output produces a **separate** Report-grade evidence row
  on whichever claim is active (never raises level). Forms Evidence
  `(relation, level)` per the F2 ladder, one row per source. Emits
  `cost.recorded` for both decision LLM rounds and Codex turns (L4
  returns cost in RawResult.metadata). ResponsePlan carries canonical
  `output_risk_class` + `required_gate_mode` per spec §3.4.13.
- **L4 Execution** — `codex_client.py` (vendored), `codex_action.py`
  (incl. submit_report MCP server injection via `-c
  mcp_servers.jarvis-tools.*` spawn flags per spec §3.5.8),
  `codex_mcp_tools.py` (stdio MCP server subprocess spawned by Codex,
  exposes the single `submit_report` tool), `diff_capture.py`
  (incl. dirty-tree auto-stash), real `spawn_worker_handler`,
  dual-slot `verify_diff_handler` (returns `RawResultBundle` with
  observation slot 1 + post_action_check verification/error slot 2
  when `verify_command` is non-None per spec §3.5.7; no reviewer
  call inside L4; no apply-check), new `create_task_handler`.
- **L5 Surface** — `notify.py`; voice/document channel split routes to
  TTS + banner; `surface.response_emitted` carries `delivered_via`
  (physical surfaces) and `attention_channel` (logical L3 channel);
  L5 emits canonical `surface.user_intent` (not legacy
  `utterance.received`) for CLI input per spec §3.4.1.
- **L6 Deployment** — `daemon.py` fork-detach; CLI entry point reads
  utterance, runs regex classifier, prints ack, forks. SQLite never
  opens before the fork. `sleep_wake.py` registers macOS IOPM
  notification observer in the detached child; emits `mac.sleeping` /
  `worker.suspended_by_sleep` / `mac.awake` / `worker.terminated_by_sleep`
  and runs reconciliation on wake (spec §3.7.8). `jarvis/shared/pricing.py`
  is referenced by both L3 and L4 (cost computation) — `shared/` because
  pricing is a static reference, not event-folded state.

---

## Canonical event trace (Day-2)

### Phase 1 — D-1 (yesterday's task-creation session, ~14 events)

| # | Event | source_event_id | correlation | Payload sketch | Evidence |
|---|---|---|---|---|---|
| 1 | `turn.started` | — | turn_id=T1 | started_at | none |
| 2 | `surface.user_intent` | evt 1 | turn_id=T1 | transcript="今天给我做 implement-rate-limiter…" | report |
| 3 | `cost.recorded` | — | turn_id=T1 | kind=decision, model=gpt-5.5, tokens_in/out | none |
| 4 | `action.proposed` | evt 1 | turn_id=T1, action_id=A1 | tool=create_task, args={goal, repo_path, verify_command?} | none |
| 5 | `gate.evaluated` | evt 4 | turn_id=T1, action_id=A1 | gate=pre_action, outcome=pass | none |
| 6 | `action.authorized` | evt 5 | turn_id=T1, action_id=A1 | — | none |
| 7 | `action.dispatched` | evt 6 | turn_id=T1, action_id=A1 | — | ack |
| 8 | `action.running` | evt 7 | turn_id=T1, action_id=A1 | — | ack |
| 9 | `task.created` | evt 8 | task_id=T_X, turn_id=T1, action_id=A1 | goal, repo_path, verify_command (e.g. "pytest"), source=manual | observation |
| 10 | `action.result_observed` | evt 9 | turn_id=T1, action_id=A1 | semantics=ack, run_id=None | ack |
| 11 | `cost.recorded` | — | turn_id=T1 | kind=decision (L3 response LLM), model, tokens | none |
| 12 | `action.proposed` | evt 10 | turn_id=T1, action_id=A2 | tool=response, args={text} | none |
| 13 | `gate.evaluated` | evt 12 | turn_id=T1, action_id=A2 | gate=pre_emit, outcome=allow_completion_language, output_risk_class=routine, active_claim_levels=[] | none |
| 14 | `surface.response_emitted` | evt 13 | turn_id=T1 | text="好的，task_X 已记下，明天提醒你跑", delivered_via=["stdout"] | none |
| 15 | `turn.ended` | evt 1 | turn_id=T1 | duration_ms | none |

### Phase 2 — D-day (the flagship scenario, ~24–26 events + N heartbeats)

Heartbeats fire every 30s; N = ceil(elapsed_s / 30). Event numbering
below is relative to a typical N≈6 (3-minute Codex turn).

| # | Event | source_event_id | correlation | Payload sketch | Evidence |
|---|---|---|---|---|---|
| 16 | `turn.started` | — | turn_id=T2 | started_at | none |
| 17 | `surface.user_intent` | evt 16 | turn_id=T2 | transcript="昨天那个 task 给 Codex 跑一下，做完审核了再告诉我。" | report |
| 18 | `cost.recorded` | — | turn_id=T2 | kind=decision (L3 LLM 1: routing + time window) | none |
| | *[L3 resolver internal: emits `{since_ts, until_ts}`, runs SELECT against event log, binds task_id=T_X. No additional events emitted by the resolver itself; resolution is recorded in the next `entity.resolved` per ADR-0001 resolver contract.]* | | | | |
| 19 | `entity.resolved` | evt 17 | turn_id=T2 | entity_type=task, natural_ref="昨天那个 task", resolved_to=T_X, confidence=high, outcome=resolved, candidates=[T_X], match_basis="time_window" | none |
| 20 | `action.proposed` | evt 19 | turn_id=T2, action_id=A3, task_id=T_X | tool=spawn_worker | none |
| 21 | `gate.evaluated` | evt 20 | turn_id=T2, action_id=A3 | gate=pre_action, outcome=pass | none |
| 22 | `task.executor_assigned` | evt 21 | task_id=T_X, action_id=A3 | executor=codex, model=gpt-5.5 (effort=xhigh) | none |
| 23 | `action.authorized` | evt 21 | turn_id=T2, action_id=A3 | — | none |
| 24 | `action.dispatched` | evt 23 | turn_id=T2, action_id=A3 | — | ack |
| 25 | `action.running` | evt 24 | turn_id=T2, action_id=A3 | — | ack |
| 26 | `run.started` | evt 25 | run_id=R_Y, task_id=T_X, action_id=A3 | runner=codex | ack |
| | *[CLI fork-detach happened before evt 16 (turn.started): parent printed the ack at utterance-parse time, then `os._exit(0)`-ed. Every event in this trace is emitted by the detached child. Fork is a process-model concern, not a state-model event.]* | | | | |
| | *[Per spec §3.5.9 — unified async lifecycle — worker.* events below are RUNNING-phase extensions of action A3; A3 stays in `running` state until terminal at evt 32+N. Do NOT emit a separate `action.result_observed(semantics=ack)` here; that would split worker lifecycle into a parallel state machine, which §3.5.9 explicitly forbids.]* | | | | |
| 27..27+N-1 | `worker.heartbeat` | evt 26 | run_id=R_Y, action_id=A3 | elapsed_ms=30000·k, last_log_line | observation |
| 27+N | `worker.artifact_observed` | evt 26 | run_id=R_Y, action_id=A3 | artifact_path=…/diff.txt, content_hash | observation |
| 28+N | `task.executor_reported` | evt 26 | task_id=T_X, run_id=R_Y | status=ok, summary, diff_path | report |
| 29+N | `worker.reported` | evt 26 | run_id=R_Y, action_id=A3 | status=ok, summary, artifact_path (sourced from Codex's `submit_report` tool call; if missing, evt 29+N is `worker.report_missing` instead and a Limitation Claim is emitted) | report |
| 30+N | `cost.recorded` | — | run_id=R_Y, action_id=A3 | kind=codex, model=gpt-5.5, tokens_in/out from turn/completed (L4 returned cost in RawResult; L3 emits) | none |
| 31+N | `action.result_observed` | evt 29+N | turn_id=T2, action_id=A3 | result_semantics=report, run_id=R_Y, summary, artifact_ref=diff_path | report |
| | *[L3 Result Interpreter consumes evt 31+N: spec §3.4.11 maps `report` → Report Claim, level=reported. No verified claim yet — the Codex report is agent self-report per §8.5 rule 1. Decide() then proposes the next action (verify_diff).]* | | | | |
| 32+N | `claim.created` | evt 31+N | task_id=T_X, claim_id=C1 | type=Report, statement="codex reported complete with summary X" | report |
| 33+N | `evidence.attached` | evt 32+N | claim_id=C1, evidence_id=E1 | relation=supports, level=reported, source_type=agent, source_id=R_Y | report |
| 34+N | `cost.recorded` | — | turn_id=T2 | kind=decision (L3 LLM 2: post-worker reasoning) | none |
| 35+N | `action.proposed` | evt 31+N | turn_id=T2, action_id=A4, run_id=R_Y | tool=verify_diff | none |
| 36+N | `gate.evaluated` | evt 35+N | turn_id=T2, action_id=A4 | gate=pre_action, outcome=pass | none |
| 37+N | `action.authorized` | evt 36+N | turn_id=T2, action_id=A4 | — | none |
| 38+N | `action.dispatched` | evt 37+N | turn_id=T2, action_id=A4 | — | ack |
| 39+N | `action.running` | evt 38+N | turn_id=T2, action_id=A4 | — | ack |
| 40+N | `action.result_observed` | evt 39+N | turn_id=T2, action_id=A4 | result_semantics=**observation** (slot 1, diff capture per spec §3.4.11), diff_text_preview, diff_nonempty=True, artifact_ref=diff_path | observation |
| 41+N | `action.result_observed` | evt 39+N | turn_id=T2, action_id=A4 | result_semantics=**verification** (slot 2, post_action_check per spec §3.5.7), verify_command="pytest", verify_command_exit=0, verify_command_stdout_tail | verification |
| | *[L3 Result Interpreter consumes BOTH slots independently. From slot 1 (observation), it calls `jarvis.decision.reviewer.review_diff(...)` and forms a reviewer-sourced evidence row at level=reported. From slot 2 (verification), it forms a Postcondition Claim at level=verified directly — no LLM call. No event for the reviewer invocation itself; the next `cost.recorded` records its LLM use.]* | | | | |
| 42+N | `cost.recorded` | — | turn_id=T2, run_id=R_Y | kind=reviewer, model=gpt-5.5, tokens_in/out | none |
| 43+N | `claim.created` | evt 41+N | task_id=T_X, claim_id=C2 | type=Postcondition, statement="diff implements the task goal AND verify_command exited 0" | postcondition |
| 44+N | `evidence.attached` | evt 43+N | claim_id=C2, evidence_id=E2 | relation=supports, level=**verified**, source_type=tool, source_id=verify_command (post_action_check), source_event=evt 41+N | postcondition |
| 45+N | `evidence.attached` | evt 43+N | claim_id=C2, evidence_id=E3 | relation=supports, level=**reported**, source_type=llm, source_id=reviewer (verdict=ok), source_event=evt 40+N | report |
| 46+N | `task.verified` | evt 44+N | task_id=T_X | by=jarvis_runtime, derived_from_claim=C2, derived_from_evidence=E2 | verification |
| 47+N | `cost.recorded` | — | turn_id=T2 | kind=decision (L3 LLM 3: final response) | none |
| 48+N | `action.proposed` | evt 46+N | turn_id=T2, action_id=A5 | tool=response | none |
| 49+N | `gate.evaluated` | evt 48+N | turn_id=T2, action_id=A5 | gate=pre_emit, outcome=allow_completion_language, output_risk_class=consequential_claim, required_gate_mode=full_text, active_claim_levels=[verified] | none |
| 50+N | `surface.response_emitted` | evt 49+N | turn_id=T2 | text, voice_text, document_text, delivered_via=["voice","banner","stdout"], attention_channel=voice_notify | none |
| 51+N | `turn.ended` | evt 16 | turn_id=T2 | duration_ms | none |

Total Phase-2 events (excluding heartbeats): 30 + N (was 28+N before
the §3.5.7 correction split the verify_diff `action.result_observed`
into two semantically-distinct slots — observation + verification —
and split the resulting `evidence.attached` rows by source so the
verify_command-derived verified row and the reviewer-LLM-derived
reported row attach independently to the same Postcondition Claim).
For N=6 → 36 events. **Tier-2 acceptance count tolerance: A1 widens
to [32, 44] for the Phase-2 run.**

### Negative-path appendix

Three failure modes, narrative (not tabular):

**verify_command fails** (test `test_real_codex_verify_fail.py`):
Events 1–(39+N) identical to the happy path. At evt 40+N, slot 1
fires `result_semantics=observation` as in the happy path. At evt
41+N, slot 2 fires `result_semantics=**error**` (verify_command exited
non-zero per spec §3.4.11 error row + §3.5.7 fail-of-match), not
`verification`. The L3 Result Interpreter therefore forms a Limitation
Claim at `level=executed` from slot 2 (the tool ran but the
postcondition predicate failed), **no Postcondition Claim at
level=verified**, and an Artifact Claim at `level=observed` from slot
1. `task.verified` is **not** emitted. The reviewer LLM still
contributes a separate Report-grade evidence row attached to the
Artifact Claim. At the response action, the Pre-emit Gate verdict is
`force_limitation_language`; the surface response matches one of
`{r"reported,?\s*not\s+verified", r"测试.{0,4}没过", r"未验证"}`.

**Codex subprocess crashes** (test
`test_real_codex_flagship.py::crash_path`):
Events 1–26 identical. Instead of evt 27 onward → `action.failed` is
emitted with `error="codex_subprocess_crashed"`; no `worker.reported`,
no `verify_diff` action, no `task.verified`. Surface response says
"Codex 跑挂了，没新 diff"; level remains `reported`.

**Codex turn times out** (test
`test_real_codex_flagship.py::timeout_path`):
Identical to crash except `action.timeout_assumed` replaces
`action.failed`; `close(timeout=3.0)` kills the subprocess; surface
response says "Codex 超时，未完成"; level remains `reported`.

---

## Acceptance criteria

### Tier overview (Day-2)

| Tier | Cost | Day-1 (A–I) | Day-2 additions |
|---|---|---|---|
| 1 | LLM-free, < 75s wall | Inherited | + cost-recorded canary; + codex_version preflight unit test; + ladder mapping unit test; + ack-before-fork canary; + 12 new event types in registry |
| 2 | Real LLM + real Codex (`--live-codex`) | Inherited | + **J. Real Codex round-trip** + **K. Real notification side-effects** + **L. Evidence ladder enforcement** |

### Tier 1 (LLM-free)

All Day-1 Tier-1 gates remain. New additions:

- `lint-imports KEPT (1/1)` — covers Day-2 layer additions; reviewer
  must live under `jarvis/decision/`; pricing module lives under
  `jarvis/shared/`; sleep-wake module lives under `jarvis/deployment/`.
- `ruff clean` — covers new files including vendored `codex_client.py`
  (file-level noqa for `subprocess.Popen` + JSON-RPC framing).
- `mypy strict clean` — Day-2 adds ~12 modules; strict stays green.
- New canary tests:
  - `test_canary_codex_version_preflight` — module import raises if
    `codex --version` < 0.125.0.
  - `test_canary_cost_recorded_emitted_per_llm_call` — AST scan of
    `decide()` and `review_diff()` ensures `emit_event(type=
    "cost.recorded", ...)` follows every `llm_client.chat(...)` site.
  - `test_canary_cost_recorded_l3_only` — AST scan: no `emit_event(...,
    type="cost.recorded")` call appears under `jarvis/execution/`. L4
    returns cost in `RawResult.metadata.cost`; only L3 emits the event.
  - `test_canary_daemon_ack_before_fork` — AST scan ensures
    `fork_detach()` is preceded by a `print(...)` + `sys.stdout.flush()`
    + that no `bootstrap_runtime_app(...)` precedes the fork in the
    parent code path.
  - `test_canary_reviewer_in_l3` — AST scan: `jarvis/decision/reviewer.py`
    exists; `jarvis/execution/reviewer.py` does **not** exist; any L4
    module importing a reviewer symbol is a regression.
  - `test_canary_reviewer_fresh_context` — AST scan: every call to
    `review_diff(...)` is wrapped in `with llm_client.fresh_context():`
    or equivalent; reviewer prompts do not include the decision LLM's
    session history.
  - `test_canary_no_apply_check` — AST scan: no string
    `"git apply --check"` (or equivalent) appears under
    `jarvis/execution/` or `jarvis/decision/`.
  - `test_canary_resolver_uses_projection_api` — AST scan:
    `jarvis/decision/resolver.py` does NOT contain any literal SQL
    (`"SELECT"`, `"select * from"`, raw `sqlite3.connect(...)`). The
    resolver may only call `task_ledger.tasks_in_window(...)` or
    other projection APIs.
  - `test_canary_surface_user_intent_swap` — AST scan: no
    `emit_event(...type="utterance.received")` call exists outside
    of `tests/` or future voice-surface code; CLI input path emits
    `surface.user_intent` per spec §3.4.1.
  - `test_canary_evidence_relation_required` — AST scan: every
    `emit_event(..., type="evidence.attached", ...)` site sets both
    `relation` and `level` in payload.
  - `test_canary_submit_report_injection` — AST scan:
    `jarvis/execution/codex_action.py` builds an `extra_args` list
    containing string literals `"mcp_servers.jarvis-tools.command"`,
    `"mcp_servers.jarvis-tools.args"`, `"mcp_servers.jarvis-tools.startup_timeout_sec"`,
    `"mcp_servers.jarvis-tools.tool_timeout_sec"` (the `-c` flag
    injection vector); the take_notification loop captures
    `item/tool_call` events with `tool_name == "submit_report"`. The
    canary lives at AST level — it must match before any live spawn.
  - `test_canary_mcp_injection_via_c_flags` — AST scan asserts that
    `jarvis/execution/codex_action.py` injects `submit_report` via
    the `-c mcp_servers.jarvis-tools.*` spawn-flag mechanism (a
    `"mcp_servers.jarvis-tools.command"` string literal must appear
    in the `-c` flag list passed to `CodexAppServerClient`) AND that
    no string `"mcp/register_tool"` (or equivalent runtime-RPC
    registration method) appears **anywhere under**
    `jarvis/execution/` or `jarvis/decision/`. This canary is the
    architectural guard that the injection mechanism is config-flag,
    not runtime RPC (Codex's app-server protocol surface does not
    expose runtime tool registration).
  - `test_canary_verify_diff_post_action_check` — AST scan:
    `jarvis/execution/tools.py` defines `VERIFY_DIFF_TOOL_DEF`
    (the ToolDefinition for `verify_diff`) with a `post_action_check`
    field whose `result_semantics_on_match` literal equals
    `"verification"` and whose `mode` literal equals `"inline"`;
    verifies the spec §3.5.7 binding is present at the
    ToolDefinition level so the dual-slot RawResult path is wired
    statically rather than implicitly.
  - `test_canary_regex_constants_single_source` — AST scan: no
    inline `re.compile(r"reported,?\s*not\s+verified", ...)` or
    similar limitation/completion regex literal outside
    `jarvis/decision/pre_emit_phrases.py`.
  - `test_canary_pricing_at_shared` — AST scan: no `from jarvis.state
    import pricing` or equivalent; pricing lives under
    `jarvis/shared/`.

Wall-time budget: < 75s (was < 30s Day-1; +45s amortized across:
the larger AST canary suite ~15s; sleep_wake fake-clock fixtures ~5s;
relation-column + dirty-tree git fixtures ~5s; `test_codex_mcp_tools.py`
subprocess-fixture round-trip (`initialize` → `tools/list` →
`tools/call` drive against the Step-6 stdio MCP server in a spawned
`python -m jarvis.execution.codex_mcp_tools`) ~5–10s; the dual-slot
`test_verify_diff_observation.py` matrix (verify_command present /
absent × exit 0 / non-zero × timeout) ~3–5s; remaining ~5s for the new
reviewer / resolver / create_task units). The previous < 60s target was
authored before Step 6 was split out into its own stdio-subprocess
unit and before the verify_diff dual-slot matrix existed; both add real
subprocess wall-time that cannot be mocked away without losing the
canary's load-bearing assertion. If this slips further, the subprocess
unit tests (codex_mcp_tools, sleep_wake fake clock) are the prime
suspects — they are the only Tier-1 entries that actually spawn child
processes.

### Tier 2 J — Real Codex round-trip

Gated by `--live-codex` pytest flag (skips without it).

| ID | Invariant |
|---|---|
| J1 | `codex --version` ≥ 0.125.0 at handler registration; below → `CodexVersionTooLow` raised |
| J2 | `CodexAppServerClient.initialize()` returns within 5s; OAuth refresh path covered |
| J3 | `thread/start.cwd` matches `task.created.payload["repo_path"]` byte-for-byte |
| J4 | For any Codex turn whose `elapsed_ms ≥ 30000`, at least one `worker.heartbeat` event has `source_event_id` chain back to the same `action.running` |
| J5 | `turn/completed` with non-empty `final_text` AND non-empty `git -C cwd diff` → `worker.reported.status == "ok"` |
| J6 | `CodexAppServerClient.is_alive()` is False at the moment `action.result_observed` for verify_diff emits |
| J7 | At least one `cost.recorded` row with `kind == "codex"` and same `run_id` correlation as the spawn_worker action |
| J8 | On Codex crash (subprocess return non-zero), `action.failed` emitted with `error="codex_subprocess_crashed"`; no `task.verified` |
| J9 | On Codex timeout (deadline exceeded), `action.timeout_assumed` emitted; subprocess killed via `close(timeout=3.0)` |
| J10 | `-c` flags `model=gpt-5.5`, `model_reasoning_effort=xhigh`, `sandbox_mode=workspace-write`, `sandbox_workspace_write.writable_roots=["<cwd>"]` all present in spawn argv (assert by inspecting `Popen.args` capture) |
| J11 | `submit_report` MCP tool is reachable in the Codex thread (registered via `-c mcp_servers.jarvis-tools.*` spawn flags, NOT via runtime JSON-RPC) before `turn/start` fires; verified by inspecting the spawn `Popen.args` to confirm all four `mcp_servers.jarvis-tools.{command,args,startup_timeout_sec,tool_timeout_sec}` flags are present AND by Codex's MCP `tools/list` handshake returning `submit_report`; the take_notification loop captures an `item/tool_call` event with `tool_name == "submit_report"` exactly once before `turn/completed` |
| J12 | If Codex completes a turn without calling `submit_report`, `worker.report_missing` is emitted and a Limitation Claim with `relation=limits, level=reported` is attached (spec §3.5.8) |
| J13 | Dirty-tree case: spawn_worker on a repo with uncommitted changes auto-stashes via `git stash push -u`, runs Codex, then `git stash pop`. On stash-pop conflict, the stash is preserved as `artifacts/run_<R>/conflict.patch` and surfaced via Limitation Claim |

### Tier 2 K — Real notification side-effects

| ID | Invariant |
|---|---|
| K1 | `say` subprocess started with `-v Tingting` (or configured voice) and voice-channel text as last arg |
| K2 | `osascript -e 'display notification ...'` invoked exactly once per surface emission; body truncated at 240 chars |
| K3 | CLI parent process exits within 100ms of the long-run classifier matching; **the parent's PID does not appear in `~/.jarvis/mac_events.db` writes** (parent never opened SQLite) |
| K4 | Detached child writes `worker.reported` to `~/.jarvis/mac_events.db`; a fresh `open_event_log()` from a separate process observes the row after the parent has exited |
| K5 | Reviewer-verdict-fail path delivers a *limitation* utterance through `say` (matches `{r"reported,?\s*not\s+verified", r"agent\s+reported", r"未验证"}`) rather than a completion claim |
| K6 | `surface.response_emitted.payload.delivered_via` lists physical surfaces (`["voice","banner","stdout"]` for fully-delivered surfaces when CLI still attached); `attention_channel` field carries the L3 logical channel (`voice_notify` for the flagship). Partial delivery (e.g. detached CLI → no stdout) is flagged in the payload |
| K7 | Inducing `mac.sleeping` mid-turn via the test power-observer stub causes `worker.suspended_by_sleep(run_id=R_Y, action_id=A3)` to fire before the simulated sleep. On simulated wake, `mac.awake(slept_for_ms)` fires and reconcile_after_wake() emits `worker.terminated_by_sleep` + `action.timeout_assumed` for the orphan Codex run; a Limitation Claim with `relation=limits, level=executed` is attached; `task.verified` is **not** emitted |
| K8 | `reconcile_after_wake(...)` is idempotent: invoking it twice in succession does not emit a second `action.timeout_assumed` for the same `action_id` |

### Tier 2 L — Evidence ladder enforcement

Gated by `--live-codex` (real Codex output exercises the ladder).

| ID | Invariant |
|---|---|
| L1 | `task.created` without `verify_command` + scenario runs → assert exactly ONE `action.result_observed` for the verify_diff action with `result_semantics="observation"` (slot 1 only; slot 2 verification is omitted because the task has no verify_command per spec §3.5.7); assert **no** Postcondition Claim with `level=verified` is emitted; assert an Artifact Claim with `level=observed` is emitted (sourced from the diff observation slot); assert a Limitation Claim with `level=reported, relation=limits` records the missing verify_command per spec §8.5 rule 6; assert **no** `task.verified` |
| L2 | `task.created` with `verify_command="pytest"` + verify_command exit=0 + reviewer.verdict=ok → assert TWO `action.result_observed` for verify_diff (slot 1 `result_semantics="observation"`, slot 2 `result_semantics="verification"`); assert a Postcondition Claim is emitted with TWO `evidence.attached` rows on the same claim: one at `level=verified, relation=supports, source_type=tool, source_id=verify_command` (from slot 2) AND one at `level=reported, relation=supports, source_type=llm, source_id=reviewer` (from the reviewer LLM); assert `task.verified` emitted with `derived_from_evidence` pointing at the verified-level row |
| L3 | `task.created` with `verify_command="pytest"` + verify_command exit ≠ 0 (reviewer verdict irrelevant) → assert TWO `action.result_observed` for verify_diff (slot 1 `result_semantics="observation"`, slot 2 `result_semantics="error"` since the post_action_check predicate `exit_code == 0` did not match); assert a Limitation Claim with `level=executed, relation=limits, source_id=verify_command` is emitted (the tool ran but the postcondition predicate refuted); assert **no** Postcondition Claim with `level=verified`; assert **no** `task.verified`; assert `surface.response_emitted.text` matches regex set `{r"reported,?\s*not\s+verified", r"测试.{0,4}没过", r"未验证"}` |
| L4 | Codex produced empty diff (`diff_nonempty == False`) → assert an Execution Claim at `level=executed` (spawn_worker ran to completion) PLUS a Limitation Claim at `level=reported, relation=limits` for the missing artifact (spec §8.5 rule 6); assert **no** Postcondition Claim at `level=verified`; assert **no** `task.verified`; references the F2 ladder "diff_nonempty == False" row |
| L5 | reviewer-fail path (`verify_command` absent + reviewer.verdict="fail" + `diff_nonempty == True`) → assert ONE `action.result_observed` for verify_diff with `result_semantics="observation"` (slot 1 only); assert an Artifact Claim at `level=observed, relation=supports, source=diff_capture`; assert a Limitation Claim with `level=reported, relation=refutes, source_type=llm, source_id=reviewer (verdict=fail)`; assert **no** Postcondition Claim with `level=verified`; assert **no** `task.verified`; assert the reviewer's Report-level row never reached `verified` |

### Tier 2 inherited invariants (delta notes)

A–I from ADR 0001 stay, with these specific updates:

- **A. Event Log structural** — registry now has Day-1 count + 12 new
  entries (8 Day-2 originals + 4 sleep/wake); A1 event-count tolerance
  widens to **[32, 44]** for Phase-2 (was [28, 40]; +2 covers the
  newly-explicit Report Claim + evidence_attached pair after
  worker.reported plus the corrected terminal action.result_observed
  for spawn_worker; +2 more covers the §3.5.7 dual-slot split for
  verify_diff — two `action.result_observed` events instead of one,
  and two `evidence.attached` rows instead of one).
- **B. ActionLifecycle 8-state** — Per spec §3.5.9 (unified async
  lifecycle), spawn_worker's action stays in `running` while
  worker.heartbeat / worker.artifact_observed / worker.reported fire;
  it transitions to `result_observed` (semantics=report) only AFTER
  worker.reported. ADR-0001 B2 ("transitions follow canonical order…
  no skipped states") still holds — worker.* events are running-phase
  extensions, not lifecycle states.
  **B3 amendment (Day-2 only).** ADR-0001 B3 reads "exactly two terminal
  events of class `result_observed` in the happy path" (one for A1
  spawn_worker, one for A2 verify_diff). Per spec §3.5.7
  `post_action_check` chaining and §3.4.11's singular `result_semantics`
  per event, Day-2's verify_diff (A4) emits **two**
  `action.result_observed` events when the bound task carries a
  `verify_command` — slot 1 with `result_semantics="observation"` (diff
  capture) and slot 2 with `result_semantics="verification"` on match or
  `result_semantics="error"` on miss (spec §5.4.2 allows per-output
  mapping; spec §3.5.7 inline chaining requires it). Both events carry
  the same `action_id` (A4); the action is "settled" once both slots
  have emitted (§3.5.9 unified lifecycle still holds — one action_id,
  one terminal transition over the pair). Happy-path terminal
  `result_observed` count therefore becomes **A1 + A4_slot1 + A4_slot2
  = 3** when `verify_command` is present, and **A1 + A4_slot1 = 2** when
  absent. Tier-2 B-invariant assertions over `action_id` distinct-counts
  remain valid (still exactly two distinct `action_id`s for Day-2's
  spawn_worker + verify_diff pair); only the row-count over
  `action.result_observed` widens.
- **C. Three-Gate enforcement** — `create_task` is L1 risk, JARVIS_LLM
  caller; Pre-action Gate must approve. Pre-emit Gate outcome
  vocabulary is fixed per ADR-0001 contract (`pass | refuse |
  confirm_required` for pre_action; `allow_completion_language |
  force_limitation_language` for pre_emit) — the trace never uses
  bare `outcome=allow`.
- **D. Task status derivation** — `task.executor_reported` is a
  report-level claim only; `task.verified` requires the verified row of
  the L2 ladder; `evidence.attached` payload must carry the
  `(relation, level)` pair per spec §8.6.
- **F. Negative case — verify predicate fails** — split into L3/L4/L5
  paths above (verify_command fail vs reviewer fail vs empty diff).
  All limitation phrasing references the single
  `jarvis/decision/pre_emit_phrases.py:LIMITATION_REGEXES` constant.
- **G. LLM is real, not mocked** — Codex is real too; reviewer LLM is
  real too. Mocks only in unit tier.
- **H. Anti-bypass canaries** — fourteen new entries (cost-recorded
  emitted, cost-recorded L3-only, ack-before-fork, reviewer-in-L3,
  reviewer-fresh-context, no apply-check, resolver-uses-projection-api,
  surface-user-intent-swap, evidence-relation-required,
  submit-report-injection, mcp-injection-via-c-flags,
  verify-diff-post-action-check, regex-constants-single-source,
  pricing-at-shared).
- **I. Replay determinism** — Codex's `turn_id` is recorded; replay
  tolerance widens because Codex output is non-deterministic
  (acknowledged, not enforced equality).

---

## Build order

Sized as ADR-0001-style discrete steps. Each step lands with its own
commit using the standard 5-part body. Tier 1 must stay green at every
step. Style matches ADR-0001 lines 1033–1056 (What / References /
Verification).

| Step | What | References | Verification |
|---|---|---|---|
| **0** | Lift `codex_client.py` + `pricing.py` (to `jarvis/shared/`) + `_helpers.py` + `refresh_pricing.py` + `pricing.json`. Add attribution headers. No wiring. | Hermes `agent/transports/codex_app_server.py` (verbatim); legacy `memory/cold/pricing.py`, `scripts/refresh_pricing.py`, `tools_v2/helpers.py` | Tier 1 T1.A–T1.C clean; `lint-imports` exempts vendored `codex_client.py`; canary `test_canary_pricing_at_shared` passes |
| 1 | EventTypeRegistry: register 12 new types (worker.heartbeat, worker.artifact_observed, worker.report_missing, task.executor_assigned, task.executor_reported, cost.recorded, surface.user_intent, surface.response_emitted, mac.sleeping, mac.awake, worker.suspended_by_sleep, worker.terminated_by_sleep); amend `action.result_observed.optional_payload`, `task.created.optional_payload` (+ `repo_path`, + `verify_command`), and `evidence.attached.required_payload` (+ `relation`) | spec.html §5.4 lines 1435–1450, §3.7.8 sleep/wake events, §8.6 evidence schema; F6 + F7 + F8 | Unit test: registry has 12 new entries; `evidence.attached.required_payload` contains `relation`; `task.created.required_payload == ("task_id","goal")` unchanged |
| 2 | Surface.user_intent swap: refactor `jarvis/surface/cli.py` to emit `surface.user_intent` instead of `utterance.received`; update `jarvis/decision/__init__.py`, `intent.py`, `packet.py`, `runtime/__init__.py`, and all trigger fixtures under `tests/unit/`. `utterance.received` stays in registry (reserved for future voice surface) but is unused on the CLI path | spec.html §3.4.1 trigger taxonomy | Canary `test_canary_surface_user_intent_swap`; existing unit tests pass with renamed trigger |
| 3 | `cost.recorded` emission wired from `decide()` (every LLM call) and from L3 consuming RawResult.metadata.cost returned by L4 (codex turn cost). `LLMClient` metadata returned from `chat()`. **L3 is the sole emit-site** | legacy `memory/cold/pricing.py`; spec §5.4.1 owner_layer | Canary `test_canary_cost_recorded_emitted_per_llm_call` + `test_canary_cost_recorded_l3_only`; pricing table parametrized |
| 4 | `create_task` L4 tool: schema, handler with `verify_command` auto-detection (look for `pyproject.toml + tests/`, `pytest.ini` for Python; `Cargo.toml + tests/` for Rust; `package.json + jest.config` for JS; `Makefile + test target` for make-based). Returns `None` if no framework detected (downgrade path: Limitation Claim, no auto-verified). Registered for JARVIS_LLM caller | Day-1 `jarvis/execution/tools.py` (ADAPT); legacy `tools_v2/registry.py` for ToolEntry shape | Unit test: `test_create_task`; detection unit test parametrized over directory shapes (Python / Rust / JS / make / unknown) |
| 5 | Task Ledger projection: add `tasks_in_window(since_ts, until_ts) -> list[TaskId]` method. Time-window resolver: L3 LLM emits `{since_ts, until_ts}` (anchored to current epoch_ms via system prompt); resolver calls projection API — **no direct SQL** | Day-1 `jarvis/decision/resolver.py`; spec §3.4.3 (L3 reads L2 via projection snapshots only) | Canary `test_canary_resolver_uses_projection_api`; unit test: `test_resolver_time_window`; seeded event log + multiple windows |
| 6 | `codex_mcp_tools.py`: standalone Python stdio MCP server exposing one tool (`submit_report`) with the WorkerReport schema. Hand-rolled JSON-RPC stdio framing per Hermes' `hermes_tools_mcp_server.py` pattern. No third-party `mcp` PyPI dependency. Speaks `initialize` / `tools/list` / `tools/call`. Subprocess is spawned by Codex (NOT by jarvis directly) at app-server startup per the `-c mcp_servers.jarvis-tools.*` injection in the next step | spec §3.5.8 for submit_report contract; Hermes `agent/transports/hermes_tools_mcp_server.py` for stdio JSON-RPC framing pattern; Hermes `hermes_cli/codex_runtime_plugin_migration.py:557-605` for canonical MCP server entry shape | Unit test: spawn the module as a subprocess, drive `initialize` → `tools/list` → assert `submit_report` is returned with the right `input_schema`; drive `tools/call` → assert structured payload echoes back |
| 7 | `codex_action.py` one-shot driver around `codex_client`; include `-c` flag injection for sandbox (`model`, `model_reasoning_effort`, `sandbox_mode`, `writable_roots` via `_toml_list_quote`) AND for `submit_report` MCP server (`mcp_servers.jarvis-tools.command`, `args`, `startup_timeout_sec`, `tool_timeout_sec` — the four flags that point Codex at the Step-6 subprocess); capture `item/tool_call` events with `tool_name == "submit_report"` in the take_notification loop | Hermes `codex_app_server.py:75-130` for `-c` pattern (sandbox + MCP-server flags both); spec §3.5.8 for submit_report; § submit_report tool injection in this ADR | Canary `test_canary_submit_report_injection` + `test_canary_mcp_injection_via_c_flags`; unit test (mocked JSON-RPC): assert all eight `-c` flags appear in spawn flow AND the take_notification loop captures `submit_report` tool calls |
| 8 | `diff_capture.py`: dirty-tree auto-stash before Codex (`git stash push -u`), `git -C cwd diff` capture after, `git stash pop` with conflict-as-artifact fallback. **No** `git apply --check` | F3; § Dirty-tree policy | Unit test: tmp git repo fixture; clean tree path; dirty-tree-clean-pop path; dirty-tree-conflict path (conflict written as artifact + Limitation Claim) |
| 9 | `jarvis/decision/reviewer.py` (L3): structured-output LLM call, fresh-context system prompt (via `LLMClient.fresh_context()`), `ReviewerVerdict` dataclass | F4; spec.html §13.2 I10 ("claim ≤ evidence") | Canary `test_canary_reviewer_in_l3` + `test_canary_reviewer_fresh_context`; unit test (mocked LLM): `tests/unit/test_reviewer.py` covers ok / fail / malformed JSON / token recording / fresh-context |
| 10 | Replace `spawn_worker_handler` stub: real Codex flow + submit_report MCP server injection (via the eight `-c` flags from Step 7) + heartbeat loop + diff capture (with dirty-tree stash from Step 8) + `worker.*` event emission + `task.executor_assigned` / `task.executor_reported`. On missing submit_report → emit `worker.report_missing` + Limitation Claim per spec §3.5.8. Returns RawResult with `result_semantics="report"` and `metadata.cost` populated from Codex turn tokens | Day-1 `jarvis/execution/tools.py` `spawn_worker` stub; Hermes `codex_app_server_session.py` | Unit test (mocked subprocess + clock): `test_spawn_worker_real`; missing-submit-report path; J-tier covers live path |
| 11 | Replace `verify_diff_handler` with the **dual-slot observation + post_action_check** handler: declare `VERIFY_DIFF_TOOL_DEF.post_action_check` per spec §3.5.7 (`mode="inline"`, `check_tool="verify_command"`, `expected_predicate="exit_code == 0"`, `result_semantics_on_match="verification"`, `timeout_ms=600_000`); handler returns `RawResultBundle` with slot 1 carrying the diff observation (`result_semantics="observation"`) AND, when `task.verify_command` is non-None, slot 2 carrying the verify_command result (`result_semantics="verification"` on exit 0; `result_semantics="error"` on non-zero). L4 runs verify_command inline (`subprocess.run(...)` with 600s timeout, `cwd=repo_path`). **No** reviewer call. **No** apply-check | F3, F4; spec §3.4.11 result_semantics enum; spec §3.5.7 post_action_check | Canary `test_canary_no_apply_check` + `test_canary_verify_diff_post_action_check`; unit test: `tests/unit/test_verify_diff_observation.py` covers `verify_command` present/absent, exit 0/non-zero, timeout; assert RawResultBundle has both slots with correct `result_semantics` per branch |
| 12 | L3 Result Interpreter: **per spec §5.4.2** emit ONE `action.result_observed` per RawResult slot returned by Step-11 verify_diff. Observation slot → call `jarvis.decision.reviewer.review_diff(...)` and form an Artifact Claim at `level=observed` (diff source) PLUS a Report-grade evidence row from the reviewer; Verification slot (`result_semantics="verification"`) → form a Postcondition Claim at `level=verified` and emit `task.verified`; Verification-error slot (`result_semantics="error"`) → form a Limitation Claim at `level=executed` (the tool ran, the postcondition predicate refuted); never emit `task.verified` outside the verified branch. The reviewer's verdict attaches as a SEPARATE Report-level evidence row on whichever claim is active; it never raises the level. Per spec §8.5 rule 6, missing verify_command itself produces a Limitation Claim at `level=reported`. ResponsePlan carries `output_risk_class` + `required_gate_mode` per spec §3.4.13 | F4, F2 ladder; spec.html §3.4.11 + §3.5.7 + §5.4.2 + §8.4 + §8.5 + §8.6 + §3.4.13 | Canary `test_canary_evidence_relation_required`; unit test (LLM-free): `tests/unit/test_result_interpreter_ladder.py` parametrized over all ladder rows; assert dual-slot consumption produces independent evidence rows per source (verify_command vs reviewer vs diff) |
| 13 | `pre_emit_phrases.py`: single source of truth for `LIMITATION_REGEXES` + `COMPLETION_REGEXES`; ADR-0001 F4 / F5, ADR-0002 K5 / L3 all reference these constants | § Canonical limitation phrasing | Canary `test_canary_regex_constants_single_source`; unit test: regex constants compile + match seeded text samples |
| 14 | `notify.py` (`say` + `osascript`); document-channel banner truncation at 240 chars; explicit Attention Channel → Physical Surface mapping per § Notification contract | legacy `core/media_ducking.py:151-159` (osascript shape); spec §3.4.7 + §12.4 | Unit test (mocked subprocess): `test_notify`; truncation + escaping; channel-mapping table parametrized |
| 15 | `daemon.py` fork-detach: double-fork + setsid; child stdin/stdout/stderr → /dev/null; cwd=/ | POSIX double-fork pattern | Unit test (platform-skipped on non-Mac): `test_daemon`; parent returns "parent" immediately, child returns "child" with redirected fds |
| 16 | `sleep_wake.py`: install macOS IOPM notification observer; before-sleep + on-wake callbacks emit `mac.sleeping` / `worker.suspended_by_sleep` / `mac.awake` / run reconcile_after_wake() per spec §3.7.8. Idempotent reconciliation | spec §3.7.8; Mac-only architecture | Unit test (stubbed observer + fake clock): induced sleep/wake cycle emits the four-event sequence; reconcile_after_wake() is idempotent (K7 / K8) |
| 17 | CLI entry point: regex classifier (`_LONG_RUN_RE`), ack-before-fork, fork-detach, child re-bootstrap (incl. `install_power_observer(...)`). **No** SQLite open before fork. **No** task-bound check in classifier | F5; spec §3.7.8 (child must install power observer) | Canary `test_canary_daemon_ack_before_fork`; integration: parent exits within 100ms; child's bootstrap registers sleep observer |
| 18 | Surface render: voice → `say`, document → `notify`; populate `delivered_via` (physical) + `attention_channel` (logical L3 channel) on `surface.response_emitted` | F6; § Attention channel mapping | Unit test: payload populated correctly for all-channel and partial cases |
| 19 | Full Tier 1 canary sweep (14 new canaries from § Tier 1 list: original 12 + `test_canary_mcp_injection_via_c_flags` + `test_canary_verify_diff_post_action_check`) | ADR-0001 H series canary style | Full Tier 1 green (< 75s) |
| 20 | Tier 2 J + K + L acceptance tests (gated by `--live-codex` + `--live-llm`); includes D-1 fixture seeding + D-day happy path + verify-fail + empty-diff + crash + timeout + simulated sleep/wake + missing-submit-report | this ADR § Acceptance | All J (1-13) / K (1-8) / L (1-5) invariants pass on Allen's Mac |
| 21 | `docs/progress.md` ADR-0002 acceptance summary; update CLAUDE.md anchor if needed | ADR-0001 build-order convention | Doc-only; Tier 1 docs-only |

**Estimated commit count: 22.** Each step ~50–300 LOC delta. Worktree
convention same as ADR-0001 (`worktree-claude-adr0002` or similar).

---

## Consequences

### Positive

- One scenario fully functional end-to-end on a real repo with real
  edits, real audit (reviewer LLM + opt-in `verify_command`), real
  delivery (voice + banner), real cost tracking.
- Day-1 architecture (6 layers, event spine, three gates,
  ActionLifecycle) validated against actual work.
- 12 new canonical event types fill gaps spec.html §5.4 flagged (most
  notably `surface.user_intent`, which was in the spec but missing from
  Day-1 registry; plus the four sleep/wake events `mac.sleeping`,
  `mac.awake`, `worker.suspended_by_sleep`, `worker.terminated_by_sleep`
  per spec §3.7.8).
- Hermes Codex client lifted with attribution → easy upstream sync if
  Hermes evolves.
- jarvis-legacy pricing module rescued from legacy graveyard with no
  semantic change.
- `cost.recorded` lays groundwork for future budget gates without
  enforcing yet.
- Evidence ladder honored: a `verify_command`-gated `task.verified`
  fulfils ADR-0001 line 1021's "Real Codex / pytest integration" promise.
- Reviewer at L3 keeps `lint-imports` green (L4 cannot import
  `LLMClient`).
- `-c` flag injection closes the `~/.codex/config.toml` drift open
  question (Q3 from prior draft).

### Negative

- **Two-LLM-system coupling**: OpenRouter (decision + reviewer) + Codex
  (worker). Failure on either side requires distinct error paths. No
  failover Day-2.
- **Fork-detach is fragile**: child orphaning, signal handling,
  stdout/stderr redirect bugs are all possible. Deferred: `launchd`
  plist supervision.
- **No budget enforcement**: `cost.recorded` is observability only; a
  runaway Codex turn can burn unbounded tokens.
- **No Codex provider failover**: if OAuth dies mid-run, the action
  fails. Hermes's OAuth retry covers transient; persistent auth issues
  surface as `action.failed`.
- **No multi-task disambiguation**: 2+ open tasks in the time window →
  `unknown_subject` fail-fast; Allen must rephrase.
- **Reviewer LLM is jarvis's own LLM**: same provider as decision,
  vulnerable to correlated outages. Reviewer prompt is independent
  context though, so accuracy isn't pinned to decision-LLM state.
- **Sleep/wake is best-effort**: macOS sleep hooks fire before deep
  sleep but can be skipped (kernel panic, power loss, force-kill).
  Reconciliation on wake is the safety net, not a substitute for
  before-sleep flush. Codex subprocess almost always dies through a
  sleep — Day-2 does not attempt to resume; spec §3.7.8 mandates
  fail-closed (`worker.terminated_by_sleep` + Limitation Claim).

### Constraints carried forward

- All ADR-0001 invariants (claim ≤ evidence, no entity_id invention, no
  direct agent-to-state mutation, no action without effective_policy) —
  unchanged.
- Tier 1 < 30s budget is loosened to < 75s (was 45s in the draft; +45s
  for sleep/wake + submit_report stdio-subprocess unit + dual-slot
  verify_diff matrix + relation-column + dirty-tree fixtures). If this
  slips, the subprocess-spawning units (`test_codex_mcp_tools`,
  `test_sleep_wake` fake clock) are the prime suspects — they are the
  only Tier-1 entries with real `Popen` round-trips.

---

## Open questions

1. **Codex subprocess lifecycle when CLI parent dies before child
   finishes** — does Codex orphan cleanly? Hermes's `close(timeout=3.0)`
   handles graceful; abrupt parent-kill is untested. **Day-2 stance**:
   detached child owns the Codex subprocess; parent has already exited
   before Codex spawns; J8 covers crash, not orphan.
2. **Reviewer prompt: free-form vs structured JSON output** — Day-2
   uses **structured JSON** (`{verdict, reasons}`); OpenRouter's
   structured-output mode if available, else
   `response_format={"type":"json_object"}`.
3. **~~Codex config.toml drift~~** — **Resolved by F9.** `-c` flag
   injection at spawn time overrides `~/.codex/config.toml`. Day-2 owns
   model + reasoning effort + sandbox mode + writable_roots. Allen
   edits to `~/.codex/config.toml` no longer affect jarvis runs.
4. **Pricing refresh schedule** — Day-2: manual `uv run python
   scripts/refresh_pricing.py`; no cron. Day-N: `launchd` plist or
   pulled from a CI artifact.
5. **`create_task` natural-language detection** — does the L3 system
   prompt explicitly tell the LLM "if user says '帮我做 X' or '今天/
   明天给我 Y' → call create_task"? Day-2: yes, system prompt
   augmentation in Step 3.
6. **~~Codex's own `submit_report` injection~~** — **Resolved.** Spec
   §3.5.8 mandates worker wrapper injects `submit_report` tool.
   Codex 0.125+'s app-server protocol surface is closed
   (`initialize | thread/start | turn/start | turn/interrupt`); there
   is **no runtime tool-registration JSON-RPC**. Day-2 therefore
   injects via **`-c mcp_servers.jarvis-tools.*` spawn flags** (the
   same `-c` mechanism already used for sandbox config), pointing
   Codex at a standalone Python stdio MCP server
   (`jarvis.execution.codex_mcp_tools`) which exposes the single
   `submit_report` tool. The user's `~/.codex/config.toml` is never
   mutated. Codex discovers the tool through standard MCP `tools/list`
   during initialize. The post-turn guard in the L4
   take_notification loop captures `item/tool_call` events with
   `tool_name == "submit_report"`; if `turn/completed` fires without
   a captured call, jarvis emits `worker.report_missing` + a
   Limitation Claim (prompt-pressure + post-turn-guard enforcement;
   Codex has no force-tool-call primitive). The captured
   WorkerReport is the authoritative `worker.reported` payload;
   jarvis still independently inspects diff + verify_command per
   §3.5.8 last bullet ("Worker report 永远只是 Report Claim").
7. **Time-window LLM prompt for "昨天/今天/上周"** — Day-2: hard prompt
   example with anchor "current epoch_ms = X"; asks for
   `{since_ts, until_ts}` JSON.
8. **When can a `verify_command`-less task reach `verified`
   evidence?** — Day-2: **never automatically**. The
   `claim.accepted` mechanism (Allen-accepted override) is out of scope
   for ADR-0002; flagged for Day-N. Tier 2 L1 enforces this: any
   verify_command-less run ending in `task.verified` is a regression.
9. **(New)** **Sleep during a 3-min Codex turn** — **Resolved by
   reverse-deferral.** Spec §3.7.8 sleep/wake is implemented at the
   Mac-only minimum (deviation V4): IOPM observer + reconcile_after_wake
   handle the case fail-closed. Codex subprocess is treated as
   non-resumable across sleep; Day-2 does not attempt to bridge.
10. **(New)** **Cross-Codex-version forward compatibility** — Day-2
    pins Codex ≥ 0.125.0 via preflight (J1). The `-c
    mcp_servers.<name>.*` config-flag mechanism is part of Codex's
    stable on-disk config schema, so the MCP injection path is
    forward-compatible across 0.125 → 0.130+; the JSON-RPC protocol
    surface itself is the larger compatibility risk. When Codex
    changes any of the four method names jarvis depends on
    (`initialize`, `thread/start`, `turn/start`, `turn/interrupt`)
    or the `item/tool_call` notification shape, this ADR will need
    a v1.1 patch. Day-N task.
11. **(New)** **Codex's `model_reasoning_effort=xhigh` cost** — One
    flagship run is now ~$0.60–$2.50 (rough; reviewer LLM at Step 9
    adds ~$0.10–$0.50 per run on top of the Codex turn cost — a
    separate fresh-context gpt-5.5-deep call of ~5–15k tokens each
    direction). The verify_command subprocess (up to 600s wall) is a
    latency cost, not a dollar cost; the codex_mcp_tools subprocess
    spawn is negligible. `cost.recorded` provides observability for
    every LLM call (decision rounds + reviewer + Codex turns), but **no
    per-task ceiling is enforced Day-2** — Allen's prior call. Day-N
    adds budget gates if usage runs hot.

---

## Build order acceptance gate

ADR-0002 is **accepted for build** when:

- This document is reviewed and approved by Allen.
- The 22-step build order (Steps 0–21) is converted to a `progress.md` checklist.
- A worktree (`worktree-claude-adr0002` or similar) is created off
  `main` (post-merge of ADR-0001 worktree).
- The Step 0 lift commits land first — clean baseline before any wiring.

Implementation kicks off only after this gate.
