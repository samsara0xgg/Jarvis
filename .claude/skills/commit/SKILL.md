---
name: commit
description: Create a Jarvis git commit in the repository's required format with real verification evidence. Use when the user asks to commit, or when an implementation slice is ready to be checkpointed.
---

# Jarvis commit

## Before staging

1. Run `git status` and read the relevant diff. Confirm it is one logical
   change.
2. Outside an autonomous goal run, show the user a diff summary and wait for
   confirmation before committing. Allen reviews changes before commits.
3. Stage explicit paths only.

Forbidden: `git add .`, `git add -A`, `git commit -am`, `--no-verify` or any
other hook bypass, `Co-Authored-By`, unrelated files in the same commit,
pushing.

## Title

`type(scope): English description`, at most 72 characters.

Types: `feat fix refactor test docs chore perf data`.
Scopes are layer or subsystem names; the list and examples are in
`docs/git-guide.md` §2. Multi-layer atomic changes use comma scopes such as
`feat(surface,runtime,cli): ...`, sparingly.

## Body for implementation commits

    <one-line opener; "Step N of ADR XXXX." when implementing a build step>

    - <path> — <what this file contributes>
    - <path> — <what this file contributes>

    Tier 1: lint-imports KEPT (1/1) · ruff clean (N files) · mypy strict clean
    (N files) · M/M acceptance checks pass · wall <t>s (< 30s budget).

    Legacy-bypass: <legacy/path> — <why it was not reused>.
    Legacy consulted: <legacy/path> (<what slice was borrowed>).

Rules:

- English throughout. One bullet per file or tightly related file group, em
  dash between path and contribution, wrap near 72 columns.
- `Tier 1:` is one line with mid-dot separators and includes wall-clock.
  Docs-only commits write `Tier 1: docs-only — gates not run.`
- `Legacy-bypass:` lists files under `jarvis-legacy/` that could plausibly
  have been reused but were not, one per file. `Legacy consulted:` lists files
  read and partially borrowed. A file appears in at most one trailer. Omit
  both trailers when no legacy file was considered.
- Never invent counts, timings, or legacy evidence. Every number in the body
  comes from a command run in the current work. If a gate was not run, say so.
- Unusual cases: `docs/git-guide.md` §2 is the source of truth.
