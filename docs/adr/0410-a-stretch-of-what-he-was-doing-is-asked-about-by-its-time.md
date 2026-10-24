# ADR 0410 — A stretch of what he was doing is asked about by its time

**Status:** Accepted
**Date:** 2026-10-24
**Supersedes:** none

## Context

- The new phone day page (thread 手机端 UI 落地) lets him tap 问她 on a stretch of 在干嘛, the
  doing lane of `GET /inherent/day` (ADR 0302). `about` (ADR 0214) names one item by kind and id,
  and ADR 0298 adds that item's record, found by id in the folded day. A stretch is not an item
  of the day line, so `doing` was not an about kind and nothing found it.
- A stretch's id is `doing-<start_ms>`. The lane is cut again as the day goes on: a new fact, a
  stored run of the doing writer, or his answer moves the edges. So the id the phone drew a
  minute ago can name nothing now, or a stretch that starts elsewhere.
- What the page shows for a stretch is its words (title, detail, category, basis, confidence,
  whether he confirmed it), where his attention went in it (ADR 0392), and the items it holds.
  She needs the same to answer "这段我在干嘛".
- An answer links to the item his phone has open (ADR 0391), by the id the page knows.
- The page's star of a day (its highlights, ADR 0235) carried only id, label and who chose it;
  the phone has to draw it at its time on the timeline, and the item can be on the day before's
  part of the page.

## Decision

`about` takes kind `doing` with `start_ms` and an `end_ms` after it; no other kind may carry
`end_ms`. The brain finds the stretch by that window, not by its id.

- The runtime folds the day `start_ms` falls on as `GET /inherent/day` does (its lane holds the
  day before's stretches past midnight too, ADR 0313), and writes one line after the about line:
  the window, what the page now says about the stretch that shares most of it (with that
  stretch's own times), where his screens had him in the window (at most five threads), and the
  record of every item the window touches. Items go in whole while they fit 1800 characters; the
  rest are counted. A window nothing touches says so.
- Her answer's link for it is the page's stretch that shares most of the window, by its id now;
  none when no stretch overlaps.
- Each highlight of `GET /inherent/day` (and of the highlight route's answer) carries its item's
  `start_ms`.

## Alternatives rejected

- **Find the stretch by its id.** The id is its start, which moves each time the lane is cut
  again; a 问她 on a page drawn before a re-cut would reach her with no record.
- **Store the doing lane's cut so ids stay.** The lane is served from the facts each time
  (ADR 0302); a stored cut is a second copy that goes stale.
- **The phone sends the stretch's words and items.** The phone's copy can be stale and is a
  claim; the brain holds what the page was drawn from.
- **Keep the 900-character cap of ADR 0298.** One item's record can take all of it, and a
  stretch holds several.

## Consequences

- A turn with a `doing` about folds the day, the doing lane and his attention for it, once for
  her line and once for her links: a little more than a day-line item's one fold.
- Her line follows the page as it is now: a stretch re-cut or re-worded since he tapped it reaches
  her as the page shows it now, with the window he tapped.
- Clients that read highlights get one more field; they keep what they do not know.
