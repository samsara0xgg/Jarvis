# ADR 0186 — An interview time from job mail sets two reminders and an Outlook event by itself

**Status:** Accepted
**Date:** 2026-10-06
**Supersedes:** none

## Context

- Allen, 2026-10-06, asked that when job mail yields an interview time Jarvis set reminders on its own,
  with no asking, and that the interview also reach his iPhone's built-in Calendar so it alerts him
  while the Mac is closed: the daemon's reminders (ADR 0179) only ring where the daemon runs.
- A reminder is already a pair of events in the event log, folded by `jarvis/state/reminders.py`; the
  model tool `set_reminder` is the only writer, and another thread owns that code.
- His Outlook account is connected as the `microsoft` MCP server (ms-365-mcp-server), whose tools
  include `create-calendar-event`, `update-calendar-event` and `delete-calendar-event`. The iPhone
  push is a stopgap: the Pi brain plus an iPhone app (ADR 0183) are to push reminders themselves.
- The next interview time is already read at request time from the ledger's mails (ADR 0177, 0182);
  it can change when a mail reschedules, and vanishes when the application is rejected or hidden.
- Writing to a calendar is the first thing job mail does outside Gmail. A wrong or repeated write
  puts a false or doubled event in a calendar Allen does not review.

## Decision

Reconcile, after every poll cycle and at start, each application's next interview time with what was
armed for it, and arm what is missing by itself.

Its limits:

- **What is armed.** Two reminders, 20:00 local the evening before and 30 minutes before the start,
  written as the `reminder.scheduled` and `reminder.cancelled` events of ADR 0179 with the payload
  `set_reminder` writes, and one Outlook event with a 30-minute alert. Config:
  `job_mail.interview_reminders {enabled, evening_at, before_min, outlook}`.
- **Idempotent.** A table `job_interview_reminder`, one row per application, holds the armed time, the
  two reminder ids and the Outlook event id. A reminder id is stored before its event is written, so
  a pass cut short writes the event on the next one. A moment already past is skipped, not set late.
  A changed time cancels the old reminders, sets two new ones and updates the one event. A rejected,
  hidden or time-less application has its reminders cancelled and its event deleted; a time already
  past is an interview that happened and is left in the calendar.
- **Undo.** `POST /inherent/jobs/applications/{id}/cancel-reminders` cancels both, deletes the event
  and marks that time cancelled, so no pass arms it again; a new time arms again. The Jobs card shows
  what is set and the button.
- **Outlook is guarded.** Only the three event-writing tools are called, and update or delete only
  with an event id this module created and stored. A failure is logged and retried by the next pass,
  never undoes a reminder and never stops the poller. The event carries the company, role, time (end
  from the invitation's range, else one hour), platform and join link, so those leave for Microsoft.
- **Stopgap.** The Outlook event exists only until the brain pushes reminders to the phone; then the
  `outlook` switch goes off and this ADR is superseded for that part.

## Alternatives rejected

- **Ask Allen before each one** — he said no asking; and an interview time is a fact he has already
  decided to act on by replying to the invitation.
- **Call `set_reminder` through the tool registry** — the tool is `JARVIS_LLM`-only, writes an action
  id from a model turn, and refuses any moment more than a minute past; the poller has no turn, and
  the evening slot is sometimes already past by design.
- **Search Outlook for an existing event instead of storing its id** — it needs a read tool and a
  matching rule on subject and time, and would let an update or delete reach an event Jarvis did not
  create; one stored id makes the guard exact.
- **Rewrite the event on every pass** — a Graph write each five minutes per application; the stored
  signature of what was sent costs nothing and writes only when the time or details change.

## Consequences

- A cycle cut between creating the Outlook event and storing its id leaves an orphan event and writes a
  second one on the next pass; the window is one local write.
- An Outlook event Allen edits by hand is overwritten by the next change of the time or details.
- The reminder text uses the language setting at the time it is armed, not when it rings.
