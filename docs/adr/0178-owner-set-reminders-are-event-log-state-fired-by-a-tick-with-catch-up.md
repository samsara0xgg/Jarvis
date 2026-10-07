# ADR 0178 — Owner-set reminders are event-log state, fired by a tick with catch-up

**Status:** Accepted
**Date:** 2026-10-06
**Supersedes:** none

## Context

- 2026-10-06, Allen: 「今天下午4:00-4:20有和employer一对一的meeting提前30分钟提醒我下」.
  Jarvis had no reminder tool, so the model called `create_memo`, said it had
  recorded a reminder for 3:30 PM, and nothing fired. `docs/spec.html` already
  promised that a reminder Allen sets rings at every quiet level.
- ADR 0170: state and scheduling live with the brain, and the brain restarts and
  its host sleeps, so a timer held in memory loses a reminder that was due while
  it was down.
- A memo has no time and a reminder has one; a tool that is both lets the model
  claim a ring that will never happen.

## Decision

A reminder is a `reminder.scheduled` event written by the model-only tool
`set_reminder(at, text)`, with `at` an absolute time carrying its UTC offset
(the model works out any lead time itself); what became of it is the later
`reminder.cancelled`, `reminder.fired` or `reminder.acknowledged` event, folded
from the log, with no table of its own. The daemon folds the log at start and
every 15 seconds and fires each pending reminder whose time has passed, so one
missed while the daemon was down or the Mac asleep rings at the next tick and
says how late it is; `reminder.fired` is written before anything is shown or
said, so a reminder rings exactly once. It ignores the quiet level and the away
hold: a card with sound is served through `GET /inherent/notices` until Allen
takes it in, with one spoken line unless he is on a call, speech is off, a
conversation is live or the output is not private; one more than 12 hours late
is a card only. It is never merged into a job-mail digest. `create_memo` stays a
memo and its description points to `set_reminder` for a time.

## Alternatives rejected

- **A timer per reminder in the daemon** — loses every reminder due during a
  restart or a sleep; the fold plus a tick recovers them with no extra state.
- **A reminders table next to the job ledger** — a second store to keep in step
  with the log that ADR 0170 already makes the brain's state.
- **Ask the model for a relative delay ("in 30 minutes")** — the owner's
  sentence anchors on the meeting's time, so a relative delay needs the model
  to subtract from a clock it may misread; an absolute time with an offset is
  checkable (past and over a year ahead are refused) and is what the tool echoes
  back for the model to confirm.

## Consequences

Polling means a ring is up to 15 seconds late. A `seen` post does not take a reminder in, only a
dismissal does, and the companion keeps the card on the island until he dismisses it, so one that
came up while he was away is still there when he is back, and after a companion restart. The companion draws a reminder as
it draws a job-mail notice and holds notices itself at `no-pop`, `dnd` and during
a call or away hold, so those tiers show the card only once the companion stops
holding; the daemon side serves it at every tier. A brain with no private audio
output (a Linux host) never speaks a reminder, only serves the card.
