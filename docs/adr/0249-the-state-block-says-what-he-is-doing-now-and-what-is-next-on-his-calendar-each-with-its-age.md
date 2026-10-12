# ADR 0249 — The state block says what he is doing now and what is next on his calendar, each with its age

**Status:** Accepted
**Date:** 2026-10-12
**Supersedes:** none

## Context

- A question about his present ("what am I doing", "what is next today") costs a tool call and a
  round the user waits through, when the program already holds the facts. The prompt tells the
  model not to call a tool for what the program's blocks already show.
- The state block of every turn already carries: the bus ride, with the next stop, his stop and
  whether the times are live (ADR 0246); the phone's last place with its age (ADR 0217); the
  Dashboard view (ADR 0148); and, in the ledger's "Today so far", every pending reminder soonest
  first (ADR 0201, the store of ADR 0245) and the day's activities from the latest work-state
  analysis. It does not carry the analysis's claim about the present, which `my_day()` reports as
  "Right now" (ADR 0244), and it has no calendar.
- On a brain nothing re-analyses in the background and `work_state.auto` is off by default, so the
  saved claim is as old as the last explicit refresh (ADR 0244). A line with no age is read as
  now; `my_day` already warns when the claim is over three hours old.
- A line costs its characters on every turn, and the state block is replayed in later turns as
  sent. The changing lines sit after the stable prefix, where the provider's cache stops
  (ADR 0148); nothing may be added ahead of it.
- The calendar tool is built apart from this decision. A turn must not wait on the network, so
  the block can only read what a reader already holds.

## Decision

The state block of every turn gets two more lines, each only when it has content, each at most
200 characters, after the lines it already has: `Doing now`, the latest work analysis's claim
about the present worded as `my_day` words it, with its age and, past three hours, `my_day`'s
warning that it may not describe the present, left out once it is over twelve hours old; and
`Next on his calendar`, the next two items still ahead, with how long ago the calendar was read,
taken from a reader the composition root injects (none by default, so no line). Reminders, the
ride and his place are not repeated.

## Alternatives rejected

- **A new "Right now" block that gathers the ride, the place, the reminders and these two.** The
  ride line alone may run to 520 characters and the pending reminders already sit in "Today so
  far"; copying them doubles the characters of every turn and leaves two lines per fact to keep
  equal, where one place to go stale is the fault this ADR is closing.
- **A "next reminder in N minutes" line.** The first entry of the pending list is already the next
  one, with its local date and time, and a reminder does not age. It would add a second line for
  a fact the block has.
- **Show the claim at any age.** On a brain it can be days old; a 200-character line saying
  "as of 4 days ago, may not describe the present" on every turn tells the model nothing a call to
  `my_day` would not, and that call says the same.
- **Show the claim without its age.** It is the one line here that is a past fact worded as the
  present; the saved analysis is as old as the last explicit refresh.
- **The block reads the calendar itself each turn.** It would hold the turn on the network. A
  reader that returns what it last fetched, with the time it fetched it, keeps the turn as fast as
  today; the line says how old the reading is.

## Consequences

- Two lines of up to 200 characters on turns where both have content; none where neither does.
  A later turn's replay of this block repeats them as they were, under the header that says the
  state is as it was when the words were said.
- Past twelve hours the claim leaves the block, and a question about it goes to `my_day`, which
  says how old it is. Nothing here refreshes the analysis.
- The reader is `CalendarCache` (`jarvis/runtime/now_block.py`), a daemon watcher beside the
  reminders and the rides: `my_schedule`'s `get-calendar-view` read (ADR 0248) over the rest of
  today and tomorrow, the next three timed items, at start, every 15 minutes and again 2 minutes
  after a read that failed. A turn only returns what it holds, so it adds no latency and makes no
  call. When Microsoft is not connected or a read fails the reading is dropped and the line is
  left out; it never says the calendar is clear. Its items' titles are his calendar's words,
  shown quoted on one line.
- An event added or moved after the last read shows at the next one, up to 15 minutes later, and
  nothing refreshes the reading when the model writes the calendar; the line's age says how old
  it is, and `my_schedule` reads just now. An all-day event is not offered as next.
- An item that has begun is never offered as next, so a reading an hour old can leave the line
  empty; an item in progress is not shown either, as the reader gives no end time.
- `my_day` and the block read the work state through one function, `work_state_now`, so the two
  cannot word the same analysis differently.
