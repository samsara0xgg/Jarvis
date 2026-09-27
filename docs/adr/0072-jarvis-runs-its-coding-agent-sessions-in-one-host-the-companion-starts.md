# ADR 0072 — Jarvis runs its coding-agent sessions in one host the companion starts

**Status:** Accepted
**Date:** 2026-09-26
**Supersedes:** none

## Context

- Allen, 2026-09-26: the terminal's Agent View is poor, and he wants
  Jarvis's own window for his coding agents: 「一步到位做官方包吧」, private
  use for now, other agents connected later, and an archive that Agent View
  lacks. Anything Agent View can do, the window must also do. He approved the
  window design (lab FHrstDSC v2): 「没毛病！开写吧」.
- Each agent has one official rich-client interface:
  - Claude Code has the Claude Agent SDK. Its TypeScript package is the
    richest: token stream, a permission callback that also carries
    AskUserQuestion, interrupt, queued input, model / effort / mode
    switches, session listing and transcript reading, fork.
  - Codex has `codex app-server`, the JSON-RPC interface its own desktop
    app uses. Jarvis already drives it for workers (§15.1).
  - Both use Allen's own sign-in (Claude subscription, ChatGPT login). An
    `ANTHROPIC_API_KEY` in the environment wins over the subscription.
- Both interfaces run the agent as a child over stdio. The child dies with
  its parent, and there is no supervisor to reattach to. A session can be
  resumed by id, but a turn that is running when its parent dies is lost.
- Turns run for tens of minutes. Allen restarts the daemon after every
  backend landing, and `scripts/launch.mjs` restarts the companion after
  every desktop build.
- Each agent already keeps the whole conversation in its own transcript
  (`~/.claude/projects`, `~/.codex/sessions`), and so does a turn
  continued in the terminal (`claude --resume`, `codex resume`). Only one
  process may write a session at a time.
- Neither agent keeps what the window adds: pinned, which agent, the
  worktree Jarvis made. Whether Allen has seen a session, parked it or
  archived it is already kept by the daemon, in the marks every surface
  shares (ADR 0069).
- The Agent Client Protocol (ACP) reaches Cursor, Copilot, Gemini and
  others. It has no steer, queue, fork or archive, and it reaches Claude
  and Codex only through third-party adapters.

## Decision

Run every coding-agent session Jarvis starts in one agent host: a Node
process that the companion starts when nothing answers on its port, and
that keeps running when the companion or the daemon restarts. The host
drives Claude Code through the Claude Agent SDK and Codex through
`codex app-server`, and it answers the Agents window over local HTTP.

Limits:

- A conversation is read back from the agent's own transcript. Unread,
  parked and archived are the daemon's marks (ADR 0069): the host decides
  unread for its own sessions and writes all three there. The host's own
  file keeps only the rest, and a copy of the marks for when the daemon is
  away.
- One writer at a time. Handing a session to the terminal releases it, and
  taking it back re-reads the transcript.
- Sessions started elsewhere (terminal, the Codex app) are not imported.
  Allen's older terminal sessions finish in the terminal.
- A new session gets its own git worktree unless Allen unticks it.
  Deleting a session removes its worktree only where git itself allows it.
- The island's notices keep reading Claude Code's own state (§15.3) until
  they move onto the host's sessions.
- Other agents come later, through one ACP client in the same host.

## Alternatives rejected

- **Sessions inside the companion's main process** — every desktop build
  restarts the companion through `launch.mjs`, and each restart would end
  every running turn with it.
- **Sessions inside the Python daemon** — the daemon restarts after every
  backend landing, with the same loss.
- **A third LaunchAgent for the host** — launchd would restart a crashed
  host, but its running turns are already gone by then. Everything that
  uses the host lives in the companion, which can start it. The
  install, status and uninstall plumbing in `jarvis/deployment` would buy
  no extra uptime.
- **Driving `claude attach` in a hidden terminal** (the island's reply route
  for terminal sessions, ADR 0070) — it types into a TUI. Approvals, the
  token stream and the model switch never arrive as data, and Codex has no
  counterpart.
- **Jarvis's own copy of each conversation** — a turn continued in the
  terminal writes only the agent's transcript. Jarvis's copy would be
  stale on take-back, so it would have to be rebuilt from the transcript
  anyway.
- **ACP adapters for Claude and Codex** — ACP v1 lacks steer, queue, fork
  and archive, which the window uses. The adapters also add a third-party
  hop in front of the two official interfaces.

## Consequences

- Changing the host's code needs the host restarted. That ends the turns
  that are running then, and their sessions resume from their transcripts.
- A crashed host stays down until the companion next needs it.
- The SDK runs the Claude Code binary bundled in its package (about
  200 MB). That binary moves with the package version, not with the
  terminal's `claude`.
- While the host holds a Codex thread, the Codex desktop app cannot write
  to it.
- A session's list row is only as fresh as the host's file until the
  session is opened and its transcript is read.
- The companion reads the marks once when she starts (ADR 0069), so a mark
  the window writes shows beside the notch only after her next start.
- The subscription is used through the SDK. The SDK's terms allow ordinary
  individual use and require an API key for a product others use. A
  public Jarvis needs this revisited.
