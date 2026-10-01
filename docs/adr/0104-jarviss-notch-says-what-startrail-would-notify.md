# ADR 0104 — Jarvis's notch says what Startrail would notify

**Status:** Accepted
**Date:** 2026-09-30
**Supersedes:** none

## Context

- ADR 0073 runs every Startrail (Agents window) session in the agent host;
  ADR 0095 gives the host a key that main adds to a page's requests, so no
  page holds it.
- Before this, each signal that a session waited on Allen kept its own copy
  of the rule: the window's list, the Dock badge (counted in main, and
  counting sessions held in a terminal, which the window does not) and the
  system notification.
- Allen, 2026-09-30: Startrail may become an app of its own, apart from
  Jarvis, so its own way of saying it while he is away stays; but when Jarvis
  runs beside it, Jarvis's notch should say what needs him
  (「如果 Jarvis 和它同时在线，也可以用 Jarvis 的刘海屏来提示」).
- The notch already drops a card for Claude Code sessions in a terminal
  (their hooks, through the daemon; `scripts/claude_hook.py` skips sessions
  the host runs, so they are never counted twice).
- A system notification and a notch card for the same request land within a
  second of each other, and answering the card leaves the banner up until
  macOS dismisses it.
- Main hands the companion's page the host's port only in the dev build
  (`electron/companion.ts`); the installed app's notch cannot reach the host.

## Decision

Order every "waits on you" signal, in the window and in Jarvis's notch, by
one rule (`src/agents/queue.ts`), and while the notch follows the host and
the owner's `notify.notch` is on, let the notch say what Startrail's system
notifications would say and send none of them.

Limits:

- The rule: a request or question first, then an error not yet read, then a
  finish not yet read; within each, the one that has waited longest. Parked,
  archived and terminal-held sessions never wait.
- The notch reads the host's event stream and answers through the host's own
  routes, as the window does; main adds the key. A request the notch cannot
  show in full (an MCP server's form) is answered in Startrail.
- `notify.notch` lives in the host's settings, on unless turned off, and is a
  choice only where the notch follows the host. Where it does not (the
  installed app), system notifications go as before.
- The Dock badge goes; the Dock icon stays while the window is open.

## Alternatives rejected

- **Both the notification and the notch card** — one request raises two
  alerts at once, and the banner outlives the answer given on the card by
  the seconds macOS keeps it.
- **Route Startrail's sessions to the notch through the daemon, as terminal
  sessions' hooks are** — the host already emits every change as an event;
  the daemon would need a second copy of the waiting rule, and
  `scripts/claude_hook.py` would have to stop skipping host sessions, which
  is what keeps a session from being counted twice.
- **Keep the Dock badge beside the notch** — a third place saying the same
  count, computed in main by a copy of the rule that had already drifted
  (a session held in a terminal counted there and nowhere else).

## Consequences

- The companion holds a second connection to the host's event stream; a host
  restart makes the notch reconnect, as the window does.
- The installed app has no notch path until main can hand its page the
  host's port with the key kept out of it.
- The window, her marks and the notch all depend on `queue.ts`: changing the
  rule changes every signal at once.
