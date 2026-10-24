# ADR 0384 — The phone's day page puts the stars back on a taller bar and slides its numbers

**Status:** Accepted
**Date:** 2026-10-23
**Supersedes:** the clauses of ADR 0376 that take stars and marks off the phone's page, keep only the chips that fit one row whole, and make the line for now a row of the list. Everything else in ADR 0376 stands.

## Context

- On 2026-10-23 Allen settled one layout for every screen (跨端UI标准 v0, the cloud thread
  「手机端 UI 落地」). For the phone's 今天 page it takes the fourth mock-up as the base and
  changes three things ADR 0376 decided two days earlier.
- Stars: at 19:46Z he asked for them back on the bar (「条上放星：他标的、她挑的」). The data never
  left: `GET /inherent/day` still serves `highlights[{id, label, by}]` with the day, and the
  phone already decodes it for the old dial. A highlight carries no time; its id is the id of a
  row's item or span, which does.
- The bar grows from 22 pt to 44 pt (「更高的那个可以显示出它的重点」), and a place line runs
  under it all day. A 13 pt star stands above the bar and takes no room from its blocks.
- The numbers under the bar: ADR 0376 kept only those that fit one row whole. With sleep, trips,
  exercise, the Mac, calls, talks with her, money and pictures, then steps, resting heart rate
  and HRV, a 400 pt row holds four or five; the rest were never seen. He chose one row that
  slides sideways, its right edge fading so it reads as more.
- 「现在」 as a row of its own took a row's height to say a time the bar and the rail already
  show. He chose a warm dot on the rail: solid above it, dashed below it.

## Decision

On the phone's day page, draw his marks and her picks as stars above the bar at the start of
the row each one names, show every number of the day in one sideways-sliding row of chips, and
mark now as a dot on the list's rail instead of a row.

- A star whose id names nothing on the page is not drawn; nothing is placed by guess.
- Under a chip's lens the stars dim with the blocks that are not that kind.
- The list opens resting on the dot, as it rested on the row.

Limits: no brain route or stored state changes. The phone does not set his marks; the Mac's
timeline still does. The iPad keeps its own layout (ADR 0376's iPad clause stands).

## Alternatives rejected

- **Keep ADR 0376's page without stars** — Allen asked for them back on 2026-10-23 at 19:46Z
  after seeing both; the bar at 44 pt has the room above it that the 22 pt bar lacked.
- **Chips wrapped onto a second row** — still rejected for ADR 0376's reason: each wrapped row
  takes 32 pt from the list at every scroll position.
- **Chips that fit, the rest behind a 「更多」 chip** — one more tap to reach a number, where a
  sideways slide reaches any of them in one gesture and the fade already says there are more.
- **A star placed at a time sent with the highlight** — the day's reply has no such field yet;
  joining by id uses what is served today and needs no brain change. When the brain adds
  `start_ms` to highlights, the join can read it.

## Consequences

- A highlight on an item the day does not show as a row (a merged or hidden item) has no star.
- Stars, the place line and the now line all sit on or near the bar: a finger scrubbing the bar
  reads blocks, not stars; a star is not touchable.
- With the now row gone, a test or scene that found now by its row finds it by the dot's place.
