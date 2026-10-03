# ADR 0140 — A predicted tool group says its wait line at once

**Status:** Accepted
**Date:** 2026-10-03
**Supersedes:** none

## Context

- ADR 0136 says a line of what the tool is about to do at `action.dispatched`; the first tool
  of a turn is proposed 2.3 to 6.9 s after his words (calendar 6.9 s, mail 5.2 s, web 3.1 s,
  `~/.jarvis/experiments/jev-tool-predict-2026-10-03`), and ADR 0121's clock waits 8.0 s. The
  turn is silent until the tool call.
- The same request as ADR 0139 can also ask which kind of tool the answer needs first (11
  groups and none). On 462 scored voice turns at 0.95: 32 predictions, 78% the right group,
  88% some tool; 0 write turns predicted as a read group; recall 24%. Per group at 0.90:
  calendar 6 of 6 turns found, 67% precision; mail 100% precision, 33% recall; web 91%
  precision, 34% recall; records, agents and memory groups 0%. Jev answers in about 131 ms,
  inside every group's first tool call.
- ADR 0127 already skips `tool_search` for calendar, to-do and mail reads; after it only
  writes, other plugins and the lights still need it (4 turns at 0.95 in the window).
- A prefetched result would only help if the model then called the tool with matching
  arguments. The owner's log: of 9 `get-calendar-view` calls only 2 ask a window inside today
  plus 7 days, 3 of 11 `gmail_search` calls are `in:inbox`, and all 79 `web_search` queries are
  the model's own differing rewrites; `list-todo-tasks` needs a list id the model fetches first.

## Decision

With `realtime.jev_oneshot.tool_line.enabled`, a tool group in `tool_line.groups`
(calendar_todo_read, mail_read, web_search, comms_write) at `tool_line.at` (0.95) or above
is written as a `route.tool_predicted` event once the turn exists, and the commentary watcher
says that kind of work's line (ADR 0136's calendar, mail and web lines; the generic line for a
draft or an event write) at once, as the turn's first line. The per-turn line count of ADR
0121 keeps the dispatch line and the 8.0 s clock quiet after it; a line the dispatch already
said keeps this one quiet; the answer-started, turn-ended and pending-confirmation checks are
unchanged. For every tool the model proposes on a voice line, the dataset notes the predicted
group and its confidence next to the tool and its arguments, so a week of data can decide
whether a prefetch would pay.

## Alternatives rejected

- **Prefetch read-only data for the model.** The arguments are the model's: 2 of 9 calendar
  windows and 3 of 11 mail searches match what a prefetch could know, and none of 79 searches;
  the code (a handler wrapper with window filtering) does not pay for that hit rate. Revisit
  from the dataset.
- **Preload deferred tools for the predicted group.** At 0.95 it skips `tool_search` on 4 turns
  of the window and puts a 72-111k-token uncached request first.
- **Say a line for records, agents or memory groups.** 0% precision; write groups other than
  comms_write are quiet at dispatch by ADR 0136 because they ask or answer at once.
- **Keep the model's own lead-in as the first line.** It arrives with the tool call, seconds
  after this line would have been heard.

## Consequences

- A turn whose prediction was spoken loses the model's own lead-in: the first line is the
  fixed one.
- A wrong prediction (22% of the lines at 0.95) says "let me look" before an answer that needed
  no tool; the line claims no result.
- The line is spoken before the model has asked for anything, so a group Jev reads from a
  garbled fragment is spoken for it; the dataset has the raw answer.
