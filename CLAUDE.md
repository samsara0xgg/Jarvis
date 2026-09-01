# Jarvis — Allen 的私人 state-centric runtime

## Rules

- Discuss first, write code later.
- Commit OK, **never push** unless explicitly asked.
- **No Co-Authored-By** in commit messages.
- Don't violate layer boundaries — `lint-imports` enforces them.

## Testing

- No new Python unit tests; `tests/unit` is retired. Verify Python work with
  the task's acceptance command, data-driven checks (input → expected tables),
  canaries, regression pins, integration/scenario harnesses, and required live
  tests.
- Swift protocol/reducer tests for the repository-owned Inherent app may live
  in `InherentCardTests`; this is the explicit exception needed for strict
  concurrency, reconnect, and UI-state verification.
- New capability → live test before calling it done (real LLM, real run,
  result matches expectation). Small fixes / refactors skip this —
  judge by risk.

## Models

- Default: `opus` — orchestrates, writes task cards, reviews.
- `fable` — design, ADRs, major changes.
- `sonnet` — turning settled designs / ADRs into code.
- `haiku` — lookups, mechanical sweeps.
- Unlisted cases: pick by judgment; escalate when stuck.

## Git

- Conventional Commits: `type(scope): English description`. Types: `feat fix refactor test docs chore perf data`.
- One thing per commit. Forbidden: `git add .` / `git add -A` · `git commit -am` · `push --force` to `main` · `--no-verify` / any hook bypass.
- Multi-layer atomic changes use comma scopes: `feat(surface,runtime,cli): ...`. Use sparingly.

### Commit body template

For any commit landing implementation, the body has five parts:

```
<one-line opener — "Step N of ADR XXXX." when implementing a build step>

- <path> — <what this file contributes>
- <path> — <...>

Tier 1: lint-imports KEPT (1/1) · ruff clean (N files) · mypy strict clean (N files) · M/M hermetic tests pass · wall <t>s (< 30s budget).

Legacy-bypass: <legacy/path> — <reason it was not reused>.
Legacy consulted: <legacy/path> (<what slice was borrowed>).
```

Rules:

- Title and body both English; title ≤ 72 chars.
- One bullet per file (or per closely-related file group); em-dash separator; wrap ~72 cols.
- `Tier 1:` is one line, mid-dot `·` separators, includes wall-clock. Docs-only commits write `Tier 1: docs-only — gates not run.`
- `Legacy-bypass:` for files that **could plausibly have been reused** but were not (one per file). `Legacy consulted:` for files read and partially borrowed. A file appears in at most one trailer.
- No `Co-Authored-By`.

One-line example:

```
feat(state): L2 event log with SQLite append-only + EventTypeRegistry

Step 4 of ADR 0001. Adds the append-only spine + Day-1 registry.

- jarvis/state/event_log.py — EventLog class, registry validation, monotonic ts guard, append-only UPDATE/DELETE triggers.
- event-log canary/regression acceptance — registry rejection, source-event integrity, and trigger enforcement.

Tier 1: lint-imports KEPT (1/1) · ruff clean (8 files) · mypy strict clean (8 files) · 17/17 acceptance checks pass · wall 0.05s (< 30s budget).

Legacy-bypass: jarvis-legacy/core/event_bus.py — pub/sub conflicts with append-only Event Log, spec §3.3.1.
```

See `docs/git-guide.md` § 2 for the full template, scopes, branch policy, and recovery commands.

## Architecture

6 layers (see `docs/spec.html` for the full spec):

```
L1 constitution   L2 state      L3 decision
L4 execution      L5 surface    L6 deployment
```

`runtime/` is the only place allowed to wire across layers.
