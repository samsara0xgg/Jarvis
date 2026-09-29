# ADR 0096 — A session started outside the window can be taken in

**Status:** Accepted
**Date:** 2026-09-29
**Supersedes:** none

## Context

- ADR 0073 has the window drive only the sessions it started, and notes that
  only one process may write a session at a time.
- Claude Code keeps every session's transcript under `~/.claude/projects`;
  the Agent SDK lists them with a summary, the time each last changed and its
  folder (`listSessions`). Codex's app-server lists its threads, including
  ones made in a terminal or in Codex's own app (`thread/list`).
- Neither list says whether a process has the session open now, and a
  `claude` started without `--resume` carries no session id in its command
  line.
- The window already hands a session to the terminal by its id
  (`claude --resume <id>`, `codex resume <id>`), so a conversation moves
  between the two under one id.
- Startrail release audit, B12 (2026-09-29): Codex's desktop app imports
  recent work from Claude Code and Cursor, Hermes continues Claude Code and
  Codex sessions, and Claude Code has `/resume`; Startrail could not pick up
  a session begun in a terminal. Allen asked this thread to close the audit's
  functional gaps.

## Decision

The window lists the Claude Code and Codex sessions on this Mac that it does
not hold, and takes one in when asked: the row keeps the session's own id,
reads the conversation back from the agent's transcript, and goes on in it.

Limits: a session that changed in the last two minutes may still be open
elsewhere, so taking it takes a second, deliberate press; once taken, the row
is the window's like any other; a worktree Claude Code made under
`<repo>/.claude/worktrees/` counts as the window's own worktree.

## Alternatives rejected

- **Take in a fork, under a new id** — the terminal's `--resume <id>` would
  no longer show what was said in the window, and each fork writes a whole
  new transcript; one id keeps one conversation, as the hand-off to the
  terminal already does.
- **Refuse a session some process still has open, found with `ps`** — a
  `claude` started fresh has no session id in its arguments, so the check
  misses exactly the sessions most likely to be open.

## Consequences

- A session still open in a terminal and taken in with the second press has
  two writers; keeping to one is left to the owner.
- Deleting a taken-in row deletes its transcripts like any row's (ADR 0088),
  so the terminal loses the session too, and deletes a Claude Code worktree
  where git allows.
- Listing reads every session's summary on each call, which grows with the
  owner's history; the list stops at 200.
