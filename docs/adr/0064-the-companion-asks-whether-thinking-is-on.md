# ADR 0064 — The companion asks whether thinking is on

**Status:** Superseded-by-0108
**Date:** 2026-09-26
**Supersedes:** none

## Context

- ADR 0061 turns thinking on from Allen's own words and stores nothing: each
  turn reads the mode back from the event log, and ten quiet minutes end it
  with no event at all.
- The companion showed nothing of it. Allen, 2026-09-26, on the third look
  lab: "算是这样吧", with the shooting stars and every moving star taken out,
  "做进去吧". The lab's defaults stand: the deep violet, the far-looking
  face, the whole Conversation page, her eyes as the mark while the mode
  waits.
- On the v1 wire the `open` op leaves only once the whole answer exists
  (`realtime.response.routine_streaming` is off), so nothing on the socket
  marks the start of thinking. The daemon drops a silent turn by reading the
  `open` header, so an op sent earlier from `response.started` would reach
  the companion before that check.

## Decision

- `GET /inherent/think` answers `{on, on_words, off_words}` from the same read
  a turn uses. The companion reads it at start, every 30 seconds, and again
  as soon as Allen's words go in or an answer opens.
- The companion times a deep answer itself, from his words going in (the
  submit answer or the voice `accepted`) to that answer's `open`, and keeps
  the seconds in its own memory.
- While the mode is on, nothing on screen moves that did not move before:
  a deeper colour and a stiller face, no stars or meteors.

## Alternatives rejected

- **A pushed `think` op.** The mode also ends by ten minutes of silence, and
  no event marks that moment, so something would have to push without an
  event: a daemon timer, the stored state ADR 0061 rejected.
- **A start op from `response.started`.** `_drop_for_silent_channel` knows a
  turn is silent only from its `open` header, so a `queue_review` or
  `silent_log` turn would show a thinking face for an answer that never
  comes.
- **The seconds stored with the answer's memory.db row.** The conversation
  record would carry a display value no model reads, to survive a companion
  reload that costs only the old answers' seconds.

## Consequences

- The ten-minute end shows on screen up to 30 seconds late.
- A reloaded companion shows no seconds on answers it did not see open.
- The seconds count the whole wait, tool calls included, not only reasoning.
- The companion compiles the daemon's Python patterns as JavaScript. One it
  cannot read leaves only the typing preview blind; the daemon still decides.
