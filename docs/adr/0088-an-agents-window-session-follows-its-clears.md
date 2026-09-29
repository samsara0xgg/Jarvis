# ADR 0088 — An Agents-window session follows its clears

**Status:** Accepted
**Date:** 2026-09-29
**Supersedes:** none

## Context

- ADR 0073 keys each Agents-window row by the Claude Code session id and
  stores no conversation: it resumes, reads back, hands to the terminal,
  renames, forks and deletes by that id.
- `/clear` (and a plan run with a clean context) does not empty a session.
  Claude Code writes a `conversation_reset` frame, then goes on under a new
  session id in a new transcript. On 2026-09-29 the row
  `b3ca2534` was cleared; the child went on in `a53c1e30`, while the row
  kept `b3ca2534`. The next read-back would have shown only the old
  conversation, the next resume would have restored it, and `claude --resume`
  from the window would have opened it: the clear undone without a sign.
- The frame's `new_conversation_id` is not the new session id (a probe on
  2026-09-29 read `9157540a` there while the transcript was `95c4d308`); every
  later message carries the new id in `session_id`.
- Allen, 2026-09-29: after a clear the context must really be empty, and the
  old conversation must stay in the window above it, because he often forgets
  to copy the end of a session before clearing.

## Decision

A Claude row stays one row across its clears: it records each new session id
in order, runs, resumes and hands off in the last one, and reads all of them
back with a line where the context was cleared.

Limits: a clear made in the terminal while the session is handed off is not
seen; taking it back resumes the id the row knew. Codex rows are unchanged
(the window offers no `/clear` for Codex).

## Alternatives rejected

- **Each clear opens a new row** — the old conversation leaves the view Allen
  is working in, which is exactly what he asked to keep; and the row's pins,
  marks, worktree and landing would have to be copied to the new row.
- **Re-key the row to the new id** — the id is also the keeper's key for the
  child (ADR 0082), the daemon's key for marks (ADR 0069) and the window's
  selection; changing it mid-session breaks all three, and the old
  conversation still drops out of the read-back.

## Consequences

One row now owns several transcripts, so deleting it deletes them all, and a
fork starts from the last one only.
