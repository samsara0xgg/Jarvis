# ADR 0082 — A keeper holds Claude sessions through host restarts

**Status:** Accepted
**Date:** 2026-09-29
**Supersedes:** none

## Context

- ADR 0073 runs every Agents-window session in one agent host and accepts
  that a turn running when the host dies is lost: the Agent SDK runs Claude
  Code as a child over stdio, and there was no supervisor to reattach to.
- Allen is now developing the host itself, so it restarts many times a day.
  On 2026-09-29 one restart cut three running sessions, and killing the host
  left their `claude` children running as orphans (PPID 1). They finished
  their turns unseen while the next host resumed the same sessions beside
  them: two writers on one transcript and one worktree.
- Allen, 2026-09-29: sessions must be independent of the client. They keep
  running while it is closed, and reopening returns to exactly where they
  were: 「打开以后，可以立刻恢复到关掉之前的状态」.
- Hermes desktop, the reference he pointed to, has no process of this kind.
  Its backend dies with the app, a renderer reload replays a 512-event ring,
  and a backend restart loses the turn and re-sends the last prompt if it is
  under 15 minutes old.
- The Agent SDK accepts `spawnClaudeCodeProcess`, a custom spawner that only
  has to return stdin, stdout and exit events. A spike on 2026-09-29 ran a
  child through a separate process, killed the host during a 20 s command
  and during an open permission request, and started a new host on the same
  child. The command finished, the request was answered from the new host,
  and the session took its next message. For that, a second SDK
  `initialize` had to be answered from the first one's cached reply.
- A `claude` child with its stdin still open waits between turns, so an
  idle session costs one resident process, as it already did under the host.

## Decision

Give each Claude Code session's child to a keeper: a small process of its
own that outlives the host, holds the child's stdio, and hands the child back
to the next host with what it missed. The host keeps everything else.

Limits:

- The keeper replays only what the transcript cannot supply: lines since
  the child's last result, requests still open, and the first handshake's
  answer. The conversation is still read back from the transcript.
- Restarting the keeper still ends running turns, so it stays small and
  rarely changes.
- Codex's `codex app-server` stays a child of the host for now. Its turns
  still end with the host.
- The host shows the window nothing until every kept child has been taken
  back and every open conversation read.

## Alternatives rejected

- **Hermes' way: turns die with the host, and the last prompt is re-sent
  when the session reopens** — the step that was running is redone and the
  reasoning spent on it is lost. On 2026-09-29 the cut sessions were 8 to 20
  minutes into their turns.
- **Leave orphaned children running and resume beside them** — observed on
  2026-09-29: the orphan and the resumed session both wrote the same
  transcript and edited the same worktree, and one session killed the other
  as a stray.
- **A LaunchAgent in place of the host** — launchd would restart the host
  after it dies, but the children would still die with it.
- **Driving `claude --bg` sessions through Claude Code's own daemon** — the
  window would type into a TUI, and approvals, the token stream and the model
  switch would not arrive as data (the reason ADR 0073 rejected `claude
  attach`).

## Consequences

- The keeper's protocol is Claude Code's stream-json control protocol seen
  from outside. An SDK or CLI release that changes the handshake, or how
  requests and answers are paired, breaks reattachment. A normal session
  still runs, because the keeper forwards everything else unread.
- Children live outside the host's process group, so stopping the host no
  longer stops them. A session is stopped by releasing it through the SDK,
  or by stopping the keeper, which ends all of them.
- A child whose session was deleted while no host ran is stopped by the next
  host at boot. A child the keeper holds with no host at all runs until the
  keeper is stopped.
- A host with many sessions opens the window later, because boot reads every
  open conversation first.
