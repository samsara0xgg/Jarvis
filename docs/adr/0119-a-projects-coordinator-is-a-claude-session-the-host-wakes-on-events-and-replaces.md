# ADR 0119 — A project's coordinator is a Claude session the host wakes on events and replaces

**Status:** Accepted
**Date:** 2026-10-01
**Supersedes:** none

## Context

- Allen, 2026-10-01, asked for Claude Projects' way of working in Startrail:
  whichever session the owner talks to, the project answers as one, and work moves
  to the session that owns it (ADR 0118 has the project itself).
- Claude's coordinator is an ordinary Claude Code session with a prompt of
  its own and its tools cut to dispatching; its plain text never reaches the
  owner; by default it runs Opus at low effort; it "works from recent
  messages, recent threads, and project memory rather than its full
  history", and the Jarvis project's coordinator changed session id three
  times in two days (local records, 2026-09-29).
- Allen's local project threads, 2026-09-27 to 10-01:
  - the server refuses a thread's message to a sibling thread ("may send
    messages only to the conversation session that started it"); every
    cross-thread warning went through the coordinator, which relayed an
    owner's message in one thread to another within 6–9 s;
  - 3 of 56 coordinator relays carried the owner's words copied by the
    server, the rest the coordinator's paraphrase; one thread refused an
    install "the owner authorised" in the coordinator's words, and Allen
    copied instructions between threads by hand because a session on the Mac
    takes only the owner's own messages;
  - the coordinator posted over 30 "stuck on a permission prompt" notes and
    struck each through by hand once answered; one push waited 7 h 20 min.
- ADR 0023: no model call on a timer. ADR 0094: in the installed app a
  Claude session spends the owner's own account.
- The Agent SDK serves tools from the host's own process
  (`createSdkMcpServer`) and limits a session's tools (`tools`,
  `allowedTools`, `canUseTool`).

## Decision

A project may have a coordinator: a Claude session the host runs with a
prompt of its own, file reading anywhere and writing only in the project's
memory and files folders, and host tools to post in the project's message
stream, edit its own posts, start a session with a brief, pass a note to a
session, draft a message the owner sends as their own, and list or read the
project's sessions. Only those tool calls reach the owner.

The host wakes it only when the owner posts in the stream, the owner writes
in one of the project's sessions, or one of them ends a turn, batching what
happens within 1.5 s; and it starts a fresh one, given the project's
instructions, memory index, sessions and last 30 posts, when the last one's
context passed 100k tokens or it sat idle an hour.

Limits: the coordinator's words reach a session marked as its own; the
owner's words reach a session only as the host's verbatim copy of a message
the coordinator names; the host, not the coordinator, posts that a session
waits for the owner and strikes the post through once answered; the
coordinator is switched per project and defaults to Sonnet at low effort.

## Alternatives rejected

- **Sessions message each other directly** — Claude's Projects refuses it,
  and in Allen's records each cross-session warning (a merge, a reset demo
  environment, a fix owned elsewhere) needed the one session that sees every
  thread.
- **Compact the coordinator** — what it needs is on disk (posts, sessions,
  memory) and a fresh session costs one cached prompt; Claude's own
  coordinator is replaced, not compacted.
- **Let the coordinator pass the owner's consent in its own words** — a
  thread refused exactly that in the records, and a model-written "the owner
  approved" cannot be told from a forged one; a copy made by the host can.
- **The coordinator reports approval waits** — over 30 model turns in the
  records for a fact the host already holds (a session's state is `wait`).

## Consequences

- Every turn a project session ends costs a coordinator turn on the
  owner's account; switching the coordinator off leaves ADR 0118's project.
- A replaced coordinator forgets what is not in memory, the stream or the
  session list.
- Sessions have no tool to reach the coordinator; it hears from them only
  when a turn ends.
- The coordinator can start sessions that spend, as the owner could.
