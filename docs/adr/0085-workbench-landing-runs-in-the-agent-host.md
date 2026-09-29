# ADR 0085 — Workbench landing runs in the agent host

**Status:** Accepted
**Date:** 2026-09-29
**Supersedes:** none

## Context

On September 29, 2026 Allen asked for the workbench prototype
(`docs/design/agents-workbench.html`) to be built, landing included. Its
landing is seven steps from a session's changes to main, each one shown as it
runs: gates light up one by one, the commit title can be edited before the
commit, an interrupt stops only between steps (a gate at once), a red gate
stops with "let Claude fix it" or "stay on the branch", and the push waits for
Allen. `docs/git-guide.md` §3 fixes the steps themselves: the commit skill,
a fast-forward into main from the main checkout, a restart of what the change
touches, `git push origin main` approved by Allen, and the worktree and branch
removed only when git agrees.

An agent's tool calls come in whatever order its model chooses, and
interrupting a turn can stop git halfway through a rebase. The restart step
restarts the companion, which is the process that owns the Agents window.

## Decision

Run landing as a fixed line in the agent host: the host takes every git,
gate and `launchctl` step itself, picks the gates from the changed paths,
drafts the commit message with one tool-less Claude Haiku turn over the diff
and the commit skill when Allen has not written a title, and waits for
Allen's answer in the window before it pushes.

Gates and restarts exist only for the Jarvis repository; other repositories
land without them. Only a session's own worktree branch (against main) or a
session working on main (against `origin/main`) can land.

## Alternatives rejected

- **Ask the session's agent to land** — the window could only guess the
  steps from its tool calls, the order is the model's, a stop between steps
  cannot be promised, and an interrupt during `git rebase` leaves the
  worktree mid-rebase.
- **Run the line in the companion's main process** — its own restart step
  kills the companion, so every desktop landing would die before its push.
- **Draft the message inside the session's conversation** — adds a turn to
  Allen's conversation on every landing, and fails exactly when that
  session's plan is used up, which the same prototype shows as a normal
  state.

## Consequences

The host now runs git and `launchctl` on main for Allen, so a fault there
acts on main. The commit body's bullets come from a small model reading only
the diff. A line under way is not persisted: a host restart forgets it and
leaves git as far as it got. The gate commands copy `docs/git-guide.md` §1
and must follow it when it changes.
