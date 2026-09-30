# ADR 0103 — Editing a sent message makes a new version of the conversation

**Status:** Accepted
**Date:** 2026-09-30
**Supersedes:** none

## Context

- Startrail (ADR 0073) lets Allen change something he said and send it
  again, and step between what the conversation became each time
  (Startrail one-window design, points m-edit and m-ver; Allen, 2026-09-30:
  「就全都按你的来做吧」).
- Neither agent edits a sent message in place. Claude Code's SDK forks a
  session up to a message (`forkSession` with `upToMessageId`) and puts files
  back from its checkpoints (`rewindFiles`); Codex's app server forks a
  thread before a turn (`thread/fork` with `beforeTurnId`) and has no file
  rollback.
- The window keys everything about a session by its id: its place in the
  list, pinned and parked, unread, reactions, marks the daemon keeps.
- A Claude Code transcript is appended to, never cut; what came after the
  edited message exists only in the session it was said in.

## Decision

Make an edit a fork: the conversation before the edited message becomes a new
session that carries the new words, the old title, place in the list, marks
and the reactions on what stays; the session edited is archived as the
version before it, out of the list, and the page steps between versions.

Limits:

- A running turn stops first; what was queued behind it is taken back.
- Claude's files go back to the edited message's checkpoint; Codex's do not,
  and the conversation says so. Stepping between versions never moves files.
- Writing in an older version makes it the current one again.
- Versions of one conversation share a root, and each edited message a family
  with numbered versions, kept on the sessions in the host's store.
- A session in a terminal, or whose worktree landing cleaned away, cannot be
  edited.

## Alternatives rejected

- **Rewind in place: cut the session back to the message and send again in
  the same session** — Claude Code keeps no branch point in the session
  itself: after the fork the old answers are only in the old session, so
  cutting it would make them unreachable from the window.
- **List every version as a session of its own** — each edit adds a row with
  the same title; three edits of one message show four look-alike sessions.
- **Only the new session, the old one deleted** — the step back to an earlier
  version is the point of m-ver, and deleting the transcript makes it
  impossible.

## Consequences

- The host's store grows by one archived session per edit, and each keeps
  its transcript on disk.
- Codex conversations can differ from their files after an edit.
