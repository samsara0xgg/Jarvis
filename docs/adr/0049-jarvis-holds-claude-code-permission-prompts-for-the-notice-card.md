# ADR 0049 — Jarvis Holds Claude Code Permission Prompts for the Notice Card

**Status:** Accepted
**Date:** 2026-09-25
**Supersedes:** 0046

## Context

- Allen, 2026-09-25, on the notice lab (artifact BVPznnBWeDHZeCyZz9QHQB,
  version 2): the marks look 星芒 and 像素 ("先做星星+像素的"), the icons
  inside follow them, both switchable later in Settings; then "然后一整套提醒
  系统全部都接进来吧". The lab's scheme answers permission prompts, questions
  and plan reviews on the card, and marks sessions compacting or stopped.
- ADR 0046 reads sessions from `claude agents --json`, job `state.json` and
  the transcript tail, and installs nothing. That listing says a session is
  `waiting`, not what for, and never says compacting or stopped on an API
  error. Its reasons against hooks for the roster (no Stop on Esc, no name,
  no job state) still hold.
- Claude Code 2.1.283 (hooks docs, and its bundled code where the docs are
  silent): `PermissionRequest` fires only when a dialog would appear; its
  stdout decision is allow, deny with a message, allow with
  `updatedPermissions` (echoing one of its `permission_suggestions`), or
  allow with `updatedInput`. `AskUserQuestion` and `ExitPlanMode` ignore an
  allow without `updatedInput`; the answers go back keyed by question text.
  Empty stdout is no decision. Matching hooks run in parallel and the first
  decision wins; hooks without one are skipped. In an interactive session
  the terminal dialog opens at the same time and the first answer wins; the
  hook process is not cancelled when the terminal answers "yes". A
  background session waits for its hooks before showing a dialog. Settings
  edits reach running sessions.
- `PreCompact` / `PostCompact` bracket a compaction; `StopFailure` carries
  an error code (`rate_limit`, `server_error`, ...) and its details.
- Vibe Island's bridge already runs on every Claude Code hook event,
  `PermissionRequest` included (timeout 86400 s).

## Decision

Jarvis takes four Claude Code hook events: it holds each
`PermissionRequest` until Allen answers on the companion's notice card and
returns that answer as the hook's decision, and it marks a session
compacting from `PreCompact` to `PostCompact` and stopped from
`StopFailure` until it works again; the roster, status and text stay read
from Claude Code's own state as in ADR 0046.

Limits: a prompt is let go with no decision, leaving Claude Code's own
dialog in charge, when no companion has read the board in the last 10 s
(also while it waits), when the hook process goes away, and when the
session writes on more than 3 s after the prompt came in. Vibe Island's
hooks stay in place. Codex approvals are shown, not answered.

## Alternatives rejected

- **Stay pull-only (ADR 0046)** — the card cannot answer anything, and on
  2026-09-25 the only fields `claude agents --json` gave were `cwd`, `id`,
  `kind`, `name`, `pid`, `sessionId`, `startedAt`, `state`, `status` and
  `waitingFor`, the waiting session's `waitingFor` reading "dialog open":
  no tool, no command, no question, no compaction, no error.
- **Take `PermissionRequest` away from Vibe Island** — Claude Code takes
  the first decision among parallel hooks and skips hooks without one, so
  both handlers can stay; removing Vibe Island's would drop the answer path
  Allen uses today before the card has answered one live prompt.
- **Learn from `PostToolUse` that the terminal answered first** — one more
  Python process on every tool call of every session; the transcript
  write-on rule uses the board Jarvis already reads.

## Consequences

- Jarvis now edits `~/.claude/settings.json` and owns a script there
  (`~/.jarvis/claude-hooks/hook.py`); every prompt, compaction and API
  error in every session passes through it.
- While Vibe Island runs, each prompt shows twice; an answer in one leaves
  the other's card up until it notices.
- A background session shows no dialog while Jarvis holds its prompt, so a
  companion that is running but ignored keeps that session waiting; the
  card folds after 30 s and reminds once.
- The write-on rule can let go of a prompt a session is still waiting on if
  something writes to its transcript meanwhile; the card then disappears and
  only Claude Code's dialog is left.
- "Always" applies Claude Code's first suggestion only.
