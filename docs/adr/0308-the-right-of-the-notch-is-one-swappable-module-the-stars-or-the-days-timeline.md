# ADR 0308 — The right of the notch is one swappable module: the stars or the day's timeline

**Status:** Accepted
**Date:** 2026-10-19
**Supersedes:** none

## Context

- 2026-10-19, Allen: the right of the notch shows the agent marks and, after them, the pinned bus
  trip (ADR 0069, 0187, 0200); her place is the left. He wants the day (ADR 0199, 0235) one rest away
  from the island, and a rest or a click on the notch itself to stay the Dashboard's.
- The wing is 64 pt in the 点线环 look, and 87.5 pt remain beside the notch while the Dashboard is
  open (ADR 0200). Four marks take 69 pt; the line `家 · 3:00` measures 49 pt; with the turn mark
  alone, line and padding the wing is 78 pt.
- Two panes cannot hang at once: the stage is 640 pt wide, the sessions panel is 440 pt and the day
  needs about 520 pt (a bar label and a row's time range need the width).
- A rest on the wing already has two meanings (the pill shows its trip, a mark opens the sessions),
  and amber is the turn mark's colour, "waiting on you" (ADR 0187), while a day has a now.
- `GET /inherent/day` serves the day as items, not words. What the wing says is derived from them
  with the local clock, as the pin's countdown is from absolute times (ADR 0200).

## Decision

The right of the notch is one module chosen in Settings as `wing`, `marks` (today's stars, exactly as
before) or `timeline` (the default, also when nothing is stored), and the timeline module is the turn
mark alone, one line saying where he is, and the pinned trip, with the day dropping from the island
as the Dashboard comes.

- The line is the stay that covers now and how long it has lasted (`家 · 3:12`; `停留` for a place he
  never named), while he moves its words and minutes (`27 路 · 12 分`, `步行 · 6 分`, `开车 · 40 分`),
  else `今天`; the pill never opens the day, and the turn mark opens the sessions panel on a click,
  never on a rest.
- A rest on the line (the Dashboard's 300 ms and slow pointer; nothing when the Dashboard is set to
  open on a click) opens the day, unpinned; a click opens it pinned, and a click on a pinned day
  or Esc closes it; once the pointer has been in it, leaving the line and the pane closes it after
  the Dashboard's 600 ms unless a press that began in it is held.
- It never hangs with the sessions panel or the Dashboard: it replaces the first, folds the second,
  and is folded when the Dashboard opens; a click on the line while the Dashboard hangs hands over.
  Notices wait while it is open, and at `dnd` it keeps the day as it was (ADR 0153).
- A place has one hue (home warm, school blue, any other kept place one of four by FNV-1a of its id,
  as the phone does, an unnamed stay a neutral haze); now is warm white; amber appears nowhere in it.

## Alternatives rejected

- **The line after the four marks** — with four marks (69 pt), the line (49 pt) and padding the wing
  needs 133 pt against the 87.5 pt left with the Dashboard open, so the whole wing would fold
  (ADR 0187), taking the turn mark with it.
- **The day as a second pane beside the sessions panel** — 440 pt plus 520 pt is 960 pt on a 640 pt
  stage; one drop pane with two contents fits, and a swap between them is the list and page swap
  the sessions already make.
- **A rest anywhere on the wing opens the day** — the pill's rest already shows a trip and a mark's
  click already opens the sessions, so one pointer position would mean two things; the line has its
  own zone and a rest on the turn mark or the pill opens nothing new (checked in
  `scripts/verify-notch-timeline.mjs`).
- **The daemon serves the wing's words** — the minutes change every minute, so a route would be read
  60 times an hour for words the items already give; one read every 30 s and the local clock suffice.
- **Amber for the now line and tick** — the turn mark's amber is the only amber on the island, and a
  day with one amber thing in it would read as one waiting for him; the same script asserts no dot
  or bar piece is amber.

## Consequences

- Working, finished and parked sessions have no mark in the timeline wing, and with nothing on his
  turn the sessions panel has no way in from the wing (⌥Tab and the Dashboard's Agents block still
  do). Whoever works mostly with sessions picks Stars.
- While the setting is `timeline` the companion reads the day every 30 s and again at each opening of
  the pane; with Stars it reads nothing.
- Esc reaches the page only while the companion window has key focus, which it does not take on its
  own (as for the Dashboard); leaving the pane closes it either way.
- The place hues are the same rule as the phone's `PlaceTint`; changing one means changing both.
- A `kind` the notch does not know is left out of the pane, never guessed.
