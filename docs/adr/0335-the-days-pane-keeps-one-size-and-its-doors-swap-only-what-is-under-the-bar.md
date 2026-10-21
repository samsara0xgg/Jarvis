# ADR 0335 — The day's pane keeps one size and its doors swap only what is under the bar

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** none

## Context

- 2026-10-21, Allen: the day's pane (ADR 0308) is the whole of Jarvis on the Mac; the Dashboard's
  tools should sit one level under it, and switching to them must feel comfortable. A tool expanding
  inside the day's list felt wrong (06:17Z). A card rising over the list was rejected, as a look and
  as an interaction (07:43Z). Swapping the whole pane read clearest, but "if Jarvis keeps changing
  size it lags; ideally its size changes rarely" (08:10Z). At 08:24Z he asked for all of it to be
  built.
- What a size change costs here: the pane's height follows its content (`.notch .dt`
  `max-height: 584px`); every change springs the black shape in Notch.tsx and makes fitWindow.ts
  re-measure the page on every frame; growing past the window resizes the macOS window itself
  (in 200 pt steps), which is where a frame drops; shrinking waits 600 ms. In the pane rendered
  with its test day, the mail page drawn as a whole-pane swap was 435 pt tall against the day's 638.
- ← and → already change the day in the pane, and ↑ ↓ walk its rows; a row opens as a page that
  slides in.
- The bar already works as a time axis for more than one thing: the day's chips light their kind on
  it (`is-lens`), a pointer on a row brackets the row's time, a click on it jumps the list, a drag cuts
  the list to a range.
- The stage is 640 pt wide and the pane 520 pt (ADR 0308). Amber means "waiting on him" (ADR 0187);
  the day's own bar has none (ADR 0308).

## Decision

The day's pane has one fixed height and a row of doors on its bottom edge (today, mail, to-dos,
Agents, usage, more), and a door swaps only the line under the date, the numbers row and the rows,
while the head, the bar and the doors stay where they are.

- The pane changes size only when it opens and when it folds: not on a door, a day, new rows or a
  card.
- The bar stays the same day on every door; what he was doing dims, and the door's own moments are
  lit on it (a letter's arrival, a session's run, a usage window's level); on a door a mark is amber
  exactly when that thing waits on him, and the day's own pieces stay as ADR 0308 draws them.
- A pointer on a row, a click on the bar and a drag on it work on a door as they do on the day.
- A door changes by a fade, never a slide; Esc goes back to the day before the pane folds.
- The tools with no time of their own (memory, projects, plugins, settings, the conversation, the
  brief, job mail) sit behind the "more" door and open as a row's page does.
- The Dashboard stays reachable as before until Allen decides it goes.

## Alternatives rejected

- **The whole pane swaps to the tool** — the test day's mail page was 435 pt against the day's 638,
  so every switch is a resize of the shape and possibly of the window, the cost measured above.
- **A card that rises over the list** — rejected by Allen on 2026-10-21 07:43Z, as a look and as an
  interaction.
- **A column that comes out beside the pane** — 520 pt plus a 340 pt column is 860 pt on a 640 pt
  stage, so the stage and the window would have to widen each time it opens.
- **Doors that slide sideways** — ← → already change the day, so a sideways move would mean two
  different things.
- **No bar on a door's page** — the bar takes 61 pt of the 584; dropping it moves the rows up by 61 pt
  on every switch, and a door's rows lose the one axis that shows when each thing happened against
  what he was doing.

## Consequences

- A door with few rows leaves an empty band above the doors, and so does a morning with three rows:
  the pane is as tall as a full day all the time.
- Until the Dashboard is removed, two surfaces reach the same tools. A tool opened in the pane is
  the Dashboard's own page drawn alone (a second instance of it); what each instance keeps for
  itself (the home's hidden cards, the brief marked read) is not shared between the two.
- Every door's data is read while the pane is open, so the doors can show their counts.
