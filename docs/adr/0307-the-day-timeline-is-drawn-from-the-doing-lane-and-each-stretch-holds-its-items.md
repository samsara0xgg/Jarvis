# ADR 0307 — The day timeline is drawn from the doing lane, and each stretch holds its items

**Status:** Accepted
**Date:** 2026-10-20
**Supersedes:** none

## Context

- The phone's day timeline drew one row per day-line item (ADR 0199). 2026-10-20 his talks with
  her filled the list. yilun (thread 第二层：怎么整理, 03:07Z): 「我的时间线本来的初衷，就是每一段分段显示在干什么」.
  02:47Z: the list is for things of high information density, and talks with her are not
  drawn at all.
- The doing lane (ADR 0302) tiles his waking day in stretches of what he was doing. Each stretch
  already had `item_ids`, but only the lasting facts it covered. Pictures, payments, events,
  reminders, departures, Claude chats, away notes, Claude Code sessions, and ADR 0305's media,
  messages and earbuds were in no stretch.
- To nest the rest, a client would have to cut the day by time itself. That repeats the brain's
  rules for open-ended items, moments and whole-day notes in Swift and in TypeScript, and the
  copies drift.
- Thread 一天时间线界面, `/mnt/project-files/时间线分层约定.md` (2026-10-20), agreed with
  yilun at 03:15Z: each row of the list is a stretch of the doing lane. The items a stretch holds
  show only when the row is opened.

## Decision

Each served stretch of the doing lane names, in `item_ids`, every item of the day that falls in
its time, sorted by start. Clients nest items by that list and never assign them by time.

- **Lasting items** are held by every stretch they overlap. An item still going reaches the end
  of the waking window.
- **Moments** are held by the stretch they fall in. A reminder is a moment at its due time.
- **Whole-day notes** (an all-day `label`, a `claude_day` letter) are held by no stretch.
- **Talks with her** are still held. Whether to draw them is the client's call.
- **Planned stretches** hold nothing.
- **Fact spans** keep `item_ids` = `[own id]`.

## Alternatives rejected

- **A `doing: [span id]` field on each item** — the thread first asked for this. It says the same
  thing in the other direction. It would also change `items`, which ADR 0302 keeps unchanged by
  the doing lane. The list on the stretch already existed; nothing read it, so widening it broke no
  reader (grep of `item_ids` across `jarvis/`, `ios/` and `desktop/`, 2026-10-20).
- **Clients assign items by time** — that is three copies of one rule. The open-ended-item rule
  alone differs by kind (a stay without an end goes on; a reminder without an end is a moment).

## Consequences

- `item_ids` on a doing stretch no longer means "the facts that name it". Code that wants those
  reads the segment's own `item_ids` (`doing.Segment`), as the writer's brief does.
- A stay that lasts all day is listed under every stretch. The client clips its length to the row.
- A new item kind that has no end because it is a moment must join `_MOMENTS`. Otherwise it reads
  as still going.
