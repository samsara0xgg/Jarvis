# Day timeline: code map (phone + Mac)

Line numbers as of `land/notch-timeline` `64aa08bb` (the Mac branches rebased on main + the phone call-icon fix). Update this file when structure moves.

- P = `ios/Jarvis`
- M = `desktop/resonance/src`

Not covered: the tests (`ios/JarvisTests/DayTimeline*Tests.swift`, `desktop/resonance/scripts/verify-notch-timeline.mjs`), `electron/`, and the brain's `_read_day` in `jarvis/runtime/inherent_loop.py`.

## Phone (SwiftUI)

### 1. Decoding GET /inherent/day
- Fetch: `P/Brain/BrainClient.swift:388` `BrainClient.day`. Called per day from `P/Brain/BrainLink.swift:174`.
- Reply: `BrainClient.swift:49-89` `DayReply.init(from:)`.
  - Items, labels, highlights and spans are each wrapped in `Lossy<>` (:92), so a bad element drops only itself.
  - `Soft`/`SoftList` are at :275-289.
- Types: `DaySpan` at :111-123 (with `item_ids` and `clues`), `DayClue` at :126, `DayCoverage` at :131, `DayItem` at :166-240.
- `P/Sky/DayTimeline.swift:104-127` `TimelineState` (`nowMinute`, `reply`, `model(r)`).
- `P/Sky/DayTimelineModel.swift:11` `TLClock` turns ms into minutes in the brain's tz.

### 2. List rows, doing stretches and the category table
- `DayTimelineModel.swift:525` `build` is the orchestrator.
  - :546-553 filters items.
  - :564-588 assigns lens, sums and `seg()`.
  - :593 uses `segmentList` when there are doing spans, else `arrange` (:656, the old place-group list).
  - :600-605 adds the `.doing(color)` bar segs.
- `P/Sky/DayTimelineDoing.swift`:
  - :13 `TLCategory.of` is the category table (colour + SF symbol).
  - :31 `TLCounts`.
  - :62 `doingStretches` keeps kind doing/unknown that start before now and clips the end to now.
  - :73 `segmentList`: sleep rows, plus later/waiting rows except talk, plus one row per span.
  - :93 `segmentRow`:
    - title = `doingWords` or 不知道在干嘛;
    - symbol at :100-101 (commute takes the trip's symbol; an observed call gets `phone.fill`);
    - counts and lenses at :108-117;
    - `lines` at :118;
    - bar name and icon at :121-124.
- `P/Sky/DayTimelineRows.swift`:
  - :53 `row(item)` builds every non-doing row (the same words as the Mac's `rowOf`).
  - :369 `doingWords`.
- `DayTimelineModel.swift:207` `TLEntry`, :266 `starrable`, :282 `opens`.
- Draw: `P/Sky/DayTimelineRowViews.swift:51` `TLRowView` (:208 `TLCountsView`). `DayTimeline.swift:415` `TLList`, with the dim rule at :480.

### 3. Day bar
- `P/Sky/DayTimelineBar.swift:56` `scene` builds the pieces.
  - Priority is via `Model:462` `seg(at:)`: sleep > trip > place/doing.
  - The ahead boxes are at :83-91.
- `Bar:99` `TLPaint`. Fills are at :109, with stage shading at :122 and the hatch at :504.
- `Bar:153` `TLBarLabel.fit`, :196 `TLGeo.rects`, :256 `TLBarView`.
- Axis: `Model:809-820` (`axisKinds`, `axisStart`). Wake is at :612 and early at :613.
- `Model:399` `TLSeg`. The hypnogram is at `P/Sky/DayTimelineOpen.swift:339`.

### 4. Opened row
- `P/Sky/DayTimelineOpen.swift:13` `TLOpenView`.
  - :72 `segment` shows the note, then the inside lines (:85 `insideLine`), then how she knows.
  - Buttons are at :225 and the 这段在干嘛 link at :245.
- Where its data comes from:
  - note = `Doing:106`;
  - an inside line's text and clipped length = `Doing:135` `line(_:in:)`;
  - how she knows = `Doing:144`.
- Actions: `Model:762` `actions(of:)`. Titles and symbols are at `RowViews:15-49`, the long-press menu at :238.

### 5. Readout on the bar
- `Model:481` `readout(at:)`. Ranking is at :503, and the per-row sources (`told`) at :635-639.
- View: `P/Sky/DayTimelineTop.swift:83` `readout()`. Scrub is at :125.

### 6. Chips
- `Model:298` `TLLens`; the chips are built at :619-633; `lensMarks` is at :513; `TLChip` at :341.
- `Top:151` `TLChips`, :196 `activeLens`.
- There are no category chips yet, only lens chips.

### 7. 问她 / 这段在干嘛
- 问她 is offered when `e.about != nil` (`Model:772`). A stretch's `about` is the place stay, else the first item (`Doing:126-129`).
  - Then `engine.askAbout` (`P/Sky/StarTalk.swift:58`).
  - The id rules are in `P/Brain/About.swift:6,24,27`.
- 这段在干嘛 is in `P/Sky/DayTimelineSayWhat.swift`: kinds at :22, the 200 cap at :17, begin/submit at :68/:76.
  - Route: `P/Brain/DayActions.swift:97` `stateSpan` (POST /inherent/day/answer), called via `P/Sky/DayTimelineBrain.swift:15,27`.
- Star: `P/Sky/DayTimelineMarks.swift:41` (cap 5 at :15). Mail confirm: `DayTimelineCommit.swift:13`.

## Mac (React/TS, notch companion)

### 1. Decoding
- Fetch:
  - `M/Companion.tsx:273` `useRoute('/inherent/day')` for today, every 30 s (`M/homeData.ts:33`);
  - `M/DayTimeline.tsx:245` for other days;
  - then :303 `buildDay`.
- `M/dayView.ts`:
  - :109 `tidyReply`, :77 `tidy` (item);
  - :99 `doingOf` (`item_ids` at :105);
  - types at :8, :32 and :44.

### 2. Rows, stretches and the category table
- `dayView.ts:553` `buildDay`:
  - spans sorted by start then id (:569);
  - on a segmented day, only sleep/future/waiting rows are kept (:583-584);
  - `segmentOf` per span (:594-599).
- `dayView.ts:518` `segmentOf`:
  - inside items from `item_ids` (:521);
  - icon at :524-526 (no words gives `unknown`, commute gives bus or move, an observed call gives `call`);
  - inside lines at :527-534, counts at :535, how she knows at :536-537.
- Tables in `dayView.ts`: :504 `CATEGORIES`, :510 `COUNTED`, :512 `NOT_INSIDE`, :513 `SHORT`, :514 `BASES`.
- Words in `dayView.ts`: :365 `doingWords`, :400 `rowOf`.
- `M/dayIcons.tsx:7` `ICONS`.
- Draw: `M/DayTimeline.tsx:120` `RowView`, :170 `StayView`.

### 3. Day bar
- `M/barView.ts:76` `buildBar`.
  - Priority: sleep 3, items 2, seg/stay 1.
  - An unknown stretch is a hole (:102); commute is a 0.28 haze (:103); sleep is one plain piece (:110).
- Axis: :20 `wakeOf`, :27 `REACH`, :30 `axisOf`. Label fit is at :66-68.
- `M/DayBar.tsx:98` `DayBar`, with its state hook `useBarState` at :32.

### 4. Opened row
- `M/dayOpen.ts:99` `openOf`, :90 `openSegment`.
- `M/RowOpen.tsx:217` `OpenBody` (:152 `Inside`, :194 `Buttons`, :189 `SayWhat`).
- The actions are wired at `DayTimeline.tsx:609` `body()`.

### 5. Hover card
- `barView.ts:203` `peek` (`NEAR_MIN` 10, `ORDER` :160, 4 lines), and :292 `summarize` for a dragged range.
- `DayBar.tsx:56` `PeekBody`, :70 `RangeBody`.
- The wing's one line: `dayView.ts:197` `wingText`, drawn at `Notch.tsx:511,714`.

### 6. Chips and numbers
- `barView.ts:243` `LABELS`, :261 `numbers`. `DayBar.tsx:26-27` `KIND`/`KIND_ICON`, with the chips at :270-278.
- Dim: `M/day-bar.css:137-139`.
- `M/numberCard.ts:140` `cardOf` (the phone has none).

### 7. 问她 / 这段在干嘛
- `dayOpen.ts`: :217 `askOf` (a stretch sends a `lead` text, no chip), :233 `NOT_STRETCH`, :239 `sayOf`, :247 `SAY_MAX`.
- `DayTimeline.tsx`: :547/551 ask, :562/567 say (POST /inherent/day/answer), :490 star.
- Sent from `Companion.tsx:515` `askAbout`.
- `dayView.ts:224` `ABOUT_KINDS`, :227 `starrable`.

## Change together (phone <-> Mac)

| Rule | Phone | Mac |
|---|---|---|
| Category colour + icon | Doing:13 | dayView.ts:504 |
| doingWords | Rows:369-380 | dayView.ts:365-377 |
| Observed-call icon | Doing:101 | dayView.ts:526 |
| Commute icon | Doing:100 | dayView.ts:525 |
| Which spans, clip to now | Doing:62-69 | dayView.ts:99-100, 519 |
| Inside-line filter | Doing:118 | dayView.ts:512, 521, 528 |
| Inside-line length clip | Doing:135-141 | dayView.ts:531-533 |
| Counted icons | Doing:109-115 | dayView.ts:510, 535 |
| How she knows wording | Doing:144 | dayView.ts:514, 536 |
| Hole / unknown | Doing:123-124, Bar:75 | dayView.ts:524, barView.ts:102 |
| Lens table, words, sums | Model:298-339, 302, 619-633 | dayView.ts:53, barView.ts:243, 261, DayBar.tsx:26 |
| Lens dim levels | DayTimeline.swift:480 | day-bar.css:137 |
| Axis start | Model:809-820 | barView.ts:27-37 |
| Bar paint + priority | Bar:109, Model:405 | barView.ts:84, 103 |
| Label fit | Bar:153 | barView.ts:66 |
| Readout / peek | Model:481-506 | barView.ts:160, 203-239 |
| Say-what kinds, cap, route | SayWhat:17, 22, DayActions:97 | dayOpen.ts:233, 247, DayTimeline.tsx:571 |
| 问她 ids, starrable | About.swift:24, 27, Model:266 | dayView.ts:224, 227 |
| Highlight label regex, star cap | Model:535, Marks:15 | dayView.ts:559, DayTimeline.tsx:493 |
| Row words per item kind | Rows:53-293 | dayView.ts:400-478 |
| Weather, money | Rows:387, Model:63 | dayView.ts:259-299 |

## Where they already disagree (decide before unifying)

- **A stretch with no title but a coloured category:** the phone colours it, the Mac makes it a hole.
- **An observed call on the bar:** the phone makes it a hole with no icon (the colour is nil); the Mac draws a neutral piece with a call icon.
- **Commute on the bar:** the phone fills it at 0.85 with no hatch; the Mac draws a 0.28 haze.
- **`claude_day` inside a stretch:** the phone keeps it; the Mac drops it.
- **Inside-line limits** (detail over 40 chars hidden, picture id must be 32 hex, clipped length only when the overlap is at least 60 s): the Mac only.
- **Span kinds:** the phone takes doing/unknown before now; the Mac takes anything except planned and keeps future spans. The sort differs too: the phone by start, the Mac by start then id.
- **问她 on a stretch:** the phone sends an item chip; the Mac sends a lead text.
- **Summarize text:** the two clients word it differently.
- **Payment lens:** the phone tags only outgoing payments; the Mac tags all on the bar and counts outgoing in the chip.
- **Wake:** the phone uses the earliest sleep end; the Mac uses the longest sleep. Axis "early": the phone uses the doing starts; the Mac uses the REACH kinds.
- **Sleep on the bar:** the phone shades by stage; the Mac draws one plain piece (10-20 decision: plain).
- **Say-what kinds:** the phone includes claude; the Mac excludes `claude_day`.
- **Short stop:** starrable on the Mac only.
