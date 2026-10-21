# ADR 0376 — The phone's day timeline opens a row where it stands and keeps only what does work

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** none

## Context

- The phone's day page is a bar over a list of rows, drawn from the doing lane (ADR 0307). Until
  now a tapped row slid a page of its own over the list, with a way back and steps to the rows on
  either side. Each look at a stretch cost a tap there and a tap back, and while the page was up
  the rows before and after it were out of sight.
- The bar was drawn by time: 380 pt for a waking day of about 18 hours is 0.35 pt a minute, so a
  10-minute trip is 3.5 pt wide. A block's icon needs 16 pt (a 12 pt glyph and 2 pt each side),
  so the short stretches that make a day readable (a trip, a call, a stop) wore nothing.
- On 2026-10-21 Allen went through every element of the page asking what each is for
  (「每一个存在的元素都要思考他们的必要性」), approved the second round of the redesign and handed the
  timeline to it (「那就按现在这个版本 … 时间线以你为准」). The verdicts this rests on:
  - The stars of what mattered (his marks and her picks), the pins on the bar, the sun's tick, the
    label on the line for now and the bar's zoom each took room from the rows or the bar and
    answered no question he asks of his day.
  - Talks with her have their own page. Drawn again as rows of the day they doubled it, and he
    asked on 2026-10-20 that no talk be a row.
  - 让她总结 and the 问她 and 星标 buttons inside an opened row repeated the long press.
  - He wants to ask her about rows that have nothing more to show, not only about those that open.
- A stretch the doing writer is not sure of already carries the question it would ask (ADR 0302),
  and `POST /inherent/day/answer` already takes his choice or his words for a span id.
- On the bar one finger both taps and holds: a hold reads out what was going on under it.
- The iPad's list sits beside a stage that has room for an opened row, and he chose to keep it
  there (「放右边大区域」).

## Decision

On the phone, open a row of the day under itself in the list, only when it has more to show
than its line or something he can do there, and keep on the page only the elements below.

- **Rows.** A tap opens a row that has more to show (`TLEntry.expands`); a second tap closes it.
  A row with nothing more frames its time on the bar for 1.3 s instead. Every row but the line
  for now, a pause and a place's header answers a tap, and a small arrow at the right is drawn
  only on the rows that open.
- **An opened stretch** says one thing a line in time order (each run of his attention, what was
  alongside on his phone, each item, what took him off), the drift summed in one line, the
  question she would ask in one line with its options, and 依据 as a link. His answer goes to
  `/inherent/day/answer` by span id, one at a time; one that fails opens the row and says so.
- **A stretch she is not sure of** wears a "？" after its title.
- **Groups.** Two or more stretches in a row at one kept place sit under a header with the
  place's name and say only their own line.
- **问她** is in the long press of every row, a place's header included, that has an item the
  brain can be told about (ADR 0214), whether the row opens or not.
- **The bar** gives every block that has happened at least 17 pt and shares the rest by time.
  Blanks under 5 minutes and what is ahead get no floor. Where the floors would take nine tenths
  of the bar, it is drawn by time. Every reading of a minute from a point, or of a point from a
  minute, goes through that one axis.
- **A touch on the bar** that lifts within 0.18 s and 5 pt is a tap. Past now it goes back to
  now. On a block of the opened row it lights that row's line at that minute for 1.9 s. On the
  block of a row that opens it opens that row; on any other it scrolls to the row and frames it.
  A longer touch is a hold that reads out.
- **Frames on the bar**: the opened row's block, a time framed for a moment, and while the list
  scrolls the part of the day it shows, gone 1.1 s after the scrolling stops.
- **The chips** under the bar are only those that fit one row whole, in their order.
- **Gone from the phone's page**: stars and marks, pins, the sun's tick, the label on the line
  for now, the zoom, 让她总结, opening a talk from the day, and talk rows, also on a day with
  no doing lane.
- **On the iPad** the list does not unfold; an opened row shows on the stage at the right.

Limits: no brain route and no stored state change. Her highlights and the highlight route
(ADR 0296) stay for their other readers: the Mac's timeline, recall and the nightly passes.

## Alternatives rejected

- **A page per row, as before** — the page covers the list, so an opened stretch is on screen
  alone: setting it beside the stretch before it is a step away and a step back, two taps, where
  an opened row under itself keeps the rows above and below it in view at no tap.
- **A bar by time with the icon drawn only where it fits** — at 0.35 pt a minute, every stretch
  under 46 minutes is narrower than its icon, which on a day of trips and calls is most of the
  blocks that are not stays; they would need a hold to be read.
- **A floor for every piece, short blanks and what is ahead included** — at 0.35 pt a minute a
  blank of three minutes between two stretches, given 17 pt, takes as much bar as a stretch of 48
  minutes, for time in which nothing happened and that has no icon to show; the part of today
  still ahead has no icon either, and its planned events are boxes and ticks drawn over it.
- **Chips wrapped onto a second row** — each wrapped row takes its height from the list on every
  scroll position, for chips that are counts of what the list itself shows.
- **问她 as a button in an opened row** — only rows that open would have it; the long press
  reaches every row with something to put to her, including those that do not open.
- **The iPad's list opening rows in place too** — the stage at the right would stand empty
  while the list grew, on the device whose width is there to show both.

## Consequences

- A stretch inside which no item is of a kind the brain can be told about has no 问她: the
  about contract (`jarvis/shared/about.py`) names items, not spans. Asking about any stretch
  needs the brain to take a span as an about, which is a change of that contract.
- His own highlight marks can no longer be set from the phone; the Mac's timeline still sets
  them. Her picks are still served with the day and the phone draws none of them.
- The bar is no longer proportional: a block's width no longer says how long it was. The length
  is told by the row's right column and by the hold's readout.
- The phone and the iPad open a row in two ways from one view of what a row opens to, so a
  change to an opened row is checked in both places.
- An opened row pushes the rows under it down; one opened near the foot scrolls the list so its
  top stays in view.
