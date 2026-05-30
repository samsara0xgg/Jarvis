# ADR 0001 — Mac-only Flagship Scenario (Day-1 MVP)

## Status

Approved 2026-05-17. Supersedes the v1 draft after critique review and
Allen's clarifications on (a) identity (Jarvis only, Xiaoyue discarded),
(b) real-LLM-only testing (no mocks of any kind including recorded
playback), (c) LLM provider locked to OpenRouter + gpt-5.5 with no
budget ceiling, (d) reference-source policy (agent has discretion on
reuse depth).

## Context

`spec.html` defines the canonical 6-layer state-centric runtime.
`architecture-mac-only.md` scopes Day-1 to a single Mac domain.

Day-1 has **two available reference sources** the implementer may
consult and adapt from:

1. `/Users/alllllenshi/Projects/jarvis-legacy/` — the previous complete
   Jarvis Python implementation (flat single-package architecture).
2. `/Users/alllllenshi/Projects/hermes-agent/repo/` — the Hermes agent
   codebase. Notably `tools/registry.py` (563 lines) is the canonical
   reference for tool registration / dispatch shape; `tools/*.py`
   collectively are the gold standard for tool definition format.

The full Day-1 reuse map across both sources is in
`docs/adr/0001-legacy-scan.md` (Build Step 0 output, written
2026-05-17). This ADR locks the scope, scenario, acceptance, and build
order; it does not re-enumerate the per-file scan.

Allen wants the first scenario to:

- exercise **every layer** end-to-end (L1 invariants → L6 storage)
- prove the parts of the spec that make Jarvis distinctive (agent
  protocol, evidence model, three-gate system, async lifecycle, task
  continuity across sessions) — not just the trivial spine
- be the smallest scenario that does so
- use a **real cloud LLM** for any L3 reasoning. **No mocks of any
  kind** — including `unittest.mock`, recorded-response playback,
  deterministic LLM fixtures, or stubbed `LLMClient.chat()`. Tests of
  LLM-free components (Event Log, projection fold, ActionLifecycle
  state machine, gate logic given pre-constructed inputs) are not
  considered mocks because they do not involve the LLM at all.
- consult available reference sources where useful; the implementer has
  discretion on adaptation depth. When stuck, the reference sources are
  the escape hatch.
- have **strict, machine-verifiable acceptance** because subsequent
  iteration will be driven by an autonomous loop (`/loop` + Codex /
  Claude Code). Any soft criterion the loop can satisfy by short-circuit
  is a failure mode.

A trivial scenario (`what time`, `read clipboard`) touches all 6 layers
structurally but exercises ~30% of the architecture — agent / task /
evidence / continuity mechanisms never fire. Rejected.

## Decision

### Scenario

> Allen: *"昨天那个 task 给 codex 跑一下，做完审核了再告诉我。"*

Three keywords in one scenario:
**spawn subagent + verify-then-review + continue yesterday's task.**

Day-1 simplifications:

- Test fixture seeds **exactly one** open task created 24h+ ago in the
  Task Ledger. Entity resolution is therefore unambiguous; multi-task
  disambiguation (ConfirmationRequest path) is deferred to Stage 2.
- A resolver code path **must still exist** — LLM extracts a natural
  reference (`"yesterday's task"`) and L3 calls
  `resolve_task_ref(natural_ref, task_ledger_snapshot)` to produce the
  canonical `task_id`. LLM must not invent entity IDs (spec §3.3.7,
  §3.4.10). Single-candidate fuzzy resolution emits
  canonical `entity.resolved(confidence=fuzzy, resolver_warning=true)`;
  multi-candidate ambiguity emits `entity.resolved(outcome=ambiguous)`.
- Memory system is **not used** and **not built**. Task persistence is
  satisfied entirely by the Task Ledger projection (folded from
  `task.*` events), which is event-sourced, not memory-sourced.

### Identity

Jarvis. Architectural identity remains owned by L1 Constitution and
durable L2 state per spec §3.2 / §3.3. Day-1 implements this as:

- L1 Constitution exposes the product identity label `Jarvis` plus
  C1–C6 and autonomy axes as frozen values.
- L2 persists the durable system/task/claim state; Day-1 has no
  separate personality projection.
- `prompts/jarvis_v1.md` is the single **L3 prompt text asset** copied
  verbatim from legacy `prompts/phase1/v1.md`; it is not the
  architectural source of identity.

The "Xiaoyue / 小月" persona from legacy `core/personality.py` is
**discarded entirely** (not deferred to Stage 2).

### Reference sources (available, not mandated)

The implementer is encouraged to consult, adapt, and copy from the
following two reference roots. Adaptation depth is at the implementer's
discretion; if a clean adaptation is not obvious or yields more
complexity than a fresh write, write fresh. When stuck on a design
question (tool schema shape, LLM client interface, prompt-block
ordering), use these as the escape hatch.

**1. `/Users/alllllenshi/Projects/jarvis-legacy/`** — previous Jarvis
implementation.

Files with high Day-1 reuse value (full per-file analysis in
`docs/adr/0001-legacy-scan.md`):

| Legacy file | Day-1 use | Notes |
|---|---|---|
| `prompts/phase1/v1.md` | **Verbatim** copy to `prompts/jarvis_v1.md` | 164 lines, 11 XML-tagged sections |
| `config.yaml` lines 277–301 (LLM section) | **Verbatim** copy to `config/jarvis.yaml` `llm:` block | OpenRouter + gpt-5.5 deep preset |
| `core/llm.py` (1650 LOC) | Adapt to `jarvis/decision/llm.py` | Multi-provider client + tool-use loop + metadata. Trim `core.personality` + `memory.hot.assembler` deps; replace `prompt_context` parameter with `system: str` (Q3 option (a)). Keep `chat_stream` code unused for Stage 2. Pass `tracker=None` Day-1. |
| `core/tool_result.py` (500 LOC) | Adapt vocabulary into `jarvis/decision/__init__.py` (Result Interpreter) | `outcome.type` / `verification_source` / `claim_policy { allowed_claims, forbidden_claims }` near-1:1 with spec §3.4.11 + §5.3 + §8 |
| `core/response_channels.py` (52 LOC) | **Verbatim** copy / embed in `jarvis/surface/cli.py` | Parses `<voice>` / `<document>` from prompt v1 output |
| `tools_v2/registry.py` (178 LOC) | Adapt to `jarvis/execution/tools.py` | Borrow `ToolEntry` shape + register/dispatch/get_definitions. Swap `caller_scope: set[str]` for `Set[CallerPrincipal]` enum. Add ActionLifecycle 8-state orchestration. |
| `tools_v2/helpers.py` (59 LOC) | **Verbatim** copy / embed | `tool_error()` / `tool_result()` JSON serializers |
| `core/regex_router.py` (325 LOC) | Pattern reference only | Day-1 Tier 0 is empty scaffold; consult only when first deterministic shortcut is added (Stage 2) |
| `jarvis.py` `JarvisApp.__init__` | Pattern reference only | Composition root shape (single class, single owner of cross-layer wiring) |

**2. `/Users/alllllenshi/Projects/hermes-agent/repo/`** — Hermes agent
codebase.

| Hermes file | Day-1 use | Notes |
|---|---|---|
| `tools/registry.py` (563 LOC) | **Canonical reference** for tool registration shape | When `jarvis/execution/tools.py` design questions arise (e.g. toolset membership, schema cache invalidation, registry threading model), Hermes is the gold standard. Day-1 may stay closer to the slimmer `tools_v2/` shape; Stage 2 real tools should align to Hermes. |
| `tools/*.py` collectively | **Canonical reference** for tool definition format | input_schema construction, error envelope shape, registration call site. Use when designing real tools in Stage 2. |

### What is explicitly discarded (not deferred)

Per Allen's directive ("所有能重写的全部重写"):

- `core/personality.py` — Xiaoyue persona, not Day-1, not Stage 2.
- `core/event_bus.py` — pub/sub model conflicts with spec §3.3.1.
- `core/tool_registry.py` — superseded by `tools_v2/registry.py`.
- `core/command_parser.py` — Hue-specific.
- `auth/permission_manager.py` — name collision; device-RBAC ≠
  Pre-action Gate. Implementer must Legacy-bypass this file explicitly
  when Pre-action Gate ships.
- `memory/cold/outcome_detector.py` — marked DEPRECATED in source.

### AuthorizationLease (Day-1 treatment)

Defined as TypedDict in `jarvis/shared/__init__.py`. Day-1 scenario uses
only `spawn_worker` (L2 task action) and `verify_diff` (L0 read);
neither requires a lease. `ActionRequest.authorization_lease` is always
`None` on Day-1. Type skeleton exists for Stage 2 high-risk scenarios.

### Stub strategy

| Layer | Real | Stub |
|---|---|---|
| L1 Constitution | C1–C6 + 5 axes as frozen module | — |
| L2 Event Log | SQLite at `${JARVIS_RUNTIME_ROOT}/mac_events.db`, append-only with write-side immutability enforcement | — |
| L2 Event Type Registry | minimum schemas for the ~21-event scenario trace | — |
| L2 Projections | Task Ledger + Recent Trace + Claim/Evidence (pure fold) | Memory / Drift Watch / Mode Runtime State / Status Board — not built Day-1 |
| L3 Situation Packet assembler | real, reads projection snapshots | — |
| L3 Effective Policy Resolver | real, Collaborate mode preset only | other modes deferred |
| L3 Intent Router Tier 0 | empty scaffold (`match()` returns None) | per Allen's confirmation, no patterns Day-1 |
| L3 Intent Router Tier 2 | **real cloud LLM** via OpenRouter + gpt-5.5 | — |
| L3 invariant gates | real Pre-action / Post-action (Result Interpreter) / Pre-emit | — |
| L3 Resolver | real `resolve_task_ref(natural_ref, ledger_snapshot)` emitting canonical `entity.resolved` | — |
| L3 Attention Policy | real but minimal — only `voice_notify` / `silent_log` / `queue_review` | other 7 channels map to `silent_log` |
| L4 ToolRegistry + lifecycle | real ActionLifecycle 8-state | — |
| L4 `spawn_worker` | **stub** — writes a real artifact (`${JARVIS_RUNTIME_ROOT}/artifacts/run_<R1>/diff.json` with `{"status":"ok"}` or controllable equivalent), then schedules `worker.reported` via `threading.Timer` for true async lifecycle | — |
| L4 `verify_diff` | **stub** — reads the real artifact, checks predicate (`json["status"] == "ok"`), returns verification semantics on match, error on miss | — |
| L4 `read_state` | removed; Task Ledger reads happen inside L3 Situation Packet assembler, not via L4 tool | — |
| L5 Surface | CLI adapter only (stdin / stdout) | Voice / Inherent / Mac notification deferred |
| L6 Deployment | `JARVIS_RUNTIME_ROOT` env var (default `~/.jarvis/`); single Mac domain | sleep/wake protocol deferred |

### Six-layer boundary contract (Day-1)

Day-1 implementation must preserve spec §3 ownership, even where the
current `.importlinter` contract is less specific than the product
architecture.

- **L1 Constitution** owns product identity label `Jarvis`, C1–C6,
  non-goals, and autonomy axes. It does not own prompt wording,
  runtime config, tool permission decisions, UI rendering, or event
  storage.
- **L2 State Object** owns Event Log, EventTypeRegistry, projections,
  Task/Claim/Evidence derivation, and entity-resolution records. It
  never executes tools, resolves policy, renders UI, or stores task
  status as mutable truth.
- **L3 Runtime Decision** owns Situation Packet assembly, Effective
  Policy, Intent Routing, Resolver, Pre-action Gate, Result
  Interpreter, Pre-emit Gate, Attention Policy, and ResponsePlan. It
  reads L2 through projection APIs only; it does not reach into SQLite
  tables, write artifacts, or render stdout. Because Mode Runtime
  State is deferred Day-1, the Collaborate preset is static config;
  when mutable mode state exists, it belongs in L2 rather than L3.
- **L4 Capability Execution** owns ToolRegistry, ActionLifecycle,
  worker/tool adapters, raw results, and artifact writes. It executes
  only a gated `ActionRequest`; it never decides permission, verifies
  task truth, or writes final user language.
- **L5 Surface** owns CLI input capture, canonical `utterance.received`
  emission, response-channel parsing, and stdout rendering of an
  already approved `ResponsePlan`. It does not perform intent routing,
  entity resolution, policy checks, evidence interpretation, or task
  verification.
- **L6 Deployment** owns local path bootstrap, runtime-root selection,
  artifact-store placement, and Mac-only process placement. It does
  not define event semantics, mutate projections, decide policy, or
  interpret claims. Day-1 is stricter than the generic layer DAG:
  `jarvis/deployment` must not import `jarvis.state`; `jarvis/runtime`
  passes provisioned paths into L2 during composition.
- **Runtime composition root**: `jarvis/runtime/__init__.py` is the
  only module allowed to wire multiple layer siblings together. Other
  layers communicate through typed contracts (`Event`, projections,
  `ActionRequest`, `RawResult`, `ResponsePlan`) rather than importing
  sibling implementations.

**Async lifecycle pattern.** The `spawn_worker` stub synchronously
emits `action.running`, then schedules `worker.reported` via
`threading.Timer(delay, ...)`. L3 is re-invoked by the
`worker.reported` event on the timer thread (or test clock), not by a
blocking sleep in the main flow. This exercises true event-triggered
re-entry per spec §3.4.1.

**Verification is real predicate, not canned.** `verify_diff` actually
reads the artifact file the worker wrote and checks a predicate.
"Verified by Jarvis" therefore corresponds to a real artifact + real
predicate match. The negative test case (F below) flips the stub to
write a failing artifact (`{"status": "fail"}`); `verify_diff` returns
an error, no Postcondition Claim is emitted, language downgrades.

**No mocks of any kind.** Tests run against a real cloud LLM via the
adapted legacy client. Any `unittest.mock`, recorded-response playback,
or `LLMClient` substitution is a regression. See § Acceptance criteria
Tier 2 § G.

### Resolver contract

`resolve_task_ref` is L3's entity-resolution choke point. Spec §3.3.7
and §3.4.10 forbid the LLM from inventing entity IDs; this contract is
how that rule is enforced on Day-1.

**Signature:**

```python
def resolve_task_ref(
    natural_ref: str,
    ledger_snapshot: TaskLedgerSnapshot,
) -> ResolverResult: ...

@dataclass(frozen=True)
class ResolverResult:
    resolved_to: TaskId | None       # canonical task_id, None if no match
    confidence: Literal["exact", "high", "fuzzy", "none"]
    candidates: list[TaskId]         # ranked candidates, empty if none
    match_basis: str                 # short string explaining the match
```

**Hard rules** (each enforceable by inspection / canary):

- No LLM call inside the resolver body. Resolver is a pure function
  over `(natural_ref, ledger_snapshot)`. AST scan rejects any import
  of `jarvis.decision.llm` inside `resolver.py` or wherever the
  function lives.
- Resolver **must** consult `ledger_snapshot` — empty `candidates`
  list is allowed (no match), but a `resolved_to` value that is not
  in `ledger_snapshot.open_tasks()` is a contract violation.
- Day-1 single-candidate behavior: when exactly one open task exists
  and `natural_ref` is non-empty, resolver returns
  `confidence in {"exact", "high", "fuzzy"}`, `resolved_to=that_task`,
  and emits `entity.resolved(outcome=resolved)` (single-candidate path) — never
  silently `none`.
- Multi-candidate behavior (Stage 2, not exercised Day-1): resolver
  returns `confidence="fuzzy"` with `candidates=[t1, t2, ...]` and
  emits `entity.resolved(outcome=ambiguous, resolver_warning=true)`;
  caller (L3) is then expected to issue a `ConfirmationRequest`.
- No-match behavior: returns `confidence="none"`, emits
  `entity.resolved(outcome=failed, resolved_to=null)`.

**Events emitted by L3 after consuming `ResolverResult`:**

| `ResolverResult.confidence` | event emitted | payload required fields |
|---|---|---|
| `exact` / `high` | `entity.resolved(outcome=resolved)` | `entity_type="task"`, `natural_ref`, `resolved_to`, `confidence`, `candidates`, `match_basis` |
| `fuzzy` (single candidate) | `entity.resolved(outcome=resolved, resolver_warning=true)` | as above (candidates list length 1) |
| `fuzzy` (multi candidate) | `entity.resolved(outcome=ambiguous, resolver_warning=true)` | as above (candidates list length > 1) |
| `none` | `entity.resolved(outcome=failed)` | `entity_type="task"`, `natural_ref`, `resolved_to=null`, `confidence="none"`, `candidates=[]`, `match_basis` |

`candidates` field must be **non-empty** when
`entity.resolved.outcome in {"resolved", "ambiguous"}`. Empty candidates
with non-None `resolved_to` = bug.

### Gate contracts

The three invariant gates (Pre-action / Post-action Result Interpreter /
Pre-emit) are not just `gate.evaluated` event emitters — they have
substantive contracts.
Emitting the event without running the checks below is a short-circuit
and a regression.

**Pre-action Gate** (spec §3.4.10):

- Inputs: `ActionRequest`, `EffectivePolicy`, `EntityRegistry` /
  `TaskLedgerSnapshot`.
- MUST check, in order:
  1. `caller_principal` is allowed to call this `tool_name` per
     `EffectivePolicy.tool_surface(caller_principal)`.
  2. `target_entity_ref` resolves to a trusted entity
     (`TaskLedgerSnapshot.open_tasks()` membership for task IDs;
     extend to entity registry when Stage 2 adds devices).
  3. `risk_level` ≤ `EffectivePolicy.autonomy_ceiling`.
  4. If `risk_level >= L3`, `authorization_lease` is non-None and
     valid (not expired, scope covers `tool_name` and
     `target_entity_ref`).
- Output: `GateResult { outcome: pass | refuse | confirm_required,
  reasons: list[str] }`.
- Emits: `gate.evaluated(gate=pre_action, action_id, outcome, reasons)`
  before `action.authorized` (on pass) or instead of
  `action.authorized` (on refuse / confirm_required).

**Post-action Gate / Result Interpreter** (spec §3.4.11, §13.1):

- Inputs: raw `ActionResult` from L4, tool's `result_semantics`,
  source `ActionRequest`.
- MUST map `result_semantics` → `evidence.level` per the legacy
  `core/tool_result.py` claim_policy table (vocabulary explicitly
  borrowed for Day-1):

  | `result_semantics` | allowed claim type | allowed evidence level |
  |---|---|---|
  | `ack` | Execution | `executed` |
  | `observation` | Artifact / State | `observed` |
  | `verification` | Postcondition | `verified` |
  | `report` | Report | `reported` |
  | `error` | Limitation / Refute | `reported` (limitation) |

- MUST NOT emit `evidence.level` exceeding the row for the actual
  `result_semantics` returned by L4.
- Outputs: list of new events
  (`claim.created` + `evidence.attached`).
- Each emitted event has `source_event_id` pointing to the
  `action.result_observed` it interpreted.

**Pre-emit Gate** (spec §3.4.12):

- Inputs: draft response text (from L3 LLM tool-use loop), current
  Claim/Evidence projection (for the active subject — e.g.
  `task_X`).
- MUST check:
  1. The strongest evidence level supporting completion claims for
     the active subject.
  2. Whether the draft response contains "completion-class" language
     (per the `consequential_claim` keyword set: `完成` / `已完成` /
     `verified` / `done` / etc.).
- Output: `ResponsePlan { permission: allow_completion_language |
  force_limitation_language, downgrade_required: bool,
  active_claim_levels: list[str] }`.
- Emits: `gate.evaluated(gate=pre_emit, outcome, claim_levels)`
  before any byte of the response reaches the L5 surface.
- `outcome = allow_completion_language` only when at least one
  Postcondition Claim with `evidence.level=verified` (or
  `level=accepted`) exists for the active subject.
- `outcome = force_limitation_language` otherwise. When forced, the
  surface adapter must rewrite or refuse the draft (Day-1: refuse
  + re-prompt LLM via a system note, or downgrade phrase per
  template).

### Prompt

`prompts/jarvis_v1.md` — verbatim copy of legacy
`prompts/phase1/v1.md`. Already encodes voice/document split (§18),
weak/medium/strong evidence levels (§8),
`reported_complete` vs `trusted_complete` (§13.2 I8/I11), tool error
handling (§3.5), Chinese-default voice. Its prompt copy names Jarvis;
architectural identity still comes from L1/L2 per § Identity above.

### Configurable paths

All runtime state paths are derived from `JARVIS_RUNTIME_ROOT`
(default: `~/.jarvis/`). Tests must override to a temp directory via
fixture. Hardcoded `~/.jarvis/` references in source = regression.

```
${JARVIS_RUNTIME_ROOT}/mac_events.db          (event log SQLite)
${JARVIS_RUNTIME_ROOT}/artifacts/run_<R>/...  (worker artifact store)
${JARVIS_RUNTIME_ROOT}/registry.json          (EventTypeRegistry, if file-backed)
```

### Day-1 EventTypeRegistry extensions

Day-1 uses every canonical spec event type when one exists. In
particular, resolver output uses spec §5.2's canonical
`entity.resolved` event type, not `resolver.*`.

Day-1 adds exactly one local audit event type:

```json
{
  "event_type": "gate.evaluated",
  "owner_layer": "L3",
  "producer": "Runtime Decision",
  "required_payload": ["gate", "outcome", "reasons"],
  "optional_payload": [
    "action_id",
    "response_hash",
    "claim_levels",
    "check_results"
  ],
  "correlation_fields": ["action_id", "turn_id", "run_id"],
  "projection_consumers": ["Recent Trace"],
  "evidence_semantics": "none",
  "cross_domain_policy": "local_only",
  "artifact_policy": "none",
  "schema_version": 1
}
```

This event is audit instrumentation for the runtime gates. It does not
replace ActionLifecycle events, Claim/Evidence events, or ResponsePlan
contracts.

### Lint configuration (full)

`pyproject.toml` additions (Step 1):

```toml
[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["ALL"]
ignore = [
    "D100",  # missing module docstring — many small modules don't need one
    "D104",  # missing docstring in public package — __init__.py files
    "D203",  # one-blank-line-before-class — conflicts with D211
    "D212",  # multi-line-summary-first-line — conflicts with D213
    "COM812",  # trailing comma — conflicts with ruff format
    "ISC001",  # implicit string concatenation — conflicts with ruff format
]

[tool.ruff.lint.pydocstyle]
convention = "google"

[tool.mypy]
python_version = "3.12"
strict = true
disallow_untyped_defs = true
disallow_any_explicit = false  # too noisy with TypedDict + sqlite Row
warn_unused_ignores = true
warn_return_any = true
warn_unreachable = true
no_implicit_optional = true
check_untyped_defs = true

[[tool.mypy.overrides]]
module = "tests.*"
disallow_untyped_defs = false
```

`.importlinter` is already configured (existing file). Add no new
import-linter contracts Day-1; stricter Day-1 ownership rules are
covered by canary H13.

Every new entry to the `ignore` list above must be accompanied by a
one-line code comment in `pyproject.toml` justifying the rule
exemption. Loop-driven attempts to extend the ignore list without
justification = regression caught by canary H6.

Step 1 also adds the runtime dependencies needed by the Day-1 module
map:

```toml
dependencies = [
    "openai",
    "anthropic",
    "PyYAML",
]
```

`openai` is required for the OpenRouter OpenAI-compatible client.
`anthropic` stays because the adapted legacy client preserves the
provider switch, even though Day-1 defaults to OpenRouter. `PyYAML` is
required to load `config/jarvis.yaml`.

### Module map (Day-1)

```
docs/adr/0001-mac-only-flagship-scenario.md   (this file)
docs/adr/0001-legacy-scan.md                  (Build Step 0 output)
prompts/jarvis_v1.md                          (L3 prompt text asset; legacy verbatim; not L1 identity source)
config/jarvis.yaml                            (runtime config; legacy LLM section verbatim; not state)

pyproject.toml                                 (ratchet to ruff ALL + mypy --strict per § Lint configuration)

jarvis/constitution/__init__.py                (L1) C1–C6 + autonomy axes, frozen
jarvis/shared/__init__.py                      types: Event, Claim, Evidence, ActionRequest, AuthorizationLease, CallerPrincipal, PromptContext (Day-1: dataclass with `system: str` only)
jarvis/deployment/__init__.py                  (L6) JARVIS_RUNTIME_ROOT bootstrap; mac_events.db / artifacts/ path provisioning
jarvis/state/event_log.py                      (L2) SQLite + emit_event + EventTypeRegistry + append-only enforcement (write-time guard)
jarvis/state/projections.py                   (L2) Task Ledger + Recent Trace + Claim/Evidence (pure fold from events)
jarvis/execution/tools.py                      (L4) ToolRegistry + ActionLifecycle 8-state + spawn_worker stub + verify_diff stub. Embeds helpers from legacy tools_v2/helpers.py.
jarvis/decision/llm.py                         (L3) adapted from legacy core/llm.py (trimmed deps, system: str parameter)
jarvis/decision/__init__.py                    (L3) decide() entry + Situation Packet + Effective Policy Resolver + Intent Router + Resolver + 3 Gates + Result Interpreter (vocabulary borrowed from legacy core/tool_result.py)
jarvis/surface/cli.py                          (L5) stdin/stdout adapter + Pre-emit Gate boundary + response_channels parser (legacy core/response_channels.py)
jarvis/runtime/__init__.py                     composition root — only place crossing layers
jarvis/cli/__init__.py                         CLI entry point (`python -m jarvis "..."`)

tests/conftest.py                              fixtures: temp JARVIS_RUNTIME_ROOT, seeded task fixture, real LLM key check, LLM client teardown
tests/unit/                                    (Tier 1) per-module unit tests, no LLM
tests/scenarios/test_flagship.py               (Tier 2) end-to-end happy path, real LLM
tests/scenarios/test_flagship_verify_fails.py  (Tier 2) negative case, verify stub flipped
tests/canary/                                  (Tier 1) anti-bypass static + runtime checks
```

### Canonical event trace (acceptance reference)

Each canonical event is its own row in `events`. No event lumping.
Numbers below are illustrative ordering; exact count tolerance is in
acceptance criterion A1.

```
T-1day  (fixture, seeded by tests/conftest.py)
  evt 00  task.created(task_id=task_X, goal="...", source=manual)

T-0     (Allen utterance through CLI)
  evt 01  turn.started(turn_id=T1)
  evt 02  utterance.received(transcript="...", turn_id=T1)
  evt 03  entity.resolved(entity_type=task,
                          natural_ref="yesterday's task",
                          resolved_to=task_X,
                          confidence=high,
                          outcome=resolved)

  evt 04  action.proposed(spawn_worker, action_id=A1, run_id=R1,
                           caller=jarvis_llm)
  evt 05  gate.evaluated(pre_action, action_id=A1, outcome=pass)
  evt 06  action.authorized(action_id=A1)
  evt 07  action.dispatched(action_id=A1)
  evt 08  action.running(action_id=A1)
  evt 09  run.started(run_id=R1, task_id=task_X, runner=codex_stub)

  [threading.Timer fires worker.reported after delay]

  evt 10  worker.reported(run_id=R1, action_id=A1,
                           status=reported_complete,
                           summary="codex stub wrote diff artifact")
  evt 11  action.result_observed(action_id=A1, semantics=report)
  evt 12  claim.created(claim_id=C1, type=Report,
                         statement="codex reported complete")
  evt 13  evidence.attached(evidence_id=E1, claim_id=C1, level=reported)

  [L3 reinvocation triggered by worker.reported]

  evt 14  action.proposed(verify_diff, action_id=A2, run_id=R1,
                           caller=observer)
  evt 15  gate.evaluated(pre_action, action_id=A2, outcome=pass)
  evt 16  action.authorized(action_id=A2)
  evt 17  action.dispatched(action_id=A2)
  evt 18  action.running(action_id=A2)
  evt 19  action.result_observed(action_id=A2, semantics=verification)
  evt 20  claim.created(claim_id=C2, type=Postcondition,
                         statement="diff predicate satisfied")
  evt 21  evidence.attached(evidence_id=E2, claim_id=C2, level=verified)

  [L3 reinvocation triggered by action.result_observed]

  evt 22  task.verified(task_id=task_X, by=jarvis)
  evt 23  gate.evaluated(pre_emit, claim_levels=[verified],
                          outcome=allow_completion_language)
  evt 24  turn.ended(turn_id=T1)
```

24 events on the happy path (excluding fixture seed). Negative path
(verify fails) ends with no `task.verified`, a Limitation Claim with
`evidence.attached(level=reported, scope=...)`, and a downgraded
Pre-emit Gate verdict — see acceptance criterion F.

## Acceptance criteria

### Acceptance philosophy

These criteria are designed to be machine-verifiable signals that the
**architecture was actually implemented**, not signals that a clever
agent emitted the right event names. Read this section before writing
code:

- **Implement the contracts, then the tests pass.** Do not work
  backward from acceptance assertions to minimum-effort code. Every
  gate, every projection, every lifecycle transition has a substantive
  contract in this ADR — implement that contract per spec, and
  acceptance will follow.
- **Architectural correctness > test green.** If you find a way to
  pass an acceptance assertion that violates the contracts above (e.g.,
  emitting `gate.evaluated(outcome=pass)` without running the gate's
  MUST-check list), that is a regression even if the test is green.
  Canary tests are designed to catch this, but they cannot catch every
  case — the agent must hold the line.
- **Don't game the negative.** The negative case
  (`test_flagship_verify_fails.py`) is the most important acceptance
  signal in this ADR. If the happy path passes but the negative case
  does not produce a downgraded Pre-emit verdict, the Evidence Model is
  not actually implemented. Do not "fix" the negative case by adding
  hardcoded limitation language — fix the Pre-emit Gate logic.
- **Layer discipline holds at all times.** `lint-imports` is not a
  ceremony — every import must respect the 6-layer DAG. If a circular
  dependency tempts you, that is a sign the responsibility is in the
  wrong layer. Restructure; do not silence the linter.
- **Reference sources are help, not crutch.** Adapt freely from
  `jarvis-legacy/` and `hermes-agent/`, but never copy code that
  conflicts with the contracts above. If a legacy file looks reusable
  but its behavior contradicts a spec section, write fresh and
  Legacy-bypass.

### Process discipline

The Build order (below) is a sequence, not a menu. Each step has a
verification gate. The next step does not begin until the current
step's verification is green.

- **Step N may not be started before Step N-1's verification is
  green.** This applies even when steps look independent (e.g.,
  Steps 5 and 6 both touch projections / tools — Step 5 still goes
  first because Step 6 needs L2 in place to register lifecycle
  events).
- **Every step ends by running its verification gate.** If an available
  Tier 1 check regresses inside any step, fix before proceeding.
- **No skipping ahead to make scenario tests pass.** If any step reveals
  that a scenario test needs something not in the current module map,
  raise an ADR amendment — do not silently add a file.
- **Commits within a step are fine; the verification gate runs once
  per step.** This ADR does not prescribe commit cadence.
- **`Legacy-bypass:` commit annotation is required** when a module
  could plausibly have reused a legacy file but the implementer chose
  to write fresh. Format: `Legacy-bypass: <legacy-path> — <reason>`.
  If the run is not making commits, record the same line in
  `progress.md` under that step.
- **Each finished step's outcome is summarized in `progress.md`** at
  repo root with: which files were created/edited, which legacy files
  were consulted, which canaries existed/passed, what Tier 1 looked like.
  `progress.md` is the autonomous loop's working memory across
  iterations.
- Until Step 11 creates the full canary suite, each step runs the
  available Tier 1 subset: T1.A–C plus unit tests created so far. From
  Step 11 onward, every step runs full T1.A–E.

### Tier overview

**Tier 1** — fast, no LLM, runs on every iteration. Code must be
Tier-1-green before any Tier-2 run. Suitable for autonomous loop inner
cycle.

**Tier 2** — full scenario acceptance with **real LLM**. Runs
explicitly via `pytest tests/scenarios/ --live-llm`. Real OpenRouter +
gpt-5.5 API calls per run; no budget cap, but inspect rapidly-growing
token counts (cost is informational, not blocking).

**Done = all of Tier 1 green AND all of Tier 2 green.** A test that
adds `@pytest.skip` / `xfail` / `@pytest.mark.<anything>` to bypass a
criterion without an ADR amendment is itself a regression.

### Tier 1 — Code health and LLM-free invariants (no LLM)

| ID | Check | Pass condition |
|---|---|---|
| T1.A | `lint-imports` | exit 0; no cross-sibling or upward imports |
| T1.B | `ruff check . --select ALL` | exit 0; ignores list per § Lint configuration only |
| T1.C | `mypy --strict .` | exit 0; zero warnings |
| T1.D | `pytest tests/unit/ tests/canary/ -x` | exit 0 |
| T1.E | All unit tests under `tests/unit/` complete in < 30 seconds total | wall-clock measured by pytest |

Tier 1 covers (no LLM involvement, therefore not "mocking" anything):

- `tests/unit/test_event_log.py` — SQLite append, replay deterministic,
  unregistered type rejected, UPDATE/DELETE blocked, schema_version
  enforcement.
- `tests/unit/test_projections.py` — Task Ledger fold, Recent Trace
  ring buffer, Claim/Evidence projection, rebuild idempotent.
- `tests/unit/test_lifecycle.py` — ActionLifecycle 8-state machine,
  valid transitions only, no skipped states.
- `tests/unit/test_gates.py` — Pre-action / Result Interpreter / Pre-emit
  Gate logic given pre-constructed inputs (no LLM call inside any
  gate).
- `tests/unit/test_resolver.py` — `resolve_task_ref(natural_ref,
  snapshot)` given a fixed Task Ledger snapshot.
- `tests/unit/test_response_channels.py` — `<voice>` / `<document>`
  parser.
- `tests/unit/test_constitution.py` — C1–C6 + autonomy axes are frozen
  (attempt to mutate raises).
- `tests/canary/` — see H.

### Tier 2 — Scenario invariants (real LLM)

Run via `pytest tests/scenarios/test_flagship.py
tests/scenarios/test_flagship_verify_fails.py -v --live-llm`.

#### A. Event Log structural invariants

SQL queries against the test `events.db` after the happy-path run:

- **A1.** Event count is in range **[22, 28]**. The canonical trace is
  24 events; ± 4 tolerance for LLM-induced reasoning variance (extra
  `claim.created` for limitation framing, extra `entity.resolved` if
  multi-step). Outside this range = regression.
- **A2.** Every distinct `events.type` exists in EventTypeRegistry.
  `SELECT type FROM events WHERE type NOT IN (SELECT event_type FROM
  registry)` returns 0 rows.
- **A3.** Every `events.schema_version` matches a registry version for
  that type. Mismatch → fail.
- **A4.** Every non-NULL `source_event_id` resolves to a row in
  `events`.
- **A5.** `ts_epoch_ms` monotonically non-decreasing across all rows.
- **A6.** Append-only enforcement: direct `UPDATE events SET
  payload_json=...` and `DELETE FROM events WHERE id=...` both raise.
  Tested by attempting both inside the test and asserting an exception.
- **A7.** Every canonical event in the trace (evt 00 through evt 24)
  has at least one matching row in `events` by type. List:
  `task.created`, `turn.started`, `utterance.received`,
  `entity.resolved`, `action.proposed`,
  `gate.evaluated`, `action.authorized`, `action.dispatched`,
  `action.running`, `run.started`, `worker.reported`,
  `action.result_observed`, `claim.created`, `evidence.attached`,
  `task.verified`, `turn.ended`.
- **A8.** **Claim and Evidence are independent events.** No row in
  `events` has a `payload_json` containing both `claim.*` and
  `evidence.*` fields. (Catches event lumping regression.)

#### B. ActionLifecycle 8-state completeness

- **B1.** Every distinct `action_id` reaches one of `result_observed` /
  `failed` / `timeout_assumed` / `cancelled` before the final
  `turn.ended` event.
- **B2.** State transitions follow canonical order `proposed →
  authorized → dispatched → running → terminal`. Asserted by walking
  events per `action_id` and checking pair-wise ordering. No skipped
  states.
- **B3.** `spawn_worker` (A1) and `verify_diff` (A2) each have a
  complete lifecycle. Exactly two terminal events of class
  `result_observed` in the happy path.
- **B4.** **`worker.reported` is delivered by a different thread than
  the main flow.** Asserted by recording `threading.current_thread()`
  inside the timer callback and inside the action.proposed emission;
  threads must differ. (Catches synchronous-sleep regression.)

#### C. Three-Gate enforcement

- **C1.** For every `action.authorized` event, a `gate.evaluated` event
  with `gate=pre_action` exists referencing the same `action_id`,
  emitted before the authorization.
- **C2.** For every `action.result_observed`, the Result Interpreter
  emitted ≥ 1 `claim.created` and ≥ 1 `evidence.attached` referencing
  the same `action_id` (via `source_event_id` chain).
- **C3.** The final CLI output is preceded by a `gate.evaluated` event
  with `gate=pre_emit` referencing the response text hash. Surface
  adapter is instrumented to refuse writes lacking the gate token. If a
  draft is refused and re-prompted, earlier pre-emit gate events may
  exist; the final output must match the latest valid gate token.
- **C4.** On the happy path, the final `gate.evaluated(pre_emit)`
  event has `outcome=allow_completion_language` AND the payload
  `active_claim_levels` list contains at least one of `verified` or
  `accepted`. (Negative-case symmetric check in F.)
- **C5.** Pre-action Gate evaluations record the MUST-check outcomes
  (caller-allowed, entity-trusted, risk-within-ceiling,
  lease-validated) in the `reasons` field — empty `reasons` lists for
  passing gates are a regression. Asserted by sampling at least one
  `gate.evaluated(pre_action)` and inspecting payload structure.

#### D. Task status derivation (semantic invariants)

These map to spec §13.2 numbered invariants — both names listed for
clarity.

- **D1.** *Agent report is not verification* (I8). The only event
  Result Interpreter emits in response to `worker.reported` is a
  Report Claim with `evidence.level = reported`. No `task.verified` is
  emitted on the basis of `worker.reported` alone — confirmed by
  checking that any `task.verified` event has `source_event_id` chain
  rooted in a Postcondition Claim, not a Report Claim.
- **D2.** *Worker cannot self-verify* (I11). No event with
  `actor = worker_agent` is in the source chain of any
  `evidence.attached(level=verified)`.
- **D3.** *Claim must not exceed evidence* (I10). Task Ledger derives
  `task_X.derived_status = "verified_complete"` only after a
  Postcondition Claim with `evidence.level = verified` is folded in.
- **D4.** Status is derived, not stored. Two structural checks (no
  patching needed):
  - `PRAGMA table_info(task_ledger)` (or equivalent projection table
    introspection) shows no `status` column. Status is a computed
    property at read time.
  - Replaying the entire event log into a fresh projection produces
    the same status — see D5.
- **D5.** `rebuild_projections(events.db)` produces a Task Ledger
  identical to the in-memory projection (deep equality including
  derived `status`).

#### E. Claim / Evidence integrity

- **E1.** `worker.reported` produces exactly one `claim.created`
  (type=Report) plus exactly one `evidence.attached` (level=reported)
  referencing it. Never higher level.
- **E2.** `verify_diff` (happy path: predicate matches) produces a
  `claim.created` (type=Postcondition) plus an `evidence.attached`
  (level=verified). The artifact path and content hash are recorded in
  the `evidence` payload (via the worker's real artifact file).
- **E3.** No emitted `evidence.attached` has a level exceeding what the
  source tool's `result_semantics` permits — asserted by replaying
  events through Result Interpreter and comparing levels.
- **E4.** Claim/Evidence projection is rebuildable: dropping the
  projection cache and re-folding from events produces identical state.

#### F. Negative case — verify predicate fails

`tests/scenarios/test_flagship_verify_fails.py`. Fixture controls
`spawn_worker` to write `{"status": "fail"}` to the artifact.

- **F1.** `verify_diff` emits an error result (not `verification`
  semantics). No `task.verified` event is emitted.
- **F2.** `task_X.derived_status` after the run is `reported_complete`
  (NOT `verified_complete`).
- **F3.** A Limitation Claim is created
  (`claim.created(type=Limitation, statement~="not verified")`) with
  `evidence.attached(level=reported)` referencing the failed verify.
- **F4.** CLI output must contain limitation language — at least one of
  the regex set `{r"reported,?\s*not\s+verified", r"未验证",
  r"没验证", r"测试.{0,4}没过", r"还没验"}`.
- **F5.** CLI output must NOT contain bare completion claims — must
  NOT match any of `{r"^完成", r"已完成(?!\s*报告)", r"\bverified\b",
  r"\bdone\b"}` outside an explicit "agent reported" frame.
- **F6.** Re-derived projection (D5 equivalent) matches in-memory
  state.

#### G. LLM is real, not mocked (any form)

- **G1.** After the run, `LLMClient.last_input_tokens > 0`,
  `LLMClient.model == "gpt-5.5"`, and `LLMClient.base_url` (or
  equivalent public/debug accessor) contains `openrouter`. The
  transport provider may be recorded as `"openai"` because OpenRouter
  is used through the OpenAI-compatible client. Provider must NOT be
  `"mock"` / `"stub"` / any test sentinel.
- **G2.** Static AST check (also in T1.D via `tests/canary/`):
  `tests/scenarios/*.py` do not import from `unittest.mock`,
  `responses`, `vcr`, or any module matching `*_mock` /
  `*_fixture_record`. `pytest.MonkeyPatch` is allowed only in
  `tests/conftest.py` for environment/path setup; it must not replace
  `LLMClient`, `decide`, tool outputs, or scenario behavior.
- **G3.** OpenRouter API key env var (`OPENROUTER_PROXY_KEY`)
  resolves to a non-stub value (length > 20, not `"DUMMY"` /
  `"test"` / `""`). Missing → test fails with explicit message before
  any LLM call.
- **G4.** No bytes are read from any `*.json` / `*.yaml` / `*.txt`
  file under any `recordings/` or `cassettes/` directory during a
  scenario test. Asserted by an `open()` audit hook.
- **G5.** Token use is recorded as a test artifact at
  `tests/_artifacts/llm_use_<ts>.json` (informational; no cost ceiling,
  no blocking threshold).

#### H. Anti-bypass canaries (Tier 1, AST + runtime)

These catch architectural short-circuits an iterating agent might
attempt.

- **H1.** `tests/canary/test_no_projection_writes.py` — AST scan of
  `jarvis/` for direct INSERT/UPDATE/DELETE on projection tables. Only
  `jarvis/state/event_log.py` may execute INSERT (and only into
  `events`). Projection module may only SELECT and `rebuild`.
- **H2.** `tests/canary/test_emit_event_registered.py` — AST scan for
  all `emit_event("<type>", ...)` literals; type string must exist in
  EventTypeRegistry. Unregistered → fail.
- **H3.** `tests/canary/test_pre_emit_required.py` — runtime check on
  `surface.cli.write_output(...)`; raises if Pre-emit Gate token is
  stale or absent.
- **H4.** `tests/canary/test_no_llm_substitution.py` — runtime check:
  after fixture setup, assert `decision.llm.LLMClient` is the original
  class, not a `Mock` / `MagicMock` / subclass override.
- **H5.** `tests/canary/test_layer_imports.py` — redundant with
  `lint-imports` but uses pytest to surface a clear failure inside the
  test suite.
- **H6.** `tests/canary/test_lint_ignores_justified.py` — scans
  `pyproject.toml` for ruff ignore entries; every entry must have a
  one-line code comment on the same line or immediately above it.
  Unjustified additions = regression.
- **H7.** `tests/canary/test_no_recorded_llm.py` — AST scan: no
  imports from `vcrpy`, `responses`, `betamax`, `pytest-recording`;
  no file path containing `cassette` or `recording` is opened during
  scenario tests.
- **H8.** `tests/canary/test_no_hardcoded_runtime_root.py` — AST scan
  for literal `"~/.jarvis"` strings outside `jarvis/deployment/`.
- **H9.** `tests/canary/test_decide_not_substituted.py` — runtime
  check: after fixture setup, assert
  `jarvis.decision.decide` is the original function (not patched /
  wrapped / replaced).
- **H10.** `tests/canary/test_resolver_purity.py` — AST scan: the
  resolver module / function does not import or call
  `jarvis.decision.llm` (LLM-free). And: every emitted
  `entity.resolved` event with `outcome in {"resolved", "ambiguous"}`
  has a non-empty `candidates` field — empty candidates with non-None
  `resolved_to` is a contract violation.
- **H11.** `tests/canary/test_status_not_stored.py` — AST scan of
  `jarvis/state/projections.py`: no assignment statement targeting a
  `status` field on Task Ledger rows (e.g. `task["status"] = ...`,
  `task.status = ...`, SQL `UPDATE ... SET status = ...`) exists
  outside the body of `derive_status()`. Status is computed, not
  written.
- **H12.** `tests/canary/test_gate_contracts.py` — AST scan: each gate
  module / function (`pre_action`, `result_interpreter`, `pre_emit`)
  contains the MUST-check primitives from § Gate contracts. Checked by
  presence of identifiers (`caller_principal`, `risk_level`,
  `result_semantics`, `evidence` / `claim`) inside the function body.
  This is a soft canary — easily satisfied syntactically — but flags
  obvious no-op gate implementations.
- **H13.** `tests/canary/test_layer_ownership_boundaries.py` — AST
  scan: `jarvis/runtime` is the only composition root that imports
  multiple layer siblings; `jarvis/deployment` does not import
  `jarvis.state`; `jarvis/surface` does not import `jarvis.decision` or
  `jarvis.execution`; `jarvis/execution` does not import
  `jarvis.decision` or `jarvis.surface`. This catches ownership
  violations that a pure DAG import check may still allow.

#### I. Replay determinism (LLM-tolerance)

- **I1.** Two consecutive runs from identical seeded fixture produce
  identical event **types** in identical order. Count tolerance ± 2
  (LLM may add one or two extra `claim.created` for limitation
  framing).
- **I2.** Final `task_X.derived_status` identical across re-runs of
  the happy path.
- **I3.** Negative-case final `derived_status` (`reported_complete`)
  identical across re-runs.

## Trace through the 6 layers

```
L5 in:   utterance.received(...)
L2:      append
L3 #1:   trigger = utterance.received
         Packet = Recent Trace + Task Ledger snapshot (with open tasks
                                                       age > 1d)
         Policy Resolver → Collaborate default
         Tier 2 LLM proposes spawn_worker(task referenced as
                                          "yesterday's task")
         Resolver: natural_ref → Task Ledger candidate ranking →
                    single candidate task_X → entity.resolved
         Pre-action Gate: jarvis_llm has spawn_worker (L2);
                          entity (task_X) is trusted; risk = L2 →
                          no confirmation under Collaborate
         emit ActionRequest, gate.evaluated
L4:      ToolRegistry.execute(spawn_worker[stub])
         lifecycle: proposed → authorized → dispatched → running
         stub writes artifact file, schedules worker.reported via
         threading.Timer (async)

L3 #2:   trigger = worker.reported (priority=high, from timer thread)
         Packet now reflects agent_reported_not_verified
         Attention Policy: Allen said "审核了再告诉我" →
           channel = silent_log + spawn verify run
         Pre-action Gate: verify_diff (L0 read) → OK
         emit verify ActionRequest, gate.evaluated
L4:      ToolRegistry.execute(verify_diff[stub])
         reads artifact file, evaluates predicate
         returns verification semantics on match, error otherwise

L3 #3:   trigger = action.result_observed
         Result Interpreter (vocabulary from core/tool_result.py):
           verification semantics → Postcondition Claim +
           evidence.attached(verified)
         Task Ledger derives: task_X → verified_complete
         Attention Policy: real verified claim → voice_notify channel
         Pre-emit Gate: claim_level = verified → completion language OK
         emit ResponsePlan, gate.evaluated, turn.ended
L5 out:  CLI parses LLM response via response_channels (verbatim from
         legacy), prints <document> block to stdout
L2:      events appended
L6:      mac_events.db on disk in JARVIS_RUNTIME_ROOT
L1:      invariants enforced throughout
```

## Consequences

**Positive.**

- Day-1 validates async ActionLifecycle (real threading), three Gates,
  Pre-emit downgrade (negative case), Task continuity, Resolver path,
  and the three I-invariants — the architecturally distinctive parts.
- Stubs are surgical (worker writes real artifact, verify reads it) and
  replaceable without touching L1–L3 or wiring. Real Codex / real
  pytest is a Stage 2 swap of `spawn_worker` and `verify_diff`
  implementations.
- Real LLM in test loop catches prompt regressions early.
- 6-layer skeleton compiles from Day-1 — `lint-imports` enforces
  boundaries immediately, no future restructuring needed for RPi
  expansion.
- Strict tiered acceptance gives autonomous loop a sharp pass/fail
  signal: Tier 1 fast iteration, Tier 2 occasional integration. Canary
  tests prevent the loop from satisfying assertions via short-circuit.
- Reference-source policy keeps implementer agency intact while
  pointing at battle-tested code when stuck.

**Negative.**

- Day-1 Tier 2 test cost: real LLM calls. No budget cap per Allen, but
  loop runs that fire scenario tests repeatedly will accumulate token
  use; informational metric G5 tracks this.
- Stub L4 means we don't yet validate real Codex / pytest integration.
  Deferred to Stage 2.
- 14 source files + 4 test groups on Day-1 is more than "trivial
  what-time" would need. Accepted per project-exploration / scaffolding
  exception in `CLAUDE.md`.

**Risks.**

- LLM nondeterminism may make event-count assertion (A1) flaky.
  Mitigation: ± 4 tolerance; structural assertions (types, transitions,
  derived status) over wording where possible. F4 / F5 use regex sets
  for limitation language.
- Real-LLM tests are network-dependent. Mitigation: G3 fails fast on
  missing API key; retries are the legacy client's responsibility.

## Open questions

All Day-1 blockers resolved. Remaining items deferred to Stage 2 ADR:

1. Multi-task disambiguation (ConfirmationRequest path) — Stage 2.
2. Real Codex / pytest integration replacing stubs — Stage 2.
3. Sleep/wake protocol (spec §3.7.8) — Stage 2.
4. Streaming CLI output via `chat_stream` (code preserved in adapted
   `decision/llm.py`) — Stage 2.
5. Tier 0 regex patterns (Day-1 ships empty scaffold per Allen) —
   Stage 2 first shortcut.
6. Health tracker wiring (`tracker=None` Day-1) — Stage 2.
7. LLM cost tracking in USD (informational `tests/_artifacts/llm_use_*`
   on Day-1; per-model USD rates via legacy
   `scripts/refresh_pricing.py` + `memory/cold/pricing.py` available
   when Stage 2 wants them).

## Build order

After this ADR is approved:

| Step | What | References (optional, agent discretion) | Verification |
|---|---|---|---|
| **0** | **(Done)** Full Legacy + Hermes reference scan at `docs/adr/0001-legacy-scan.md` | — | scan doc exists; ADR cross-references it |
| 1 | `pyproject.toml` ratchet to ruff ALL + mypy --strict per § Lint configuration | — | Tier 1 T1.A–T1.C clean on empty scaffold |
| 2 | `jarvis/constitution/__init__.py` + `jarvis/shared/__init__.py` | spec §3.2.1, §3.5.2 | unit test: principles frozen, types compile |
| 3 | `jarvis/deployment/__init__.py` (paths + bootstrap with `JARVIS_RUNTIME_ROOT`) | — | unit test: bootstrap creates runtime root idempotently |
| 4 | `jarvis/state/event_log.py` | spec §5.1, §5.4 | unit test: append, replay, unregistered type rejected, UPDATE/DELETE blocked |
| 5 | `jarvis/state/projections.py` | spec §6, §7, §8 | unit test: seed events → expected projection state; rebuild matches |
| 6 | `jarvis/execution/tools.py` (registry + lifecycle + 2 stub tools) | legacy `tools_v2/registry.py` (ADAPT) + `helpers.py` (verbatim); Hermes `tools/registry.py` for design questions | unit test: full 8-state lifecycle; stub writes / reads real artifact; lifecycle never skips states |
| 7 | `prompts/jarvis_v1.md` + `config/jarvis.yaml` (LLM section) | legacy `prompts/phase1/v1.md` + `config.yaml:277-301` verbatim | byte-diff against legacy sources is empty for in-scope sections |
| 8 | `jarvis/decision/llm.py` | legacy `core/llm.py` (ADAPT, trim per § Reference sources) | unit test (LLM-free): client construction from `config/jarvis.yaml`, preset switching, metadata accessors. Real-LLM smoke at Step 12. |
| 9 | `jarvis/decision/__init__.py` (packet + policy + intent + resolver + 3 gates + Result Interpreter) | legacy `core/tool_result.py` for vocabulary; spec §3.4 for gates | unit tests per gate; resolver unit test with seeded ledger |
| 10 | `jarvis/surface/cli.py` + `jarvis/runtime/__init__.py` + `jarvis/cli/__init__.py` | legacy `core/response_channels.py` (verbatim) for surface; legacy `jarvis.py` `JarvisApp.__init__` as pattern reference for runtime | smoke: `python -m jarvis "hi"` round-trips through real LLM |
| 11 | `tests/canary/` (H1–H13) | — | full Tier 1 canaries green |
| 12 | `tests/scenarios/test_flagship.py` (happy path, real LLM) | — | full scenario green; A / B / C / D / E / G / I all pass |
| 13 | `tests/scenarios/test_flagship_verify_fails.py` (negative case) | — | F1–F6 pass |

Stop after Step 13. Stage 2 ADR will plan real Codex / verify
integration, sleep/wake protocol, Memory system, multi-task
disambiguation, streaming surfaces, and additional output channels.
