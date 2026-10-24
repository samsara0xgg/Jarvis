# ADR 0409 — His watch marks a moment as saying that time does

**Status:** Accepted
**Date:** 2026-10-24
**Supersedes:** none

## Context

- He marks what mattered on a day in two ways. Said aloud, `mark_highlight` (ADR 0235) takes a
  day and a clock time and marks the item that covers that minute: the shortest of several, so a
  picture sent inside a stay is the picture. It writes a `day_highlight_marks` row every time and
  has no cap. Tapped on a timeline row, `POST /inherent/day/highlight` (ADR 0296) marks that
  row's item by its id; it is idempotent and refuses a sixth of his marks with 409.
- The watch v1 card 标一下 (thread iPad 和手表版, 2026-10-24) is one tap: "this, now". The
  watch shows no timeline and holds no item ids. What it has is the moment of the tap.
- The watch reaches the brain only through the paired phone (ADR 0337), so a route it uses is
  guarded as every paired device's route is.
- The coordinator asked on 2026-10-24 that the tap do exactly what saying it to her does.

## Decision

`POST /inherent/day/mark` takes `{"day", "at_ms", "label"?}` and marks the item that covers the
minute `at_ms` falls in, through the same writer `mark_highlight` uses: the same item, the same
row, the same answer (with the item's id), and, like the tool, no cap.

- `at_ms` must be inside `day`; a moment nothing covers is 404, and nothing is guessed from the
  nearest item.
- A mark only: there is no unmark by moment. Taking a mark off stays with
  `POST /inherent/day/highlight` and the id this route answers with.
- His taps on rows and the watch's marks go through one writer and its lock in the daemon.

## Alternatives rejected

- **Have the watch call `POST /inherent/day/highlight`.** It needs an item id, which the watch
  would have to fetch with `GET /inherent/day` (the whole folded day, through the phone's
  relay) before every tap, then pick the covering item itself: a second copy of `covering` in
  Swift that can drift from the one the voice tool uses.
- **Send the tap as words to her (`POST /inherent/ask` "标一下").** A model turn per tap costs
  money and seconds, can pick another item or ask back, and puts a button press into his
  conversation.
- **Hold the route to the cap of five.** Saying it has no cap; the coordinator asked for the
  same semantics, and a refusal on a one-tap card has no row to unmark from.

## Consequences

- His marks by moment are not idempotent: tapping twice in the same stay writes two rows (the
  day line shows one highlight), as saying it twice does. The newest row's label is the one
  shown, so a tap without a label puts back her label (or kind and time) over one he said.
- A day can hold more than five of his marks again, through the watch as through her; the
  timeline's 409 still guards only taps on rows.
