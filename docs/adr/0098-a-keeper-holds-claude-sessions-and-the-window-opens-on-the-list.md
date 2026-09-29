# ADR 0098 — A keeper holds Claude sessions, and the window opens on the list

**Status:** Accepted
**Date:** 2026-09-29
**Supersedes:** 0082

## Context

- ADR 0073 runs every Agents-window session in one agent host and accepts
  that a turn running when the host dies is lost: the Agent SDK runs Claude
  Code as a child over stdio, and there was no supervisor to reattach to.
- Allen develops the host itself, so it restarts many times a day. On
  2026-09-29 one restart cut three running sessions, and killing the host
  left their `claude` children running as orphans (PPID 1). They finished
  their turns unseen while the next host resumed the same sessions beside
  them: two writers on one transcript and one worktree.
- Allen, 2026-09-29: sessions must be independent of the client. They keep
  running while it is closed, and reopening returns to exactly where they
  were: 「打开以后，可以立刻恢复到关掉之前的状态」.
- The Agent SDK accepts `spawnClaudeCodeProcess`, a custom spawner that only
  has to return stdin, stdout and exit events. A spike on 2026-09-29 ran a
  child through a separate process, killed the host during a 20 s command
  and during an open permission request, and started a new host on the same
  child: the command finished, the request was answered from the new host,
  and the session took its next message. A second SDK `initialize` had to be
  answered from the first one's cached reply.
- A `claude` child with its stdin still open waits between turns, so an
  idle session costs one resident process, as it already did under the host.
- A kept child goes on while no host is attached: its turn runs on, and a
  request it makes waits unanswered. The keeper holds those lines for the
  next host.
- ADR 0082 gave the children to a keeper and showed the window nothing until
  every kept child was taken back and every open conversation read, each by
  parsing its whole transcript. Startrail release audit B23 (2026-09-29):
  every start waits on that. In a Linux container with the stand-in Claude
  Code and 40 sessions of made-up 10 MB transcripts, the list came 2.3 s
  after the host started, and 0.35 s when nothing was read first; the one
  conversation opened then took 0.23 s.
- The window already shows 「在读这个会话…」 for a conversation it has not
  got and asks for it when it is opened. Every route that shows or changes a
  conversation reads it first; search and the landing's drafted commit
  message used only what was already read.

## Decision

Give each Claude Code session's child to a keeper: a small process of its
own that outlives the host, holds the child's stdio, and hands the child back
to the next host with what it missed. The host keeps everything else, and
sends the window the list of sessions as soon as it has read the list back.

Limits:

- The keeper replays only what the transcript cannot supply: lines since
  the child's last result, requests still open, and the first handshake's
  answer. The conversation is still read back from the transcript.
- A conversation is read the first time something needs it: the window
  opening it, a search, a landing, a message sent to it.
- Every kept child is taken back right after the list is sent, not when its
  session is opened; whatever acts on that session waits for it.
- Restarting the keeper still ends running turns, so it stays small and
  rarely changes.
- Codex's `codex app-server` stays a child of the host for now. Its turns
  still end with the host.

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
- **Read every conversation before sending the list** (ADR 0082) — the list
  waits on the slowest reads: 2.3 s against 0.35 s in the measurement above,
  growing with every session kept in the list.
- **Take a kept child back only when its session is opened** — a request its
  running turn makes would wait unseen, with no row showing that it waits and
  no notification, until the owner happened to open that session.

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
- The first search after a start reads every conversation it covers, so it
  takes as long as the start used to.
- Taking the kept children back still reads their conversations at start,
  behind the list: many kept sessions slow the first opening of any other.
