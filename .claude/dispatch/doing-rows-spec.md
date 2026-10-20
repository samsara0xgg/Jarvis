# The day list: one row per stretch of what he was doing (shared spec, phone and Mac)

yilun (2026-10-20): the timeline's purpose was always "each row is a stretch of what I was doing". Today the list is the raw records (stays, Mac work blocks, calls, pictures...), so the top bar shows little more than 睡眠 and 在家. He agreed the layout below (in Chinese, in /mnt/project-files/时间线分层约定*.md; the local copy is beside this file as 时间线分层约定.md). The backend thread (第二层) owns the data and its quality; the UIs only draw it. When the brain gets better, the screens get better with no UI change: no wording rules, thresholds or merging of our own.

## The data (GET /inherent/day, already served; ADR 0302, docs/spec.html#doing_runs)

`spans[]`: each `{id, lane, kind, start_ms, end_ms, title, detail, category, basis, confidence, confirmed, clues[], question, place_id, item_ids[]}`.
- `lane == "doing"` and `kind` in `doing` / `unknown`: the waking day cut into back-to-back segments, every waking minute in exactly one. The latest model run's words up to where it covers, program-cut segments after it, his own answers over both. `kind == "planned"` is the plan after now.
- `category`: home, study, work, commute, errand, meal, social, rest, sleep, exercise, unknown (第二层 is removing `home`; draw whatever comes).
- `basis`: observed / stated (he said it) / inferred (the model guessed). `confidence`: high / medium / low.
- `place_id`: the stay item he was at, or null. `detail`: the model's one line about the segment, or null. `clues[]`: `{kind, at_ms, text}`, the evidence the model used.
- `item_ids` (ADR 0307, live 10-19): every item in the segment, by start. A lasting item is in every segment it overlaps (its length inside a segment is clipped to the segment); a point (picture, payment, departure, a reminder due) only in the one it falls in; label and claude_day never; talk is there (never drawn); planned spans have none. An id not in `items` is skipped. Mac calls never carry a person.
- Other lanes (`place`, `use`, `body`) are the facts the segments are cut from; not drawn as rows.

## The list

1. **Rows up to now are the doing segments** (`lane doing`, `kind` doing or unknown), in time order. The night (the sleep item, outside the waking window) stays as it is drawn today. After now: the plan rows exactly as today. Talk items are never drawn anywhere: not as rows, not inside a segment (the 和她 chip at the top stays).
2. **A row says**:
   - time column: the segment's start `HH:mm` (as today).
   - title: the words the UI already makes from a doing span (phone `doingWords`, Mac `dayView` wording: a stated or worded span's title as given, work → `做 X`, a program-cut call → `X 通话`). A segment with no words: `不知道在干嘛` / `No idea`, in the muted colour.
   - the line under it: the place name (the stay item `place_id` points to; else none), then `不太确定` / `Not sure` when `confidence == "low"`, joined by ` · `; then the counts of what is inside, as small icons each followed by its number, in this order and only when non-zero: pictures, payments, calls (call + phone_call), messages (messages). Nothing else.
   - right column: the length (as today's stretches say it).
   - colour: the category's colour (below) on the row's icon and on the bar.
3. **Opening a row** (the same open/close as today) shows, in order:
   - `detail`, when there is one.
   - the items in `item_ids`, minus talk, stay and sleep items, in time order, each one compact line: its icon, the words today's row for that kind says (title, and its own detail where short), and its time or length at the right. A picture shows its thumbnail as today. Media: app and the top title. Messages: the conversation and `发 N · 收 M`. Phone call: the person, the direction and the length. Phone use (screen): app and minutes. Work: projects and apps as today.
   - one muted line for how she knows: `你说的` / `You said so` (stated), `记录里看到的` / `From your records` (observed), `她猜的` / `Her guess` (inferred), followed by the clue texts, `；`-joined, cut to two lines.
   - the actions it has today for a stretch: 问她, and the quiet 这段在干嘛.
4. **No doing segments in the reply** (an older brain, a day it cannot serve): the list exactly as today. This is a real case, not a fallback for later.
5. **The bar**: the waking part drawn from the doing segments, coloured by category; the night as today; an unknown segment drawn like today's "没有记录" hole. The finger readout / hover keeps working (it already reads the doing words). The place view and per-category slices come in a later round; do not build them.

## Category colours and icons (identical on both platforms)

| category | colour | phone SF Symbol | Mac icon |
|---|---|---|---|
| work | #5B8CFF | laptopcomputer | work |
| study | #9B7BFF | book.fill | school |
| meal | #F0B04F | fork.knife | new: meal |
| rest | #6FA8C8 | sofa.fill | new: rest |
| sleep | today's sleep colour | moon.fill | moon |
| exercise | #4CC38A | figure.run | run |
| commute | today's move colour | today's trip icon (bus for a ride) | today's |
| errand | #E8875A | bag.fill | new: bag |
| social | #E979A6 | person.2.fill | new: people |
| home | #A89F91 | house.fill | home |
| unknown | none (muted, the hole style) | questionmark | new: unknown |

## Out of scope this round
Place view toggle, Mac's per-category (TimeSink) slices, one-off events as dots on the bar, the open/scroll interaction redesign, single rows for important moments, thumbnails on the row. Do not change the brain.
