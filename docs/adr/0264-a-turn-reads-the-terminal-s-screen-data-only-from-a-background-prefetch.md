# ADR 0264 — A turn reads the terminal's screen data only from a background prefetch

**Status:** Accepted
**Date:** 2026-10-15
**Supersedes:** none

## Context

- The per-turn blocks of the ledger (ADR 0201) sit at the head of the state block as the time line:
  "Today so far" reads his computer's TimeSink spans and the job-hunt block reads the submission
  pages seen on screen. A brain holds no TimeSink store (ADR 0170), so each is a `timesink_read`
  over the terminal link: `ledger_screen` and `ledger_submissions`.
- A read with nothing known waits for the Mac. The Pi log of 2026-10-14 shows 491 such reads that
  day, the longest 7027 ms; at 21:18 PT turn T70128764 waited 4.7 s between his words and the
  request to the model. A read for which an answer from the last half hour is kept returns it at
  once, but the kept answer is dropped after half an hour, and the standing text's rebuild holds
  the ledger's lock through its own reads, so a turn arriving in either gap waits.
- The other lines of the state block read in-memory snapshots or the Event Log, which are local
  and take milliseconds; `CalendarCache` (ADR 0249) already shows the pattern of a background
  reader that a turn only asks for its last reading and the reading's age.
- The terminal link offers requests and answers; its observer reports that the store's head
  moved, not what a turn needs.

## Decision

Once the daemon has started the ledger's background thread, a turn builds the ledger's text
without waiting on the terminal or on the 38-day standing text: it reads only what that thread
has kept, and leaves out the text it has nothing young enough for.

- The thread asks the terminal for the two reads of the per-turn blocks every 3 minutes, and
  keeps the answers; a turn is served a kept answer for up to 15 minutes, so five asks in a row
  may fail before the line goes.
- The standing text is checked every minute, as before, and a turn takes the last one built;
  before the first build it carries none.
- "Today so far" says when the screen data was read (`TimeSink as of HH:MM`) on its Computer
  line, and with no young enough reading it has no Computer, "Before that" or "This week" lines,
  where it used to say "no activity recorded yet today". The job-hunt block's count is "at least"
  by its own wording, so it counts without the screen pages when none is kept.
- A tool that needs the data now (`my_day`, the day line, the timeline) still reads the
  terminal and waits for it.

## Alternatives rejected

- **Refresh on the terminal's head-changed events.** They say the store moved, and while he works
  it moves at the observer's poll interval, so each event would ask for a window of spans that
  took up to 7 s to read, more often than the timer does; and a Mac that goes quiet sends none, so
  the timer is needed anyway.
- **Keep the stale-while-revalidate read and widen its age.** A turn still waits on the first read
  after a restart, a new day (the window's start moves) or a gap longer than the age.
- **A shorter timer (under a minute).** The 4.7 to 7 s reads are megabytes of spans from his
  Mac; asked every 30 s that is a continuous load on the machine he is working on, for a line
  whose figures are minutes of active time.
- **Leave the screen data out of the per-turn blocks.** The model answers "what have I been doing
  today" from "Today so far" without a tool call (ADR 0201); without it each such question is a
  tool round.

## Consequences

- The Computer line can be up to 18 minutes behind (a 3-minute ask, a 15-minute age), shown by
  its `as of` time; for the same reason "still on" in it may be false for a few minutes after he
  leaves.
- The first turns after a daemon start, and any turn while the Mac has been unreachable for 15
  minutes, go without the Computer lines and the screen part of the job-hunt count. The model
  gets no line saying so; the line is simply absent.
- When the day turns or a note lands, a turn may carry the previous standing text for up to a
  minute.
- The Mac is asked for the two reads every 3 minutes round the clock while the daemon runs, 960
  reads a day whether or not he talks, about twice the 491 of 2026-10-14.
