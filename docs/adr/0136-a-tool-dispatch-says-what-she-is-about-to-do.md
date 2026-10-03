# ADR 0136 — A tool dispatch says what she is about to do

**Status:** Accepted
**Date:** 2026-10-02
**Supersedes:** none

## Context

- ADR 0121 rejected "Speak at every tool dispatch" because its 4.0 s clock
  covered the tool turns whose answer was late. The clock is now 8.0 s (same
  ADR, 2026-10-02: the line was said too often), so a voice turn that calls an
  ordinary tool hears nothing for about 5 s after the tool starts, and then a
  generic 「等一下。」 about 1 s before the answer.
- Measured on the owner's log since 2026-09-24, from the first
  `action.proposed` to the answer's playback: tool_search p50 6.0 s (p10
  2.3 s), web_search p50 6.0 s (p10 3.7 s), search_records p50 8.0 s,
  screen_look p50 9.2 s, refresh_work_state p50 14.7 s. The first tool is
  proposed a median 2.5 to 3.1 s after his words.
- Other tools answer at once or speak for themselves: get_current_time and
  `mcp__hue__*` about 0 s after the tool; ask_user, start_night_run,
  end_night_run and create_memo about 2 s; a tool that needs a confirmation
  asks aloud.
- ADR 0121's rules still hold: the 1.5 s floor after his words, one first line
  per turn, the answer-started, turn-ended and pending-confirmation checks, the
  model's `lead_in` in place of the first line, and the media owner dropping a
  line the answer overtakes.

## Decision

The `action.dispatched` of a tool on a spoken turn says a short line of what
the tool is about to do, except for the tools whose answer follows at once or
that speak for themselves, which stay silent: the first line of the turn, so
the 8.0 s clock then says nothing, and a tool on `LONG_WAIT_TOOLS` still says
ADR 0117's line. The line names the kind of work (web, screen, records, mail,
calendar, or "check" for the rest) and never a result.

## Alternatives rejected

- **Keep the dispatch silent below the 8.0 s clock.** The answer after
  web_search or tool_search arrives at a median 6.0 s, before the clock, yet
  those turns are silent for the 3 to 5 s between the tool and the answer; the
  clock fires on the turns where the answer is near.
- **Speak at every dispatch, quiet tools too.** get_current_time and the lights
  answer about 0 s after the tool, and ask_user and a confirmation speak for
  themselves; a line there is heard on top of the answer, which is what the
  1.5 s floor and ADR 0121 exist to prevent.
- **One generic line for every tool.** 「我查一下。」 before a screen look or an
  email read says less than the kind of work, and a spoken turn that hears the
  same line each time is the repeated-preamble shape ADR 0116 avoids.

## Consequences

- A tool turn answered between the 1.5 s floor and the 8.0 s clock now says one
  short line it did not before; a tool that finishes within the floor still says
  nothing.
- The quiet list and the line a tool says are hand-kept names in
  `jarvis/decision/commentary.py`; a new tool says the generic line until it is
  listed, and a new confirmation tool is quiet by joining the table of what a
  confirmed tool does (ADR 0062).
