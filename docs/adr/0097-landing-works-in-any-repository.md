# ADR 0097 — Landing works in any repository

**Status:** Accepted
**Date:** 2026-09-29
**Supersedes:** 0085

## Context

- ADR 0085 made landing a fixed line in the agent host, drawn from
  `docs/git-guide.md` §3: the changes, the gates, a commit, a fast-forward
  into main, a restart, `git push origin main` once Allen says yes, and the
  worktree removed. An agent's tool calls come in the order its model picks,
  an interrupt can stop git halfway through a rebase, and the restart step
  kills the companion, so the host runs the line, not the session or the
  window. It bound the line to the Jarvis repository: the target is `main`,
  gates and restarts are Jarvis's own and other repositories get none, and
  only a session's own worktree branch or `main` itself can land.
- Startrail release audit, B19 and D3 (2026-09-29): the window ships for
  other people's repositories. Their default branch is often not `main`,
  most take changes through pull requests, and GitHub refuses a direct push
  to a protected branch. As built, landing fast-forwards a session's branch
  into their default branch and pushes it. Codex's and Claude Code's desktop
  apps commit, push and open a pull request from the app. D3 defaulted to its
  recommendation: find the default branch, gates and a restart only where
  configured, and a last step that either pushes the default branch or pushes
  the branch and opens a pull request when `gh` is installed. Allen asked
  this thread to close the audit's functional gaps.
- git records a clone's default branch as `refs/remotes/origin/HEAD`; a
  remote added by hand has none until `git remote set-head`, which needs the
  network.
- git cannot open a pull request. `gh` can, and does not prompt without a
  terminal. GitHub and GitLab print an address for opening one when a new
  branch is pushed.
- A restart names one Mac's services (a LaunchAgent label, a port). A check
  command belongs to one repository. Both run in the owner's login shell with
  the owner's rights.
- A session taken in from a terminal (ADR 0096) often works in the main
  checkout, on a branch of its own.
- The commit message is drafted by one tool-less Claude Haiku turn over the
  diff when the owner has not written a title; Jarvis's commit skill asks for
  Conventional Commits in English, which other repositories do not all use.

## Decision

Land a session into its repository's default branch (origin's `HEAD`, else
`main`, else `master`) one of two ways, as a fixed line the agent host runs:
merge (commit, fast-forward into the default branch in the main checkout,
restart, push the default branch once the owner says yes, remove the
worktree) or pull request (commit, push the session's branch once the owner
says yes and open a pull request against the default branch with `gh`,
keeping the worktree and restarting nothing).

Limits: gates, a restart command and the default way come from the owner's
settings for that repository; with none set, the Jarvis repository keeps the
gates and restarts of `docs/git-guide.md` and any other repository has none.
Merge is the default in the Jarvis repository, a pull request elsewhere when
origin exists. A session on the default branch can only merge, which commits
and pushes it; a session on another branch outside its own worktree can only
open a pull request. Without `gh` the pull request way ends at the pushed
branch and the address the remote gave. A drafted message follows the
repository's commit skill if it has one, else the style of its recent
commits.

## Alternatives rejected

- **Read gates and the restart from a file committed in the repository** —
  a restart names services that exist on one Mac (`com.allen.jarvis` exists
  only on Allen's), so a shared file is wrong on every other machine; and
  pressing 一键落地 in a repository just cloned would run commands it ships
  before the owner has read them.
- **Guess gates from `package.json` scripts or a Makefile** — `npm test` in a
  Create React App project starts Jest in watch mode and never exits, so the
  line would sit until the gate's 20-minute cap; Jarvis's own suite needs
  `-m "not live_llm"` to stay off paid models, which no guess can know.
- **Show landing only in the Jarvis repository** (D3's other option) — leaves
  every other repository without a way from a session to a pull request
  inside the window, which Codex's and Claude Code's desktop apps both have.
- **Keep pushing the default branch in every repository** (ADR 0085) — GitHub
  answers a push to a protected branch with `GH006: Protected branch update
  failed`, and a repository that reviews pull requests loses the review.

## Consequences

- A repository checks nothing locally until the owner types its gates; its CI
  still runs on the pull request.
- A pull request landing leaves the worktree and its branch. git does not
  count a squash-merged branch as merged, so removing that worktree after the
  pull request merges takes the owner's second, forcing press.
- A repository whose origin was added by hand has no `origin/HEAD` and lands
  into `main` or `master`, whichever exists.
- Without `gh`, or with `gh` signed out, the owner opens the pull request from
  the address shown.
- The host runs git on the default branch for the owner, so a fault there acts
  on it. A line under way is not persisted: a host restart forgets it and
  leaves git as far as it got. The Jarvis gate commands copy
  `docs/git-guide.md` §1 and must follow it when it changes.
