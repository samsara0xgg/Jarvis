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
