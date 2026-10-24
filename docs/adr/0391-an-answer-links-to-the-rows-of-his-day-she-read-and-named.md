# ADR 0391 — An answer links to the rows of his day she read and named

**Status:** Accepted
**Date:** 2026-10-24
**Supersedes:** none

## Context

- The phone's new pages (cross-device UI standard, built in the 手机端 UI 落地 work) show her
  words as a card on 今天 and 地图 and as one continuous chat. An answer about his day ("you were
  at the library all afternoon") cannot point at the row it means: her answer carries times
  (ADR 0215) and places (ADR 0283), never a row of the day page or a trip of its map.
- The phone side agreed one shape on 2026-10-24: at most three links per answer, each
  `{kind: row | outing, id, date, start_ms, end_ms, title}` with a title of at most 20
  characters. A `row` is any id `GET /inherent/day` serves, an item or a span (`doing-<ms>`, a
  stay); an `outing` is a trip of `GET /inherent/footprint`. The times are always filled, so the
  phone can fall back to the row covering `start_ms` when an id has changed (a doing span is
  re-cut). In chat a row link opens 今天 at that date and that row, an outing opens 地图 on the
  trip; on 今天 and 地图 her card's links only light rows. Allen said 「动手」 on this work on
  2026-10-24 00:46Z.
- Her day reads give no ids: `my_day` answers with figures (time at the computer, top projects,
  calls, places), and the item his phone has open reaches her as the about line with its id
  (ADR 0214, 0298). The only rows she could name today are ones she was never shown.
- Every answer already opens with a claims block (ADR 0320) that is never spoken and that the
  program reads; its lines name what the answer rests on.

## Decision

`my_day` for one day lists the day's rows with their ids, she names the rows her answer is
about in its claims block, and the program turns the names into links, keeping only rows she was
shown this turn.

- **The rows.** The runtime folds a day's rows as the two routes fold them, from the local
  stores: the doing lane's stretches (those nothing speaks to are left out), stays, sleep and
  workouts, calls, the day's events, and the outings between his long stays; a move is left out,
  its outing stands for it. At most 20, the longest kept, by start. `my_day` for today or a past
  day (not a span, not `about`) puts them on its answer as `timeline` lines
  (`9:00 am-10:00 am stay: Corner Cafe (id stay-cafe)`) and logs the ones that fit the answer as
  `day.rows_read` for the turn. A store that cannot be read leaves the day without rows, never
  without its answer.
- **Her naming.** A new claims line, `show <row id>: <a few words naming it, in his language>`,
  one per row, at most three, the ones the answer is most about. It claims nothing, so it is not
  judged against receipts and never sends an answer back; `answer.claims_checked` carries the
  rows it names as `show`. A row id keeps its case.
- **The links.** When the answer is rendered, the last checked reply's `show` rows become
  `links` on `surface.response_emitted`, in her order: a row is kept only when `my_day` showed
  it this turn or it is the item his phone has open (looked up only then); anything else is
  dropped without a word. The title is her words, else the row's own, cut to 20 characters. An
  answer that was sent back and not replaced has no links. The phone's socket relays the row as
  it is, and `GET /inherent/conversation` puts `links` on her row as it puts `places`.

## Alternatives rejected

- **Name a row by day and time, as `mark_highlight` does** — a time is covered by a stretch, a
  stay and a call at once, so the program would have to guess which one she meant; an id she
  was shown is the row she read.
- **Link whatever id she writes** — an id she never read is a guess the phone would open as a
  fact; she was shown the rows, so the program can hold her to them.
- **A tool of its own for the day's rows** — one more call before most answers about the day,
  while `my_day` is the call she already makes for them.
- **Let the program pick links from what the turn read** — it cannot tell which of twenty rows
  the answer is about; she wrote the answer.

## Consequences

- One day's `my_day` answer grows by up to 20 short lines, cut first when it would pass 4000
  characters.
- A link to a doing stretch can outlive its id when the day is re-cut; the phone opens the row
  covering its start.
- Links depend on her writing `show` lines; how often she does is seen in
  `answer.claims_checked` rows and tuned in the prompt.
- `contracts/answer/links.json` holds the links as the phone reads them.
