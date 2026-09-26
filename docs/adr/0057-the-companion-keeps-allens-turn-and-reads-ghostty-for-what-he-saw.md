# ADR 0057 — The Companion Keeps Allen's Turn and Reads Ghostty for What He Saw

**Status:** Accepted
**Date:** 2026-09-26
**Supersedes:** none

## Context

- Allen, 2026-09-25 to 26, on the notch lab 刘海里的任务 (artifact
  LpiL82fsheAM4GvmYN384X, versions 2 to 6.1): Claude Code's own line
  between "done" and "needs you" is blurry, so what matters is whether he
  has seen a session. Needs-you and unread become one list, "your turn",
  behind a beacon right of the notch ("可以做成一个清单，很棒"). A session
  asking him leaves it only when answered; a finished or stopped one leaves
  once he has looked at it, then stays as a dimmer star until he clears it.
  A finished session whose last words ask him for something follows Claude
  Code's own label (he picked that over a model reading it). On 2026-09-26:
  "来吧开干吧按照设计搞".
- `claude agents --json` and a job's `state.json` carry no read or viewed
  field, and no Claude Code hook fires when a session is looked at.
- Ghostty 1.3.1 is scriptable: `frontmost`, and `name of focused terminal
  of selected tab of front window`, which is the session's name. Logged
  every 0.4 s on 2026-09-25 while Allen switched sessions in Agent View:
  28 of 28 switches read the exact `claude agents` name, 86 title changes
  in 25 min were all session names but one brand-new session's first title,
  and the tab's own name lagged the terminal's by 0.6 s twice. One read took
  0.14 s. `focus` brings a terminal forward; `claude attach <id>` opens a
  background session in a terminal.
- 18 of his 19 sessions that day were background sessions viewed through
  Ghostty. claude.ai, the phone, other terminals and the Codex app cannot be
  read this way.
- The daemon builds the session board only when someone reads it (ADR 0046)
  and runs under launchd; the companion is a GUI app that polls the board
  every 1.5 s and is the only surface that shows this list.

## Decision

The companion keeps, in its own profile, which finished or stopped sessions
Allen has not seen and which he has cleared, and counts a Claude session as
seen once Ghostty is frontmost with that session's name on its focused
terminal for 1.5 s, or when he opens, marks or clears it from the notch; a
session waiting on him stays on his turn until its state changes.

Limits: only Ghostty is read, and only while the companion runs; anything
seen elsewhere is marked by hand.

## Alternatives rejected

- **The daemon keeps the list** — it sees a transition only when the
  companion's own poll makes it build the board, so it would learn nothing
  the companion does not; its launchd Python process has no verified macOS
  Automation grant for Ghostty, where a GUI app gets the standard prompt.
- **A model reads each finished session's last message** — Allen chose
  Claude Code's own done / blocked label on 2026-09-26, and it would cost a
  model call per finish.
- **Read the tab name** — it lagged the focused terminal's by 0.6 s twice in
  the logger run; the terminal's name never did.
- **One `osascript` per read** — a 0.14 s spawn every 0.4 s; a single
  long-lived script that reports only changes costs one Apple Event a tick.

## Consequences

- The companion sends Apple Events to Ghostty, so the first run asks for
  macOS Automation permission; refused, nothing counts as seen except what
  he opens, marks or clears from the notch.
- A session read on the web, the phone or another terminal stays on his
  turn until he marks it.
- The title of Agent View's list page has not been observed; if it keeps
  the last session's name, a session finishing while he looks at the list
  counts as seen.
- A second companion (another checkout or profile) keeps its own list.
- Finished sessions pile up in the wing until cleared; the board drops them
  after 24 h anyway.
