# ADR 0296 — He can mark a day's highlights himself, and a sixth mark is refused

**Status:** Accepted
**Date:** 2026-10-19
**Supersedes:** none

## Context

ADR 0235 let him change a day's highlights only by saying so: `mark_highlight`
is a model-only tool that takes a day and a clock time, because only the model
hears "that lunch was the best part". The day timeline on the Mac notch panel
and the iPhone 一天 page now shows each row, and on 2026-10-19 he asked to act
on a row directly, by tapping it.

Constraints:

- A tap names the item by its id. A clock time does not: the tool resolves a
  time to the shortest item that covers it, so a time inside a stay with a
  picture sent in it gives the picture, whichever row he tapped.
- Marks are rows of `day_highlight_marks`, the latest per item wins, and the
  day line merges them under her picks. Readers already treat a row the same
  whoever wrote it.
- A day has at most five highlights (`MAX_HIGHLIGHTS`, 0235), and the day
  line is built for five. The merge never cuts his marks to fit, so the tool
  can already leave a day with six or more.
- The app hides a control until its route answers, so the route must say
  clearly when it refuses.

## Decision

A paired device or the local key may mark or unmark an item of a day by its
id with `POST /inherent/day/highlight`, which writes the same mark row the
tool writes. His marks win over hers, as in 0235. When five of his marks stand
on the day, a sixth is refused with 409 and nothing is written or dropped;
unmarking works at any count.

- Marking an item already marked, or unmarking one that is not highlighted,
  writes nothing and answers 200.
- Marking without a label keeps the label his standing mark has.
- An id that is not an item of that day is 404, and a shape that is not a day,
  an id or a label of at most 24 characters is 422.
- The model keeps `mark_highlight` and its approved description unchanged.

## Alternatives rejected

- **A route that takes a day and a time, as the tool does** — on a day with a
  picture sent inside a stay, the stay's row and the picture's row cover the
  same minute and the tool always resolves to the picture, so tapping the stay
  would mark the picture.
- **Let a sixth mark push out her pick or his oldest mark** — dropping his
  oldest is a silent loss of something he chose; her picks already give way to
  his marks without help.
- **No cap on the route, as the tool has none** — each tap past five adds a
  highlight to the day payload, which 0235 says holds at most five; the tool's
  overflow takes a model's call, the route's would take a finger.

## Consequences

- The tool and the route differ at the cap: the model can mark a sixth item
  and the route then refuses another until he unmarks one.
- The count is of his marks on items still on the day line; a mark whose item
  has gone from the line does not count and is not shown.
- A press and a spoken mark can race; the later row wins, as for two spoken
  marks.
