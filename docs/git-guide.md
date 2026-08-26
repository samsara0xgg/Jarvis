# Git Workflow Complete Guide

For future Allen + CC reference. `CLAUDE.md` keeps only the hard rules;
details and examples live here.

## 1. Daily loop

1. Edit code → run all Tier 1 gates → commit only after every gate is
   green:
   - `lint-imports`
   - `ruff check . --select ALL`
   - `mypy --strict .`
   - `pytest tests/canary/ tests/integration/ -x`
2. `git status` + `git diff --stat` to glance at the change set.
3. One thing per commit (no mixing `fix` with `feat`, no new
   functionality inside a `refactor`).
4. Do not push proactively (default is never push unless Allen
   explicitly asks). Do not bypass any hook that exists. Do not
   force-push to `main`.

Tier 2 scenario tests (`pytest tests/scenarios/ --live-llm`) are
**not** part of the commit gate — they fire real LLM calls and are run
explicitly. See `docs/adr/0001-mac-only-flagship-scenario.md`
§ Acceptance criteria.

## 2. Commit message format

Conventional Commits: `type(scope): English description`

### Types

| type | usage | example |
|---|---|---|
| `feat` | new feature | `feat(decision): wire Pre-emit Gate downgrade` |
| `fix` | bug fix | `fix(state): reject unregistered event type` |
| `refactor` | refactor (behavior unchanged) | `refactor(execution): split ToolRegistry helpers` |
| `test` | test-only change | `test(tests): add no-mock-import canary` |
| `docs` | docs / notes | `docs(docs): tighten Pre-action Gate contract` |
| `chore` | misc (deps / gitignore / cleanup) | `chore(config): widen ruff selection` |
| `perf` | performance | `perf(state): index events.correlation_id` |
| `data` | data files | `data(tests): seed yesterday task fixture` |

### Scopes

Common scopes (Day-1; add new ones as the codebase grows):

```
constitution state decision execution surface deployment runtime
shared cli prompts config docs tests
```

Sub-scope is allowed when it adds clarity: `docs(adr): ...` for an
ADR edit, `test(canary): ...` for a canary test. The top-level scope
is always valid on its own.

**Multi-scope**: when a single atomic change touches multiple
top-level layers (e.g. a composition root that wires L5 + runtime +
CLI together), use comma-separated scopes:
`feat(surface,runtime,cli): L5 surface + composition root + CLI`.
Use sparingly — prefer one scope per commit unless the change is
genuinely cross-cutting.

### Title rules

- Title and body both in English.
- Title ≤ 72 characters.
- Title writes **what** at a high level; body writes **why** and
  the per-file breakdown.
- No `Co-Authored-By`.

### Body structure

For any commit that lands implementation, the body follows this
five-part skeleton:

```
<opener>

<per-file bullets>

<Tier 1 summary line>

<trailers>
```

**1. Opener** (one line, optional sentence after). When the commit
implements an ADR Build step, lead with `Step N of ADR XXXX.` plus
a sentence on what the commit adds. Otherwise open with a one-line
rationale.

**2. Per-file bullets**. One bullet per file (or per closely-related
file group), em-dash separator, concise summary of what that file
contributes. Wrap each bullet to ~72 cols; continuation lines
indented two spaces.

```
- jarvis/decision/gates.py — pre_action_gate (4 MUST-checks),
  pre_emit_gate (completion-keyword detection + downgrade_required),
  attention_policy.
```

Group several small files into one bullet when they share a concern:

```
- tests/unit/test_resolver.py, test_gates.py, test_packet.py —
  46 new LLM-free unit tests covering all MUST-checks, ...
```

**3. Tier 1 summary line**. After the bullets, blank line, then a
single line summarizing the gate results. Format (mid-dot `·`
separators):

```
Tier 1: lint-imports KEPT (1/1) · ruff clean (39 files) · mypy
strict clean (39 files) · 94/94 canary+integration tests pass · wall
0.37s (< 30s budget).
```

Always include each of the four gates and a wall-clock number. If a
gate is intentionally skipped (e.g. pure docs change), say so:
`Tier 1: docs-only — gates not run.`

**4. Trailers**. Zero or more, each its own paragraph, RFC-style
key colon space value. See § Trailers below.

### Trailers

Two reference sources are available Day-1:
`/Users/alllllenshi/Projects/jarvis-legacy/` and
`/Users/alllllenshi/Projects/hermes-agent/repo/`. Annotate which
files were considered and how:

**`Legacy-bypass: <path> — <reason>`** — for each legacy file that
the new module **could plausibly have reused** but did not.

```
Legacy-bypass: jarvis-legacy/core/event_bus.py — pub/sub conflicts
with append-only Event Log, spec §3.3.1.
```

When required:

- **Required**: new module whose name / concept overlaps a legacy
  file you chose not to use (`state/event_log.py` vs legacy
  `core/event_bus.py`).
- **Required**: rewriting something legacy already implements (e.g.
  permission checking) because the legacy concept doesn't match.
- **Not required**: no plausible legacy counterpart (e.g.
  `constitution/__init__.py`).
- **Not required**: adapting a legacy file (e.g. `decision/llm.py`
  derived from `core/llm.py`) — adaptation is reuse, not bypass.

One annotation per bypassed file.

**`Legacy consulted: <path> (<rationale>)`** — for each legacy file
the implementer read and partially reused or referenced for shape /
vocabulary, without doing a full port.

```
Legacy consulted: legacy/core/tool_result.py (vocabulary source for
Result Interpreter table only; parsing helpers intentionally
skipped per ADR § Reference sources).
```

`Legacy-bypass:` and `Legacy consulted:` are complementary, not
overlapping: bypass = "did not use", consulted = "looked at and
borrowed a slice." A file appears in at most one trailer.

### Full example

```
feat(decision): L3 decide pipeline + 3 gates + resolver + result interpreter

Step 9 of ADR 0001. Adds the L3 Runtime Decision pipeline:

- jarvis/decision/__init__.py — decide() entry, DecideContext /
  Result, RuntimePathsLike / ToolRegistryLike / LifecycleLike
  Protocols, branch handlers for utterance.received /
  worker.reported / action.result_observed, tool-use loop with
  Resolver + Pre-action Gate + Result Interpreter wiring, Pre-emit
  finalize with one-retry + template-downgrade fallback,
  task.verified emission.
- jarvis/decision/resolver.py — pure resolve_task_ref (no LLM
  import; canary H10 ready).
- jarvis/decision/policy.py — frozen EffectivePolicy + Day-1
  Collaborate preset.
- jarvis/decision/gates.py — pre_action_gate (4 MUST-checks),
  pre_emit_gate (completion-keyword detection +
  downgrade_required), attention_policy.
- jarvis/decision/result_interpreter.py — semantics → (ClaimType,
  EvidenceLevel) table; emits claim.created + evidence.attached as
  two separate events per ADR Acceptance A8.
- tests/unit/test_resolver.py, test_gates.py,
  test_result_interpreter.py, test_pre_emit_gate.py,
  test_effective_policy.py, test_attention_policy.py,
  test_packet.py, test_intent.py — 46 new LLM-free unit tests
  covering all four MUST-checks, every semantics-table row, all
  Day-1 attention rules, Pre-emit happy + force-limitation +
  downgrade paths, resolver single/multi/empty/empty-ref cases.

Tier 1: lint-imports KEPT (1/1) · ruff clean (39 files) · mypy
strict clean (39 files) · 218/218 unit tests pass · wall 0.37s
(< 30s budget).

Legacy-bypass: legacy/core/regex_router.py — Day-1 Tier 0 ships
empty scaffold; no regex patterns per ADR § Stub strategy.

Legacy consulted: legacy/core/tool_result.py (vocabulary source
for Result Interpreter table only; legacy parsing helpers
intentionally skipped per ADR § Reference sources Day-1 scope).
```

## 3. Branches

- Daily development → directly on `main`.
- Big refactors / experiments / likely-to-fail work → `feat/xxx`
  branch, merged back to `main` when done. Prefer `git merge --no-ff`
  to preserve the branch in history.
- `push origin main` happens **only when Allen explicitly asks** —
  default is no push. **Never** `push --force` to `main`.

## 4. gitignore principles

Before adding a new path, ask:

- **runtime artifacts** (`${JARVIS_RUNTIME_ROOT}/...`, logs, cache,
  builds) → ignore.
- **local secrets** (`.env`, api keys, credentials) → ignore.
- **regeneratable** (`__pycache__`, `*.pyc`, `.venv`,
  `.import_linter_cache`, `.pytest_cache`, `.ruff_cache`,
  `.mypy_cache`) → ignore.
- **large binaries / data dumps** (> 10 MB) → ignore; if a real
  need arises, commit via `git-lfs`.

When adding a new category, leave a comment in `.gitignore`
explaining **why** (you will forget). The same comment-the-escape
convention applies to the `[tool.ruff.lint] ignore` list in
`pyproject.toml` (per ADR 0001 canary H6) — every entry needs a
one-line justification.

## 5. Lifesaver cheatsheet

```bash
# Undo the last commit (unpushed) but keep the changes
git reset --soft HEAD~1

# Discard local changes (UNRECOVERABLE — careful)
git restore <file>

# Edit the previous commit message (unpushed only)
git commit --amend

# Split a commit: interactively pick hunks into stage
git add -p

# Inspect changes
git status
git diff --stat
git diff <file>

# Inspect history
git log --oneline -20
git log --stat <file>         # history of one file
git blame <file>              # line-by-line authorship
```

## 6. Don't

- `git push --force` to `main`.
- `git add .` / `git add -A` — easy to accidentally commit `.env`,
  `${JARVIS_RUNTIME_ROOT}` artifacts, or large files; use specific
  file names.
- `git commit -am` mixing multiple things together.
- Commit secrets, database dumps, runtime artifacts under
  `${JARVIS_RUNTIME_ROOT}`, or test recordings.
- Bypass any hook that exists (no `--no-verify`).

## 7. progress.md

Every completed Build step (per ADR 0001 § Build order) appends an
entry to `progress.md` at the repo root. Step 1 creates the file if
absent; Step 0's entry may be backfilled retroactively.

**Each entry uses this exact template** (loop-parseable):

```markdown
## Step N — <name> (YYYY-MM-DD)

**Files**: created `path/to/x.py`, edited `path/to/y.py`
**Legacy consulted**: `jarvis-legacy/core/foo.py` (adapted)
**Legacy-bypassed**: `jarvis-legacy/core/bar.py` — reason
**Tier 1**: PASS (T1.A–D green, 12.4s)
**Notes**: <optional one-liner>
**Next**: Step N+1
```

The 6 bold labels are fixed — `Files`, `Legacy consulted`,
`Legacy-bypassed`, `Tier 1`, `Notes`, `Next`. If a label has no
content for a given step, write `none`. Do not omit labels; do not
rename them.

`progress.md` is the autonomous loop's working memory across
iterations. Do not rewrite previous entries; append.

## 8. When things go wrong

- **Already committed and discovered a secret leak**: tell Allen
  immediately. Do not rewrite history on your own; may require
  rotating the key + a force push (only case where force-push is
  permitted).
- **A hook fails**: fix the underlying issue, re-stage, and commit
  again. **Do not** `--amend` — that commit didn't happen, and amend
  would modify the previous one.
- **merge conflict**: resolve manually. Do not `checkout --` or
  `reset --hard` as a shortcut.
- **unfamiliar files / branches**: investigate before deleting; could
  be another session's in-progress work.
