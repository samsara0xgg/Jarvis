# ADR 0154 — The Memory Page's Edits Are Versions, and What the User Touches Is Pinned

**Status:** Accepted
**Date:** 2026-10-03
**Supersedes:** none

## Context

- Core memory (ADR 0146) is trusted by every turn and rewritten every night by a model
  whose changes are gated by citations and Jev, never by the user. The only fix for a
  wrong line so far was two hand-written versions on 2026-10-03 that name items by their
  number and give their reason as prose.
- The Dashboard's memory page edits, moves, deletes, confirms and takes back changes. The
  table stays append-only, so each action is a row. An edit the night rewrites the next
  morning is worse than no edit.
- An item number is its place in one version: deleting item 3 makes item 4 the new item 3,
  so an action cannot name an item by number.
- Day summaries (ADR 0142) are append-only with the newest row current, and the nightly job
  writes only days that have no row at all.

## Decision

Every user action on core memory is one new version of origin `user` through the same
transaction as `remember` and the nightly pass; items carry an id that every write stamps
(an item from before ids is named by a hash of its text until then). An item the user edited
or moved is pinned: the nightly gate drops a rewrite of it and logs the drop, and stores a
stale of it as a `suggest_stale` note beside the version's changes, not applied, for the page
to show as a reminder; the consolidation prompt says pinned items are fixed. Confirming (对)
is a version with an unchanged document and does not pin. Undoing (撤回) a version appends a
version that restores what it changed, and is refused with the item named when any of those
items is not as that version left it; undoing a night does not make the night run that day
again. A day-summary edit appends a row with model `user`, which the night never rewrites. The
cap on the note is a Settings key, 1000 to 20000 characters, read at boot like the others;
a user write that would pass the cap is refused unless it makes the note shorter.

## Alternatives rejected

- **Edit rows in place** — the 改动 screen and undo need the previous text, and ADR 0146
  keeps old versions for exactly that.
- **Pin by item number** — on the live store of 2026-10-03 the two hand-written versions
  already shifted every later number; a pin would land on a neighbour after the next delete.
- **Confirming also pins** — an item the user agreed with on its first night would never take
  a later day's update, and a project line is rewritten as the project moves.
- **Merge an undo over later versions** — a later edit of the same item makes any merge a
  guess about what the user meant; the refusal names the item and the user takes that back
  first.

## Consequences

- A pinned item that stops being true is only a reminder: nothing removes it until the user
  does, so pins accrue and the 4000 character note fills with lines the night cannot trim.
- The prompt text in `config/jarvis.yaml` still says "under 4000 characters" while the gate
  uses the cap; a raised cap is obeyed by the gate and not by the prompt.
- The two hand-written versions of 2026-10-03 renamed one item without an id, so they show
  as a delete plus an add; undoing them still works.
- A saved cap applies at the next boot; the page shows both values until then.
- The page lists the latest 60 versions; older rows stay in the table.
