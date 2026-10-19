# ADR 0306 — Her one tool for his things is `schedule`, and `keep` keeps only facts

**Status:** Accepted
**Date:** 2026-10-19
**Supersedes:** none

## Context

- ADR 0299 makes his things one ledger and says her one tool reads and writes it. ADR 0290 asks
  any new or rebuilt tool for its need, three of his own phrasings, why the per-turn state and
  the other tools cannot answer, and where it sits.
- His phrasings, Pi log: 2026-10-17 「明天下午和朋友打羽毛球」 (a thing at a time with someone);
  2026-10-17 「今天不吃 aw，11 点去 Fujiya」 (a plan for today that replaces another); 2026-10-18
  「下午要干什么不记得了吗」 (what is on). 2026-10-18 09:25 he said the invoice was paid; nothing
  could mark it.
- Before this, the same need took four tools: `keep` wrote memos, reminders and appointments,
  `kept` read them back, `my_schedule` read the calendar, To Do, reminders and appointments but
  wrote nothing, and the Outlook plugin's tools sat behind tool search. Two tools answered "what
  reminders do I have", and nothing could move a thing, say it was done or paid, or show the
  memos to the draft (doc 「Jarvis 工具清单与结构」, 2026-10-16).
- The state block carries only the next one or two things and the count of unconfirmed ones; a
  day, a span, a search, the untimed things and every write need a call.
- Facts about him are another object (ADR 0290: one tool per object); `keep(kind=fact)` already
  writes them to core memory.
- ADR 0299 already retired the three stores and `my_schedule`; this decides the tool that takes
  their place, as ADR 0290 asks of every tool.

## Decision

`schedule` is her one tool for anything he has to go to or do, resident on the menu: it reads the
ledger (a day, a span, a search, the open untimed things, what is overdue) and adds, moves, marks
done or paid, cancels, confirms and reopens things in it, through the ledger's write path with
`by` her. `keep` and `kept` keep only lasting facts about him.

- One `action` enum carries the verbs; a thing is named by its short id, which every read and
  write returns.
- She gives times as his local time; the tool resolves them in his zone and refuses a past one.
- A thing with a time rings by default the way its kind did before: a thing to do at a time rings
  at it, one to go to at a time rings `commitments.remind_before_min` before, one to go to on a
  day rings the evening before; a due or untimed thing does not ring. She can name other rings or
  none.
- The old names point to it in tool search, and `/note` and `/notelist` go through it.

## Alternatives rejected

- **Keep `keep` for writes and `my_schedule` for reads** — two tools answered "what reminders do
  I have", and neither could move a thing or close it as done or paid.
- **One tool for things and facts** — a fact has no time, state or ring; the 11-parameter `keep`
  was the result of mixing them.
- **A separate tool per verb** — the standard puts the verbs of one object in one enum, and every
  verb needs the same id and time handling.

## Consequences

- Her exam needs new cases for the merged tool; old cases that expect `my_schedule` or the
  reminder tools are read through aliases, and those that expect a memo or appointment through
  `keep` or `kept` must be rewritten, since a name alias cannot tell a fact from a thing.
- Writes to Outlook wait for ADR 0299's last step; until then a thing she adds is not in his
  Outlook calendar.
