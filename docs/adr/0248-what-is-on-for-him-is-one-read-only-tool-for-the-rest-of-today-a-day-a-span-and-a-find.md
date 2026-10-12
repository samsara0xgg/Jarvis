# ADR 0248 — What is on for him is one read-only tool for the rest of today, a day, a span and a find

**Status:** Accepted
**Date:** 2026-10-11
**Supersedes:** the clause of ADR 0127 that puts `get-calendar-view`, `list-todo-task-lists` and `list-todo-tasks` in the shipped `always_loaded` list. Everything else in ADR 0127 stands, and ADR 0036 and ADR 0055 (his calendar and to-dos live in Microsoft; the home reads them without a model) stand as they are.

## Context

- Replay exam, 2026-10-11: "What's on my calendar tomorrow", "this week", 「我明天几点出门？」 and
  「我看一下日程」 are answered "your calendar is clear" without any calendar call (Haiku, mostly 3 of
  3 runs); Luna sometimes calls `web_search`. The three Microsoft reads were on the menu with the
  server's generic Graph descriptions, 10,581 characters of description and schema together.
- Live log on the Pi brain, 2026-09-29 to 2026-10-10: `get-calendar-view` was called 13 times
  and 12 answers were `{"value":[]}`. Not one call was malformed: the arguments were ISO times
  with `-07:00`, `timezone: America/Vancouver`, `top` and `select`; the one window that held an
  event (2026-10-10 to 10-17) returned his Reliable Controls interview with its Outlook link.
  Ten of the twelve empty answers precede 2026-10-06, when the first reminder for that interview
  was set, and the other two asked about 2026-10-10 alone, so no call is shown to have missed an
  event. He has one Microsoft account, whose calendars are Calendar, Holidays and Birthdays; a
  calendar kept elsewhere is not read at all. A reply of "clear" cannot be told from a
  mis-asked read, and nothing in the answer says which calendar or when it was read.
- The model works out the bounds itself, in shapes that vary from call to call (`select` as a
  list or a string, `top` from 10 to 30). A day's bounds are local midnights with the offset
  that midnight has: a 25-hour or 23-hour day, or an event at 23:30, is wrong by an hour or a day
  whenever the offset is guessed. Graph answers calendarView in UTC and keeps To Do due values
  as dates at midnight (ADR 0036).
- ADR 0127 put the three reads on the menu to save the `tool_search` round and the probing calls
  (`list-accounts`, `list-calendars`) that followed it; mail did the same in ADR 0242.
- "What's on" is also what is due and what will ring: his open To Do tasks, the reminders waiting
  (ADR 0245) and the bus he pinned (ADR 0204). Today they are read with three other tools or not
  at all. The home already reads the calendar and tasks without a model (ADR 0055), about 1.2 s
  for all three.

## Decision

What is on for him is one read-only built-in tool, `my_schedule`, on the menu from the first
request: no arguments give the rest of today and tomorrow, `date` a day (today, tomorrow, a
weekday, an ISO date) and `to` the last day of a span of at most 14 days, `query` the items
whose title or place hold every word, over the next 60 days, and `page=2` the next eight. Each
call reads the sources just now and merges them in time order: calendar events through the
Microsoft server's `get-calendar-view` with the local days' own bounds, open To Do tasks due in
the window, waiting reminders and the pinned bus, each item with a spoken local time ("in 45
min", "tomorrow (Tue)"), what, where and a link for an online meeting, and its source. A source
that could not be read is named in the answer with what to tell him, and an empty answer says
what was read. The three Microsoft reads leave `always_loaded` and stay behind `tool_search`,
which points to the tool (`Tool.supersedes`, ADR 0242); the server's writes and its other reads
are as before.

## Alternatives rejected

- **Keep the three reads and rewrite their descriptions.** The exam's no-call answers came with
  all three loaded and described, and a described tool still leaves the bounds, the offset and
  the all-day dates to the model; the tool does that arithmetic once and the three cost 10,581
  characters on every request.
- **A `calendar` tool beside the Microsoft To Do reads.** A day's answer is events, tasks, reminders
  and a bus together; two entries put a choice before any result, and a "clear" for one source
  reads as a "clear" for the day.
- **Graph's `$filter=contains(subject, ...)` for `query`.** A phrase is not a substring: "Reliable
  Controls interview" is not in "Interview: Reliable Controls", while every word is; the words are
  matched here, over title and place, across all four sources.
- **The home's cached calendar read (ADR 0230).** It holds a read for minutes, so "read just now"
  would be false, and it keeps neither place nor link.
- **Held cursors for `page`.** Graph's own paging is followed by the server (`fetchAllPages`);
  numbering the merged list needs no state to lose at a restart, at the price of reading again
  for page 2.
- **Treat an empty read as "clear".** The 13 logged answers were empty and the model could not
  tell the honest ones apart; the answer names the sources it read.

## Consequences

- Only the default Outlook calendar is read (`/me/calendarView`): Holidays, Birthdays and any
  calendar kept in Google or on the phone are not, and the answer says "calendar", not "all your
  calendars". A calendar elsewhere is the next decision, not this one.
- Every call is live: the calendar and the To Do lists run on two threads, one read for the lists
  and one per list; a second page reads them again.
- A To Do task appears only on its due date, which Graph keeps as a date; one without a date, or
  overdue, shows only when its own day is asked for.
- The event log, its reminders and the pin are read on the calling thread; a pin's ring reminder
  is listed once, as the bus.
- A day's bounds are the configured zone's rules in the host's tzdata. On 2026-10-11 the Mac's
  2026c has Vancouver on UTC-7 all year while the Pi's 2026a falls back on 2026-11-01, a 25-hour
  day, so the two hosts bound that day differently; the tool follows the host it runs on.
- An all-day event is placed by its dates, not by the instants Graph overlapped: Graph returns
  tomorrow's all-day event for a window that ends at midnight, and the tool drops it.
- A settings file that sets `tools.mcp.always_loaded` itself keeps the reads on the menu beside
  the tool. `list-calendars`, `list-calendar-events` and the server's other reads still load behind
  `tool_search`, and the home's Today block, the day line and the daily report keep their own reads.
