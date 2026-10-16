# ADR 0274 — What he has to go to is read out of every mail into one log the day and her tools share

**Status:** Accepted
**Date:** 2026-10-15
**Supersedes:** none

## Context

- 2026-10-14/15 he missed a weekly online group (Wednesdays 18:30, Sep 16 to Oct 21, the times
  in a 2026-09-11 mail in his Gmail), a workshop, and almost a monthly check-in. The job-mail
  poller (ADR 0155) read the group's mail and set it aside as `not_job`: nothing in Jarvis asks
  whether a mail holds something he must attend. Planning his day is the first thing he asked of
  Jarvis (design doc 「Jarvis 每天的计划：设计」, accepted by him 2026-10-15); knowing what he
  has to go to comes first.
- He gets 15 to 53 mails a day (10-05..10-15). memory.db keeps the first 3000 characters of the
  body of 1,340 mails since 2026-09-10 (ADR 0162).
- The job-mail poller reads nothing without `OPENROUTER_API_KEY` and reads at most
  `jev.room()` mails a cycle (400 Jev calls a day): tied to it, intake stops when Jev does.
- Interview mail already has its own two reminders and an Outlook event (ADR 0186), and that
  event comes back through the calendar read (ADR 0248, 0230).
- A model reading a mail can be wrong about whether it is his, or about the time. A wrong item
  that nags him is worse than one he is asked about.
- Some of these items are private (counselling, legal, health); the day line and the lock screen
  are seen by others near him.
- The phone's day decoder drops an item kind it does not know (`RealDay.swift`, `default:
  return nil`); `event` is a kind every installed build draws.

## Decision

Things he has to go to ("commitments") are events in the runtime log, folded like reminders
(ADR 0179), and every reader of his schedule reads the same fold. A commitment has a title, a
start, an optional end, an optional weekly repeat up to a last date with skipped dates, a place
or link, where it came from (a mail's id, or his own words), `private`, and `confirmed`.

- A commitment poller reads his mail on its own: its own Gmail listing (read-only tools only,
  ADR 0155's guard), the kept body snapshot when there is one, a cheap local date/time filter,
  then one call to the `haiku-bg` preset per mail that passes. It does not need Jev, OpenRouter
  or the Jev budget. Every mail it looks at is recorded with what the model was given and said.
- A mail-made commitment starts unconfirmed. He confirms or drops it in conversation; a
  commitment he states himself is confirmed. A sender he marks "always" or "never" is applied to
  that sender's later mail without asking.
- Mail the job ledger types as an interview or offer is left to ADR 0186, so each interview has
  one set of reminders.
- On first start the poller reads every mail since `backfill_since` (2026-09-10), a capped number
  a cycle.
- Until the plan's own reminders exist, each occurrence in the next seven days gets one ADR 0179
  reminder `before_min` before it, whose text for a private commitment is only its time.
- `my_schedule`, the state block's "next" line and `GET /inherent/day` (as kind `event`, with
  `source`, `confirmed` and `private`) all read the fold.

## Alternatives rejected

- **Ask Jev's existing body call one more question.** Its reads stop with no OpenRouter key and at
  400 calls a day, and its model is trained for job typing; the group's mail was read by it and
  set aside.
- **Write commitments into his Outlook calendar.** The brain's Outlook connection is absent on
  the Pi most days (ADR 0230 reads it empty), a wrong extraction would land in his real calendar,
  and a soft or unconfirmed item has no place there.
- **A new day-line kind `commitment`.** Every build on his phone drops it until a phone update is
  installed; `event` with extra fields is drawn today and read in full by the next build.
- **Auto-confirm a mail the model is sure of.** A wrong "sure" nags him; one question in
  conversation costs a sentence, and a sender rule removes the question for regular senders.

## Consequences

- Two pollers list the same mailbox, about 100 extra read-only Gmail calls a day.
- The interim reminder is an ordinary ADR 0179 reminder: it rings once, is fixed when set and
  does not follow the quiet level; the plan's nudges replace it.
- A commitment that only lives on a website (the workshop) is still not seen.
