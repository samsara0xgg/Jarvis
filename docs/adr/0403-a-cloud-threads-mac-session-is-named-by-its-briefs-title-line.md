# ADR 0403 — A cloud thread's Mac session is named by its brief's title line

**Status:** Accepted
**Date:** 2026-10-24
**Supersedes:** none

## Context

- ADR 0294 ships each Claude Code session as fields only: no prompt, answer or tool text leaves
  the Mac. The day line names a `code` item by `custom_title`, `desktop_title`, `ai_title` or
  `agent_name`.
- A project thread on claude.ai that runs on his Mac does so through `claude remote-control`, in a
  worktree named `bridge-cse_<id>`. On 2026-10-24 a read-only check of such a transcript found
  none of those titles, no Desktop record, and no name anywhere on the Mac: the first entry is the
  coordinator's opening brief (`isMeta`), the next is his message in an envelope that carries ids
  only. The thread's name lives on claude.ai alone, and there is no supported read-only way to
  fetch it. The timeline showed these sessions blank.
- He wants the session's own name on the timeline, at the time, not a day later ("没有办法当时就
  做吗"). The coordinator writes every new thread's brief and agreed to start it with one line
  `Thread title: <title>`; he approved that convention in the thread on 2026-10-24.

## Decision

The Mac shipper takes one line of text from a session: the first line that starts with
`Thread title:` (leading spaces aside) in a user entry up to and including his first prompt. The
brief arrives wrapped in a relay tag and a note, so the line is not the entry's first; Remote
Control instructions may arrive as the first prompt. The rest of the line, at most 200
characters, is the field `thread_title`; nothing else of the entry, and nothing from any later
entry, is copied. The day line names a `code` item by it after
`custom_title` and `desktop_title` and before `ai_title` and `agent_name`.

Threads started before the line existed are named once by hand: `imports/thread-names.json` on
the brain maps `cse_<id>` to the thread's name, copied from the project's thread list on
2026-10-24 at his request, and the day line reads it after `thread_title`. A missing or unreadable
table names nothing.

A session with no name from any of these is named by its folder (`project`) when it ran on the
Mac for itself; a cloud thread's stays empty, and the device shows it as a cloud thread. A
session a program ran through the SDK with at most one prompt (`entrypoints` only `sdk-cli`, no
`remote_session`) is no `code` item at all: on 2026-10-23 such calls were 15 of 42 rows, he did
no work in them, and he agreed to leave them out.

## Alternatives rejected

- **Read the name from claude.ai with Claude Code's login.** The only route is an undocumented
  endpoint called with his account's token; the Mac would hold a credential use the brain has no
  business making, and the endpoint can change without notice.
- **A nightly letter mapping `cse_` ids to names (ADR 0295 route).** Names arrive a day late, and
  he dropped it once threads are named at the start.
- **Ship the whole brief and let the brain pick the name out.** The brief is written for the
  thread and quotes his messages; ADR 0294 keeps message text on the Mac.

## Consequences

- A session whose brief does not open with the line, and that is not in the table, stays
  unnamed. A session already read past its first entry is not read again from the start.
- A thread renamed later keeps the name it was started with; the table is not refreshed.
- The convention lives with whoever writes the brief; if it stops writing the line, sessions go
  back to blank, with no error.
