# ADR 0230 — The day line shows what is planned: calendar events and pinned departures

**Status:** Accepted
**Date:** 2026-10-11
**Supersedes:** none

## Context

ADR 0199 wants the day ring's future half to show what is coming. On
2026-10-11 he agreed that planned things belong on the ring, drawn dashed.

- Today only reminders (ADR 0179) reach the future half.
- His calendar is Outlook, read through Microsoft Graph (ADR 0055) by
  `Home.today()` and served for today only at `/inherent/today`. It is not in
  the event log.
- Pinned departures (ADR 0204) are served as notices, not as day items.
- The day line is folded when asked and stores nothing (ADR 0199). A Graph
  read takes seconds and can fail.
- The phone's own calendar sees only accounts added on that phone, and it is
  not the owner of his plans.

## Decision

The day line serves the asked day's Outlook calendar events and his pinned
departures as items in his lane, with state `next` until they start. The Graph
read goes through a short in-memory cache. A failed read is listed in
`missing` and adds nothing.

Limits:
- Read only. The ring never writes to his calendar.
- All-day events are a label for the day, not a stretch of the ring.
- Past days show only what Graph still returns for them. Nothing is copied
  into the event log.

## Alternatives rejected

- **Copy the calendar into the event log on a poll** — that makes a second
  copy of Outlook state. Deletions and moves would need a diff, and the copy
  drifts every time a diff is missed.
- **Read EventKit on the phone** — the Outlook account need not be on the
  phone. The Mac and a second device would show a different future.
- **Serve plans on a separate endpoint** — the ring would merge two timelines
  on the phone, and ADR 0199 chose one timeline folded on the brain.

## Consequences

- A cold `/inherent/day` call can wait on Graph. The cache bounds this only
  after the first read.
- When Graph or the Microsoft MCP server is down, the future half shows only
  reminders and departures, and lists `calendar` as missing.
