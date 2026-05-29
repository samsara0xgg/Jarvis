# Real-Codex Tier-2 J/K/L Burn — Design

## Status

Draft 2026-05-28. Approved by Allen (design sign-off this session). Implements
the deferred ADR-0002 Step 20 acceptance suite against a real Codex 0.130 +
real OpenRouter LLM. ADR-0002 itself stays `Proposed`; this fills its Tier-2
acceptance gap incrementally.

## Context

The audit (2026-05-28) found ADR-0002 at 68% completion: the production
critical path is real (`spawn_worker_handler` runs a true Codex JSON-RPC flow,
`verify_diff` is a real dual-slot handler, the reviewer is real), but the
**Tier-2 J/K/L acceptance suite is 100% `pytest.skip(_SKELETON_SKIP)` skeletons**
— 28 named test bodies across 6 `tests/scenarios/test_real_codex_*.py` files
that never run real Codex. The DoD's literal acceptance clause is therefore
unmet: real-Codex end-to-end is entirely unproven by automated tests.

Live environment is verified ready: `codex-cli 0.130.0` on PATH (≥0.125 J1
gate), `OPENROUTER_PROXY_KEY` set (len 67), `~/.codex/auth.json` present (4.3K).

## Goal

Turn the J/K/L skeletons into real, passing acceptance tests that run against
live Codex + live LLM — proving the distinguishing features (real spawn,
submit_report MCP injection, evidence ladder, reviewer gate, task.verified
derivation) actually work end-to-end, not just structurally.

## Decisions (Allen, this session)

1. **Cadence — incremental.** Prove the shared happy-path fixture + the
   event-log-observable assertions green on ONE burn (~$1-3), report, then
   expand to variant scenarios. Not a 28-body big-bang sweep.
2. **Bug-discovery policy — small-fix-auto-continue / large-pause.** When a
   live run exposes a real code bug (P-0001-class shape mismatch, new B-bug):
   - Local fix (field name / shape / parse / plumbing): fix `jarvis/` and
     continue, logged.
   - Needs architecture / cross-layer / ADR-contract change: stop, hand Allen
     the trace + proposed fix, await direction.
   - **Never** weaken an assertion to make it green (ADR-0002: "don't game the
     negative"; "architectural correctness > test green").
3. **Coverage — core first, edges follow-up.** Defer the fiddly injection
   scenarios: J8 (crash injection), J9 (timeout real-wait), J13 (dirty-tree
   stash conflict), `sleep_during_turn` (2). Keep their skeletons + a TODO.

## Fixture Architecture

Mirror the proven Day-1 pattern (`test_flagship.py::live_happy_path_run`): one
module-scoped fixture runs the scenario ONCE against the cloud, freezes the
event-log DB path + `RunTurnResult`, and every test asserts one slice.

New fixture `live_real_codex_happy` (in `test_real_codex_flagship.py`):

1. Build a throwaway git repo (`real_python_repo`: pyproject + a passing
   `tests/test_demo.py`).
2. `bootstrap_runtime_app(runtime_root=tmp)`; set `JARVIS_RUNTIME_ROOT`.
3. Seed `task.created` at now−26h with payload **including `repo_path`
   (→ the throwaway repo) and `verify_command`** — this is the P-0003 fix; the
   Day-1 seed omitted both and `spawn_worker_handler` / `verify_diff` need them
   (`_load_task_record` reads `repo_path` off the raw event; `verify_diff`
   reads `payload["verify_command"]`).
4. `run_turn(runtime, utterance="昨天那个 task 给 codex 跑一下，做完审核了再告诉我。")`
   ONCE — drives real LLM (L3) → real Codex (`spawn_worker`) → `worker.reported`
   → `verify_diff` → reviewer → `task.verified`.
5. Capture `{db_path, result, runtime, spawn_argv?}`; write the G5 token
   artifact.

**Happy-path task goal must be Codex-reliably-completable AND keep
`verify_command` green** (e.g. "add a small pure helper + its passing test").
If Codex's diff breaks pytest, that is the verify-fail path, not a test bug.

## Increment Plan

### Increment 1 — happy fixture + shared, event-log-observable J/K/L (burn #1)

Fill these bodies to assert against the single happy-path capture:

- **J1** codex ≥0.125 preflight ran against real binary
- **J2** `CodexAppServerClient.initialize()` within 5s
- **J3** `thread/start.cwd == task.created.payload["repo_path"]` byte-for-byte
- **J5** `turn/completed` non-empty diff → `worker.reported.status == "ok"`
- **J7** ≥1 `cost.recorded` row `kind == "codex"` correlated to the spawn action
- **J11** `item/tool_call` with `tool_name == "submit_report"` captured exactly once
- **K6** `surface.response_emitted.payload.delivered_via` + `attention_channel`
- **L2** verify (`pytest` exit 0) + reviewer ok → 2 `action.result_observed`
  (observation + verification); Postcondition Claim + verified evidence;
  `task.verified` emitted

Done = these green under `--live-codex --live-llm`; report to Allen with the
captured trace + any code fixes made.

**Load-bearing open risk (confirm as first impl step):** `decision/__init__.py`
has zero `verify_command`/`repo_path` references → L3 may not plumb
`verify_command` onto the `verify_diff` ActionRequest. If so, `verify_diff` gets
`None` → observation-only → **no `task.verified`** → L2 unreachable. Confirm
statically; if missing, a small L3 plumbing fix (read task → put
`verify_command`/`repo_path` on the verify_diff ActionRequest payload) is a
"small-fix-auto-continue" item.

### Increment 2 — standard negative variants (burns #2..#6, each own fixture)

- `test_real_codex_verify_fail.py` (2): Codex diff fails `verify_command` → no
  `task.verified`, Limitation Claim, downgraded language.
- `test_real_codex_no_submit_report.py` (1): J12 — turn completes without
  `submit_report` → `worker.report_missing` + Limitation Claim.
- `test_real_codex_reviewer_fail_no_verify.py` (1): reviewer verdict fail → no
  `task.verified`, K5 limitation phrasing.
- `test_real_codex_empty_diff.py` / L1 (1): no `verify_command` (or empty diff)
  → observation-only slot, Artifact + Limitation Claim, no `task.verified`.

### Follow-up (deferred per Allen) — keep skeleton + TODO

J8 (crash injection), J9 (timeout real-wait), J13 (dirty-tree stash conflict),
`test_real_codex_sleep_during_turn.py` (2), and the flagship-file stubs J4/J6
(heartbeat / client-dead timing — assert opportunistically if the happy run
surfaces them, else TODO).

## Observability Seams

- Read from the event log wherever it carries the fact (J3 cwd, J5 status, J7
  cost, K6 delivered_via, L2 task.verified, claim/evidence rows).
- For facts only visible at the subprocess boundary (J10's four `-c` flags in
  `Popen.args`, K1/K2 `say`/`osascript` argv, J11 `item/tool_call`): add a
  minimal `conftest`-level capture seam (env/path-level, ADR-0002 G2-compliant
  — must not replace `LLMClient`/`decide`/tool outputs/scenario behavior), OR
  assert via an equivalent event the runtime already emits. Decide per-test at
  implementation time, preferring the event-log route.

## Known Risks

- **P-0003** — Day-1 seed omits `repo_path`/`verify_command`; the new fixture
  fixes this. (Done by design above.)
- **P-0001** — Tier-1 mocks were invented (flat `threadId` vs real nested
  `thread.id`, per B-0002). The live burn may expose more such shape mismatches;
  handle per the small-fix policy.
- **verify_command plumbing** (above) — load-bearing for L2 reachability.
- **K1/K2 real side-effects** — these make the Mac actually speak (`say`) and
  show notifications (`osascript`). Increment 1 does not touch K1/K2.
- **Non-determinism** — real Codex/LLM output varies; assert structural facts
  (event types, status, derived state) over exact wording, with the ADR's ±
  tolerances.

## Cost Envelope

Increment 1: 1 happy burn ≈ $1-3. Increment 2: ≈5 variant burns ≈ $4-15 total.
Follow-up: not burned. (Matches ADR-0002 Open Question 11.)

## Acceptance / Done

- Increment 1: the 8 listed bodies pass under `--live-codex --live-llm`; Tier-1
  (875 unit+canary, ruff/mypy `jarvis/`, lint-imports) stays green; any code
  fix made is logged in `docs/progress.md`.
- Overall: J/K/L core green; follow-up edges tracked as named TODOs so a
  `--collect-only` run still shows the full J1-J13 / K1-K8 / L1-L2 enumeration.
