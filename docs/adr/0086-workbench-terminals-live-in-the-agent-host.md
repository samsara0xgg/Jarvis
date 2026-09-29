# ADR 0086 — Workbench terminals live in the agent host

**Status:** Accepted
**Date:** 2026-09-29
**Supersedes:** none

## Context

The workbench prototype (`docs/design/agents-workbench.html`) has a real
terminal per session, opened with ⌃`, in the session's folder. A command
must keep running while Allen switches sessions, closes the window, or the
companion restarts; landing restarts the companion itself (ADR 0085). The
window's renderer is sandboxed. The agent host already outlives companion
restarts and knows each session's folder. Interactive programs (an editor,
`claude`, npm's prompts) need a TTY.

## Decision

Run one pseudo-terminal per session in the agent host through node-pty,
loaded on first use; stream its output to the window over server-sent events
with a rolling 256 kB replay, take keys and sizes over POST, and draw it with
xterm only while the pane is on screen.

## Alternatives rejected

- **PTYs in the companion's main process**, as the Codex and Hermes desktop
  apps do — every companion restart, including the one a desktop landing
  performs, kills the commands running in them.
- **A command runner without a TTY** — one spawn per line cannot host
  interactive programs, line editing or the shell's own prompt.
- **A WebSocket for the stream** — needs the `ws` dependency or a
  hand-written frame parser, for no delay a person can notice on loopback,
  where the host already speaks server-sent events.

## Consequences

node-pty is a native dependency whose prebuilt spawn helper npm installs
without its execute bit; the host restores it before the first spawn. Shells
outlive the window and end only with their session or the host. The host
process now carries interactive shells with Allen's full environment.
