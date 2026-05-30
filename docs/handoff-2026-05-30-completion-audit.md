# Mac-only Completion Audit + Live-Run Findings — 2026-05-30 Handoff

Handoff doc for continuing the unfinished work in a later session. Honest,
no false praise. Everything below is grounded in code reads, a spec consult,
and **real live Codex 0.130 + OpenRouter burns run this session** (not unit
green).

---

## 0. Start-here (next session)

- **Immediate state:** the flagship spine runs live end-to-end; one live-red
  bug found and **fixed + committed this session** (`87e154c`). The remaining
  work is **building the deferred error-path scenarios** (the `_SKELETON_SKIP`
  set — see §4).
- **Recommended next task:** implement **J9 (Codex timeout → `action.timeout_assumed`)**
  as the first error-path, TDD-style; it is the most controllable and proves
  the failure-lifecycle wiring. Then J8 (crash), J4 (heartbeat). See §4.
- **Environment is ready** (codex 0.130, keys, auth all present) — see §5 for
  the exact run commands. Live runs cost real money/time but are unconstrained
  (Allen: cost no object; only flag wall-clock).
- **Method:** `superpowers:systematic-debugging` (reproduce→root-cause→fix) +
  TDD. Don't trust unit-green; benchmark against a live scenario. Never patch
  prompts — fix at gate / state-machine / Result Interpreter / schema.

---

## 1. Honest completion audit (per-layer, adversarially adjusted)

Scores are "built AND wired AND live-exercised," not "unit tests pass." An
adversarial auditor deflated each self-reported number; these are the adjusted
figures.

| Layer | Adjusted % | One-line |
|---|:--:|---|
| L1 Constitution | **14%** | Architecturally **dead** — zero production importers; C1–C6 are frozen text no code reads. |
| L2 State Object | **52%** | Most live-exercised layer, but **5 of 8 spec projections are absent**. |
| L3 Runtime Decision | **62%** | Most-built; spine live. Policy/intent/attention edges static/stub/wrong-on-failure. |
| L4 Capability Execution | **48%** | One excellent live Codex runner; SandboxPolicy/lease/LegacyAdapter + most tool-domains absent. |
| L5 Surface | **36%** | Voice code substantial but **zero live audio**; Mac observer/scheduler/multimodal absent; only 3/7 attention channels live. |
| L6 Deployment | **42%** | Only artifact store fully live; real macOS sleep/wake observer is a **no-op stub**; scheduler absent. |
| Runtime wiring | **62%** | Synchronous `run_turn` spine live; daemon/fork-detach **never drove live Codex**. |

**Two honest headline numbers:**
- **Flagship critical path (the spine): ~60%** — runs live, but evidence is thin
  (one burn per variant, traces gitignored) and error paths are unbuilt.
- **Full Mac-only architecture vs spec: ~43%** — dragged down by absent
  constitution/policy engine, 5/8 projections, most surface inputs, most
  deployment behaviors.

**The single most important caveat (Allen's lens, confirmed true):** unit-green
materially overstates completeness. ~885 unit/canary tests pass + lint/mypy
clean, **but every real integration bug (B-0002/4/7/8/13, and the L5 bug fixed
today) slipped past green unit tests** and was only caught by a live burn.
`live-run-bugs.md:119` itself states Tier-1 green is "not load-bearing for
protocol fidelity to Codex 0.130." Live burn traces are **gitignored / absent
from disk** — every "live green" is prose in `progress.md`, not a committed
reproducible artifact.

Key per-layer specifics worth remembering:
- **L1:** the spec-mandated **dynamic constitution→policy→prompt engine does not
  exist** (constitution-dead + single static `Collaborate` preset + static
  `prompts/jarvis_v1.md`). This is one deferred subsystem, the heart of the spec.
- **L2 absent projections:** Status Board, Mode Runtime, Memory, Entity Registry,
  Drift Watch = **zero code**. Also: `payload bounded` unenforced (8%), `monotonic
  ts` is an index not a guard (25%), `UUIDv7` is actually `uuid4` (spec deviation, 30%).
- **L4 absent:** SandboxPolicy (the spec's L4 reality-check defense层 — biggest
  structural gap), AuthorizationLease enforcement, LegacyAdapter, clipboard/screen
  observe domains. 4 of 6 caller principals are dead enum entries.
- **L5:** B-0005/B-0006 open spec gap — **failure paths route to
  `queue_review`/`silent_log`, so Allen gets no voice/banner on failure**, only
  stdout. Voice (ASR+TTS) wired but ADR-0005 §11 audio smoke still pending Allen.
- **L6:** real `NSWorkspace` sleep/wake observer is a documented no-op
  (`sleep_wake.py:109-135` sets `_registered=True`, zero Cocoa wiring); `flush
  events` unimplemented; no concurrency bound anywhere.

---

## 2. Scenarios: designed vs actually-runnable

### 2a. Designed (what the ADRs expect)
The whole system is one flagship scenario — **"昨天那个 task 给 codex 跑一下，做完审核了再告诉我。"** — exercised end-to-end through all 6 layers, expanded by ADRs:

- **ADR-0001** (Day-1, stub worker): 1 happy + 1 verify-fail.
- **ADR-0002** (Day-2, real Codex, no simplification on critical path): the
  J/K/L invariant matrix —
  - **J** (Real Codex round-trip) J1–J13
  - **K** (Real notification side-effects) K1–K8
  - **L** (Evidence ladder) L1–L5
- **ADR-0003** (Inherent text surface): commit `serve_inherent` daemon, `/submit`, WS streaming.
- **ADR-0005** (Inherent voice surface): wake-word turn, long-press PTT turn, TTS output.

**Done = all Tier-1 + all Tier-2 (J+K+L+A–I) green, no skips.**

### 2b. Actually live now (confirmed by burns THIS session, 2026-05-30)

**A. Truly live end-to-end (real Codex + real LLM) — 5 variants, all GREEN:**
| Scenario | Ladder | File |
|---|---|---|
| Happy → `task.verified` | L2 | `test_flagship.py` + `test_real_codex_flagship.py` (J5/J7/J11/K6/L2) |
| verify_command fails → Limitation, no verified | L3 | `test_real_codex_verify_fail.py` |
| no verify_command → observation-only | L1 | `test_real_codex_empty_diff.py::no_verify` |
| true empty diff → Execution + missing_artifact | L4 | `test_real_codex_empty_diff.py::empty_diff` |
| reviewer fail + no verify → refutes, no verified | L5 | `test_real_codex_reviewer_fail_no_verify.py` **(was live-RED before today's fix; now GREEN)** |

**B. Covered at unit/argv boundary (not live Codex, but legitimate):** J3 (cwd),
J10 (4 sandbox `-c` flags), K1 (`say` argv), K2 (`osascript` 240-trunc). These are
not Event-Log-observable, so they're delegated to `test_codex_action.py` /
`test_notify.py`.

**C. NOT runnable — `_SKELETON_SKIP` skeletons (the unfinished work, see §4).**

Flagship-file precise split: **7 live · 4 unit-argv · 10 skeleton skips**.

---

## 3. Bug found + fixed THIS session (committed `87e154c`)

**Symptom:** `test_real_codex_reviewer_fail_no_verify.py` (L5) live-RED:
`assert verify_semantics == ["observation"]` got `["observation","observation"]`.

**Root cause (event-trace confirmed):** two distinct `verify_diff` actions
(different `action_id`s) — the L3 decision loop **re-proposed `verify_diff`**.
The untrusted Tier-2 LLM (spec §3.4.6), seeing the task still unsettled, proposed
`verify_diff` a second time. **No deterministic guard existed.** Systemic, not
L5-specific: on the happy path (verify_command present) a second `verify_diff`
would emit a **duplicate `task.verified`** (`decision/__init__.py:1135` has no
idempotence check). L5 passed on 2026-05-28 only because the LLM happened to
propose once that burn — it is a flaky, non-deterministic path.

**Spec consult:** spec is **silent** on verify_diff multiplicity (no explicit
"at most once"). But §3.4.3/§3.4.4 (decision engine picks next action from
`open_actions`/`missing_evidence` in the Situation Packet) + §3.4.6 (Tier-2 LLM
is an untrusted calculator → constraints must be deterministic) + the canonical
single `task.verified` support a deterministic guard. ADR-0001 the test's
exactly-one-observation expectation is correct **if** the runtime guards
redundancy — so the fix is in the runtime, **not** by relaxing the test.

**Fix (B):** `jarvis/decision/__init__.py`
- New `_verify_diff_already_proposed_this_turn(conn, turn_id, task_id)` — queries
  the event log for a prior `verify_diff` `action.proposed` for the same
  (task, turn).
- `_dispatch_one_tool_call`: when a `verify_diff` is redundant, override the
  Pre-action Gate to `refuse` (reason `redundant_verify_diff_this_turn`), reusing
  the existing refuse path → the second verify_diff never dispatches. Turn-scoped
  (cross-turn re-verification stays legitimate). No prompt patch.

**Verification (logic + wiring + live):**
- `tests/unit/test_verify_diff_idempotence.py` — 6 LLM-free tests (the detection signal).
- `tests/unit/test_decide_verify_diff_guard.py` — deterministic wiring test
  (scripted stub LLM forces the double-propose; asserts gate refuse + no dispatch
  + loop settles). This is the regression guard — a single live run cannot prove
  it because the double-propose is non-deterministic.
- Tier 1: lint-imports KEPT · ruff clean · mypy strict clean · **885/885** · wall ~13s.
- Live: L5 PASSED + happy flagship (J1/J2/J5/J7/J11/K6/L2) PASSED — real Codex
  0.130 + OpenRouter, no regression. (Note: in the post-fix live run the LLM
  proposed verify_diff once, so the guard wasn't exercised that run — the
  deterministic wiring test is the real proof.)

---

## 4. Unfinished task list — the 10 `_SKELETON_SKIP` error paths

These are **unbuilt features, not broken code** (they `pytest.skip`, not fail).
"Fixing" = flesh the scaffold into real assertions + implement the lifecycle
behavior, TDD-style, then live-verify.

| ID | Scenario | Build priority | Notes / approach |
|---|---|:--:|---|
| **J9** | Codex timeout → `action.timeout_assumed` + kill subprocess | **1st** | Most controllable: short `timeout_s` + a slow Codex task. Audit says `timeout_assumed` handler is half-live — verify the wiring. |
| **J8** | Codex crash → `action.failed(error=codex_subprocess_crashed)` | 2nd | Needs a fault-injection seam to crash the subprocess reliably. |
| **J4** | Long turn (≥30s) emits `worker.heartbeat` chained to `action.running` | 3rd | Controllable with a bigger task. Long-turn liveness signal. |
| **J6** | `is_alive()` False when verify_diff `result_observed` emits | cheap | Observational — may already hold on the existing happy fixture. |
| **J13** | Dirty-tree `git stash push -u` → conflict → `conflict.patch` artifact | mid | `_pop_pending_stashes` short-circuits on clean trees today; the conflict branch has never run at any level. |
| **K3** | CLI parent exits <100ms, never opens SQLite | mid | fork mechanism unit-tested; live-Codex-through-fork is the gap. |
| **K4** | Detached child writes `worker.reported`, separate process observes it | mid | pairs with K3. |
| **K5** | reviewer-fail delivers limitation utterance via physical `say` | mid | reviewer-fail logic already live (L5); only the physical `say` actuation is unproven. |
| **K7** | sleep mid-turn → `worker.suspended_by_sleep` / `terminated_by_sleep` + `timeout_assumed` | later | `test_real_codex_sleep_during_turn.py`; needs the real NSWorkspace observer (currently a no-op stub) wired first. |
| **K8** | `reconcile_after_wake()` idempotent | later | pairs with K7. |

**Do NOT build:**
- **J12** (no `submit_report` → `report_missing`) — **deferred by design** (P-0010
  X-decision + spec §3.5.8); a fully-wired Codex can't be made to organically skip
  `submit_report` without gaming the prompt. Unit-covered in
  `test_spawn_worker_real.py`.
- **L1 in `test_real_codex_flagship.py`** — **redundant**; the real L1 coverage is
  live in `test_real_codex_empty_diff.py::no_verify`.

**Related real gap worth scheduling (not a skeleton):** B-0005/B-0006 — failure
paths give Allen no voice/banner (route to `queue_review`/`silent_log`). Needs an
ADR-0002 amendment or spec edit pinning Limitation→`voice_notify`.

---

## 5. Environment + how to run live (verified ready 2026-05-30)

- `codex-cli 0.130.0` on PATH (≥0.125 ✓) · `OPENROUTER_PROXY_KEY` set (real) ·
  `~/.codex/auth.json` present · Python 3.12.13 · `openai` importable.
- Tier-1 (fast, no network, no cost):
  `uv run lint-imports` · `uv run ruff check .` · `uv run mypy ...` ·
  `uv run pytest tests/unit tests/canary -q`
- Live (real $ + minutes; sandbox must be disabled for network):
  - Flagship file (happy spine + shows skips, ~94s):
    `uv run pytest tests/scenarios/test_real_codex_flagship.py --live-codex --live-llm -v -rs`
  - All negative variants (~5–6 min): the four `test_real_codex_*` files.
  - Full Tier-2 sweep ≈ $4–15, ~10–15 min.
- To inspect a burn's trace: the pytest tmp db at
  `/private/tmp/.../pytest-current/<root>/mac_events.db` — query `events` with
  `sqlite3` + `json_extract(payload_json, '$.…')`.

---

## 6. Decisions & principles to carry forward

- **Live scenario is the benchmark, not unit-green.** Re-burn before trusting
  "done." Traces are gitignored — a green claim ages out.
- **Never patch prompts.** Fix wrong LLM behavior at the gate / state-machine /
  Result Interpreter / schema. (Today's fix: a Pre-action Gate refusal.)
- **Self-decide via spec; escalate only when spec is silent + you'd deviate.**
  Spec silence ≠ permission to relax a correct test; infer from design intent.
- **Tier-2 LLM is untrusted (§3.4.6)** — every invariant the LLM could violate
  needs a deterministic backstop in L3/L4, not prompt goodwill.
- **Scope: Mac before RPi.** smart-home/Hue/cross_domain stay deferred.
- **Hermes alignment (analyzed this session):** Jarvis's `codex_client.py` is
  **already vendored from Hermes** (`agent/transports/codex_app_server.py`,
  marked "DO NOT EDIT — upstream sync only"). Hermes uses Codex as its **own
  brain** (one `api_mode`) and delegates via in-process threads — it has **no
  "spawn Codex as repo-worker + event-sourced lifecycle"** analog. So aligning
  with Hermes cannot supply Jarvis's missing architecture. The one thing worth
  borrowing: Hermes's `codex_runtime.py`/session **robustness** (httpx retry,
  interrupt_check, wedged-session kill/respawn) to harden J8/J9 — port the
  mechanism, but the event-lifecycle mapping (crash→`action.failed`→Limitation)
  is Jarvis's own and must be built.

---

## 7. Commits this session
- `87e154c` fix(decision): refuse redundant verify_diff re-proposal within a turn.
