# ADR 0208 — show_on_dashboard Can Open a Settings Category

**Status:** Accepted
**Date:** 2026-10-10
**Supersedes:** none

## Context

- ADR 0176 lets `show_on_dashboard(page, item_id?)` name only an item the current view
  carries: the open item or a row the page reported. Anything else opens the page alone.
- Settings reports its category in `tab` and reports no rows, so every `item_id` on page
  `settings` is dropped, and only the page opens.
- ADR 0196 and ADR 0202 put pairing a phone behind 「手机与设备」, the Settings category
  `devices`, where 「显示二维码」 mints the code. Allen says 「帮我连手机」 and the page
  opens at the category list, two taps short of what he asked for.
- The categories are a short fixed list in the companion, not data the page reports, and
  they do not change while the panel is open.

## Decision

On page `settings`, `item_id` may also name one of the fixed Settings category ids the
brain carries, and the companion opens that category; for `devices` it also starts the
pairing QR code, as if 「显示二维码」 had been pressed.

- The rule extends ADR 0176's: an id outside the view's items and the category list still
  opens the page alone.
- The `present` op for a category carries `kind: "category"`.
- A pairing code still showing is kept, because minting another voids it (ADR 0196).

## Alternatives rejected

- **Make Settings report its categories as rows** — the report keeps at most ten rows
  and Settings has twelve categories, so two would be cut; raising the cap makes every
  turn's state line longer for a list that never changes.
- **A separate `pair_phone` tool** — it would move the same screen as `show_on_dashboard`
  and need its own L0 registration and prompt text; every further category she should
  open (accounts, voice) would need a tool of its own.
- **Keep the list only in the companion and accept any id** — the brain could not tell
  the model which ids exist, so the model would guess; a wrong guess opens the category
  list, and the tool result cannot say so.

## Consequences

- The category ids live in two places, `SETTINGS_CATEGORIES` in `runtime/dashboard.py` and
  the `cats` array in `SettingsPage.tsx`; renaming one without the other opens the
  category list instead of the category. A test pins the brain's list, not the
  companion's.
- Opening `devices` mints a pairing code when the panel opens, not when Allen taps; an
  unused code expires on its own after ten minutes.
