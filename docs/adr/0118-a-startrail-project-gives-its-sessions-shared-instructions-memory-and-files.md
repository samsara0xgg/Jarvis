# ADR 0118 — A Startrail project gives its sessions shared instructions, memory and files

**Status:** Accepted
**Date:** 2026-10-01
**Supersedes:** none

## Context

- Allen, 2026-10-01: Startrail should get a project mode like Claude Code's
  Projects (public beta since 2026-09-17), which Allen has used daily for
  the Jarvis project; the backend first, the window later.
- In Claude's Projects every cloud thread starts from the project's
  instructions (at most 16,000 characters) and reads the project memory
  index `MEMORY.md`, which Claude writes itself; a thread run on the owner's
  Mac gets the instructions but not the memory, and a session the owner
  started on their own cannot join a project
  (code.claude.com/docs/en/claude-projects). Allen's 25 local project
  threads of 2026-09-27 to 09-30 had no memory tool at all.
- A session's folder does not name its project: ADR 0037 measured a session
  ("timesink code review") that ran entirely in the jarvis folder, and a
  Startrail session in a worktree runs in a folder of its own.
- In the host, `project` on a session and the `/projects` route already mean
  the repository folder (A8).
- The Agent SDK appends text to Claude Code's own system prompt
  (`systemPrompt.append`) and gives a session extra folders
  (`additionalDirectories`); Codex's app-server takes
  `developerInstructions` when a thread starts and writable roots in its
  sandbox policy. Both agents already read and edit files in those folders.

## Decision

A project is a folder under the host's agents directory holding its settings
(name, goal, instructions of at most 16,000 characters, default folder,
agent, model, effort and mode), a memory folder indexed by `MEMORY.md`, and a
shared files folder. A session belongs to at most one project, by being
started in it or moved into it, and each time its agent starts it gets the
project's instructions and memory index (Claude: appended to Claude Code's
system prompt; Codex: developer instructions) and may read and write the
memory and files folders.

Limits: memory reaches a running session only at its next start; the index
goes in cut to 200 lines and 25,000 characters; how a project's sessions are
grouped (waiting on the owner, working, landing, in review, idle, done after
7 days without a change) is computed when read, never stored.

## Alternatives rejected

- **A project is the session's folder** — the folder does not name the
  project (ADR 0037's measurement), and one project's sessions run in as
  many worktrees as it has sessions.
- **A memory tool the host serves to each session** — both agents already
  write files in their extra folders with the tools they have; a tool would
  need its own server in every Claude Code child and separate wiring for
  Codex, for the same writes.
- **Reuse ADR 0037's project list** — it lives in the daemon's config for
  time tracking, and the host reaches the daemon for three things only; a
  window whose projects vanish while the daemon is down cannot be used.

## Consequences

- Every turn of a project session carries the instructions and up to
  25,000 characters of memory index in its prompt (cached after the first).
- Two sessions writing one memory file at once: the last write wins.
- A session moved into another project keeps its transcript; its earlier
  turns ran under the old project's prompt.
- A session whose pull request is open stays "in review" until the owner
  archives it, since the host does not learn when a pull request merges.
