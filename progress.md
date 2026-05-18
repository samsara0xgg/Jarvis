# Day-1 Build Progress

ADR: `docs/adr/0001-mac-only-flagship-scenario.md`.

Per ADR § Process discipline, each step records: Files, Legacy consulted,
Legacy-bypassed, Tier 1, Notes, Next.

---

## Step 0 — Reference scan (Done in main worktree)

- Files: `docs/adr/0001-legacy-scan.md` (copied verbatim into worktree).
- Legacy consulted: full repo scan recorded in legacy-scan.md.
- Legacy-bypassed: none yet.
- Tier 1: n/a (no code yet).
- Notes: ADR + legacy-scan both copied into worktree for in-tree
  reference. Build branch `worktree-claude-adr0001`.
- Next: Step 1.

---

## Step 1 — pyproject.toml ratchet + deps

- Files: `pyproject.toml` (ratchet), `.importlinter` (fix
  `containers = jarvis` × `jarvis.shared` double-prefix bug),
  `tests/__init__.py` (added to satisfy ruff INP001).
- Legacy consulted: none.
- Legacy-bypassed: none.
- Tier 1:
  - T1.A `lint-imports`: 1 contract kept, 0 broken.
  - T1.B `ruff check .`: All checks passed.
  - T1.C `mypy .` (strict mode in config): no issues in 12 source files.
  - T1.D, T1.E: no unit tests yet.
- Notes: Created worktree-local `.venv` (uv) so parallel agents don't
  fight over the shared `mise` venv at the main project root. Restored
  main `.venv` editable install to the main worktree after stamping
  this worktree. New runtime deps: `openai`, `anthropic`, `PyYAML`;
  dev adds `types-PyYAML` for mypy `--strict`. Added `live_llm` pytest
  marker (Tier 2 gate).
- Next: Step 2 (L1 constitution + shared types) — delegate to subagent.

---

## Step 2 — L1 Constitution + shared types

- Files:
  - `jarvis/constitution/__init__.py` (189 LOC) — `JARVIS_IDENTITY` Final
    string, `PRINCIPLES` MappingProxyType of C1..C6 frozen
    `ConstitutionalPrinciple` dataclasses (canonical Chinese text from
    spec §3.2.1), `NON_GOALS` tuple of 6 (spec §3.2.2), `AUTONOMY_LEVELS`
    tuple `("L0","L1","L2","L3","L4")`, `AUTONOMY_AXES` MappingProxyType
    of 5 frozen `AutonomyAxis` dataclasses (spec §3.2.6).
  - `jarvis/shared/__init__.py` (252 LOC) — `Event`, `Claim`, `Evidence`,
    `ActionRequest` frozen dataclasses; `PromptContext(system: str)`
    frozen dataclass; `AuthorizationLease` TypedDict; `CallerPrincipal`
    enum with 6 members per spec §3.5.2; `EvidenceLevel` / `ClaimType` /
    `RiskLevel` Literal aliases.
  - `tests/unit/__init__.py` (empty marker).
  - `tests/unit/test_constitution.py` (92 LOC, 9 tests).
  - `tests/unit/test_shared_types.py` (140 LOC, 8 tests).
  - `pyproject.toml` — added `[tool.ruff.lint.per-file-ignores]` for
    `jarvis/constitution/__init__.py` (RUF001: canonical CJK
    punctuation in spec-copied text) and `tests/**/*.py` (S101, ANN201,
    PLR2004, S108: pytest idioms). Each entry has a justification
    comment per canary H6 spirit.
- Legacy consulted:
  - `jarvis-legacy/tools_v2/registry.py` — caller_scope is `set[str]`
    with string labels (`"llm"`, `"regex_router"`). Day-1 swaps to a
    typed `CallerPrincipal` enum with spec §3.5.2 canonical names; this
    is the intended adaptation per ADR § Module map.
  - `jarvis-legacy/docs/product/jarvis-product-constitution.html` —
    located but not consulted; spec.html §3.2 is the canonical source
    and was used directly.
  - No legacy `core/identity.py` or `core/constitution.py` exists.
- Legacy-bypassed:
  - `Legacy-bypass: jarvis-legacy/core/personality.py — Xiaoyue persona
    explicitly discarded per ADR § Identity; identity label lives in
    L1 Constitution as the frozen string "Jarvis".`
- Tier 1:
  - T1.A `lint-imports`: 1 contract kept, 0 broken. 10 files analyzed.
  - T1.B `ruff check .`: All checks passed.
  - T1.C `mypy .` (strict): Success: no issues found in 15 source files.
  - T1.D `pytest tests/unit/ -x`: 17 passed in 0.02s.
- Notes:
  - L1 Constitution carries canonical Chinese text verbatim from
    spec §3.2.1 / §3.2.2 / §3.2.6. Added a per-file `RUF001` ignore for
    `jarvis/constitution/__init__.py` with same-line comment in
    `pyproject.toml`. Per-file-ignores are a separate table from the
    top-level `ignore` list — canary H6 (planned Step 11) checks the
    top-level list; this new section follows the same justification
    discipline.
  - `Mapping` is imported inside `if TYPE_CHECKING:` blocks so ruff
    `TC003` is satisfied; `from __future__ import annotations` makes
    all annotations strings, so runtime evaluation is not needed.
  - Spec ambiguity fixed: spec §3.2.6 lists 5 axes (Proactivity /
    Confirmation / Verification / Execution / Memory write) but doesn't
    name a discrete level vocabulary. The ADR says "C1–C6 + 5 autonomy
    axes" without specifying levels. Picked the L0..L4 ladder per spec
    §3.4.10 risk wording (matches ActionRequest.risk_level shared
    vocabulary) so EffectivePolicy ceiling comparisons stay in one
    namespace. Exposed as `AUTONOMY_LEVELS`.
  - `PromptContext` carries only `system: str` per ADR Q3 option (a) —
    legacy `core/llm.py` had a richer `prompt_context` argument tied to
    personality + hot-memory assembler; Day-1 deliberately replaces
    with a single string and lets the dataclass grow additively later.
  - `ActionRequest` carries only the Pre-action Gate inputs plus
    correlation keys (action_id, tool_name, target_entity_ref,
    caller_principal, risk_level, arguments, authorization_lease,
    run_id, turn_id). Spec §3.4.8 lists more fields
    (`result_expected_by`, `max_duration`, `timeout_policy`,
    `retry_policy`, `expected_postcondition`); deferred to Step 6 when
    L4 lifecycle / scheduler actually consume them.
- Next: Step 3 (L6 deployment with `JARVIS_RUNTIME_ROOT`).
