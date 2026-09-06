# Goal: inherent-history-tab-layout

> SUPERSEDED by `docs/goals/popover-overflow-scrollable.md`. The mechanism recorded below was measured and disproved; the truncation is caused by the popover question having no height cap and no scroll region.

## Goal
Opening a history chip shows the whole popover — nothing cut off at the bottom — and
moving between chips no longer makes the card jump.

## Why
Two reports from hands-on testing, both inside the history tab: "点开历史卡片的最高高度
不能超过当前卡片 就是下面会截断" and "和上面 historytab 里面的交互 会出现跳动或者不顺畅".
The window height that bounds the popover and the SwiftUI position of the popover are
computed by two different owners that do not agree, so the same disagreement produces
both the clip and the jump.

## Current behavior

### What sizes the window
- `NativeCardController.targetPanelHeight()` is the only owner of window height
  (`desktop/inherent-swift/InherentCard/NativeCardController.swift:216-225`):

      hostingView.layoutSubtreeIfNeeded()
      let fitting = hostingView.fittingSize
      let popoverHeight = NativePopoverSizing.requiredPanelHeight(
        for: model.activeHistoryTurn,
        selectedTop: model.selectedPopoverTop
      )
      let measured = max(DisplayManager.MIN_HEIGHT, fitting.height, popoverHeight)
      return DisplayManager.clampPanelHeight(measured, on: panel.screen)

- `NativePopoverSizing.requiredPanelHeight` (`NativeCardController.swift:748-753`):

      NativeCardModel.pillReservedTop + selectedTop + popoverHeight(for: turn) + panelBottomSlack

  i.e. `38 + selectedTop + popoverHeight + 4`. `selectedTop` is `8 + visibleIndex * 31`,
  8…70 (`NativeCardModel.swift:169-176`).
- `DisplayManager.clampPanelHeight` (`DisplayManager.swift:22-28`) caps at
  `min(clampHeight(h), floor(visibleFrame.height - 16 + 38))` with
  `clampHeight = min(max(60, ceil(h)), MAX_HEIGHT=800)` (`DisplayManager.swift:8,12`).
  On any normal display this cap is not what the owner is hitting: with the 410pt answer
  cap below, a popover tops out near 500-600pt.

### DISPROVED — The clamp the owner sees is NOT a height cap — it is a position/height mismatch
**This mechanism is DISPROVED by measurement — see "Measured disproof" at the end of this
section.** The inference below is kept exactly as originally written so a reader can see what
was tried, and why it looked right from source alone before anyone measured it.

- `requiredPanelHeight` assumes the popover's top sits `pillReservedTop + selectedTop`
  below the **panel's** top edge. The popover is actually offset by exactly that amount
  from the **SwiftUI root ZStack's** top (`NativeCardView.swift:548`):

      .offset(x: -(model.cardWidth + NativeCardModel.popoverGap),
              y: NativeCardModel.pillReservedTop + model.selectedPopoverTop)

- The ZStack is not pinned to the panel top. `NativeCardView.body` ends with
  (`NativeCardView.swift:31`):

      .frame(width: model.panelWidth, alignment: .topTrailing)

  Width only. No height, no `alignment: .top` against the hosting bounds.
- The hosting view *is* pinned to all four edges of the panel content view
  (`NativeCardController.swift:44-52`):

      hostingView.leadingAnchor.constraint(equalTo: containerView.leadingAnchor),
      hostingView.trailingAnchor.constraint(equalTo: containerView.trailingAnchor),
      hostingView.topAnchor.constraint(equalTo: containerView.topAnchor),
      hostingView.bottomAnchor.constraint(equalTo: containerView.bottomAnchor),

  so whenever the panel is taller than the SwiftUI content, the surplus is distributed by
  SwiftUI's default centering, not pushed to the bottom.
- `.offset` does not participate in layout, so the ZStack's height is
  `max(popoverIntrinsicHeight, cardColumnHeight)` — it never includes the `38 + selectedTop`
  the popover is shifted down by. `requiredPanelHeight` therefore always exceeds the ZStack
  height by `42 + selectedTop` (46…112pt) while a popover is open. Half of that surplus
  lands above the content, pushing the card and the popover down by 23…56pt, and the
  popover's bottom runs past the window's bottom edge by the same order — which is the
  cut-off, and is also why it reads as "cannot exceed the current card".
- Confidence: this is derived from source, not from a run. It is the first thing the
  implementer must confirm or kill with the snapshot harness below, before editing. If the
  pre-fix snapshot does NOT show the predicted downward shift and clipped tail, the central
  mechanism of this card is dead: **stop and report to the hub.** Do not re-diagnose in-lane and
  do not start editing layout code to see what happens.

#### Measured disproof
Three pre-fix snapshots, each after a full explicit xcodebuild, at selectedTop=70, 39 and
39-with-overflow: the popover's top sits at EXACTLY pillReservedTop + selectedTop from the
container top (108 = 38+70, 77 = 38+39). Surplus distributed above the content: 0pt. Card top
measured 38-40pt against a predicted 23-56pt downward step — no shift. The bottom corner is
INTACT: the opaque-span profile narrows 24.0pt symmetrically at the top and bottom edges in
every image, with 2pt of transparent slack below. Nothing was cut.

Why the inference failed, from source: `ZStack(alignment: .topTrailing)`
(`NativeCardView.swift:22`) already top-aligns its children, and the card column is vertically
flexible (the answer ScrollView, `.frame(maxHeight: 461)`, `NativeCardView.swift:399`), so it
absorbs the surplus by stretching — measured at ~125pt and ~325pt in the taller snapshots. The
ZStack fills the hosting bounds and its top stays at the panel top. There is no centering to
correct, so the card's proposed fix would have been a NO-OP.

### The clamps that ARE deliberate — do not remove either
- `NativePopoverSizing.maxAnswerViewportHeight: CGFloat = 410` (`NativeCardController.swift:745`),
  applied as `min(maxAnswerViewportHeight, answerContentHeight(for: turn))`
  (`NativeCardController.swift:765-767`) and rendered as `.frame(height: answerViewportHeight)`
  on the popover's answer `ScrollView` (`NativeCardView.swift:538`). A long answer is meant to
  scroll inside 410pt, not to grow the window. `DisplayMathTests.swift:133-142` asserts it.
- `historyViewportHeight` caps the chip strip at 3 rows —
  `min(max(model.historyCount, 0), 3)`, `(n-1)*4 + n*29.25`
  (`NativeCardView.swift:300-305`), rendered as `.frame(height: visibleHeight)` +
  `.clipped()` on a `ScrollView` (`NativeCardView.swift:273-286`). Deliberate: the strip
  scrolls. Not the owner's complaint.

### What the card's own height is bound to
- Same `targetPanelHeight()`; with no popover open the answer is `hostingView.fittingSize`,
  which grows as tokens stream in. `applyPanelHeight` (`NativeCardController.swift:227-232`)
  sets the frame with `animate: false` and `DisplayManager.applyHeight`
  (`DisplayManager.swift:32-41`) keeps `maxY` fixed, so the card grows downward.
- `NativeCardModel.requestLayout(animatedFor:)` (`NativeCardModel.swift:1021-1027`) routes to
  either `updatePanelHeight()` (instant) or `animatePanelHeight(for:)` (tweened).

### Named mechanisms for the jump
1. **Wrong-source position, above.** The 23…56pt downward shift appears the moment a popover
   opens and disappears the moment it closes. Every chip hover/unhover crosses that step. This
   is a jump, not a slow animation — `applyPanelHeight` moves the window with `animate: false`.
2. **Every in-flight height animation is cancelled by any instant relayout.**
   `updatePanelHeight()` (`NativeCardController.swift:209-214`) unconditionally does
   `heightAnimationGeneration += 1; heightAnimationTask?.cancel()` before snapping to the new
   height. In the history tab the two paths interleave constantly: `showPopover` →
   `requestLayout(animatedFor: 0.32)` (`NativeCardModel.swift:470-476`), `hidePopover` →
   `requestLayout(animatedFor: 0.30)` (`:478-484`) fired 0.45s after a chip un-hover by
   `schedulePopoverHide` (`:486-491`), `toggleHistoryShown` → 0.42
   (`:439-444`). Moving across chips restarts the tween from the current height against a new
   target, repeatedly — a chain of half-finished eases.
3. **The tween is a hand-rolled 60Hz sleep loop, not display-synced.**
   `startPanelHeightAnimation` drives `panel.setFrame` from a `Task` doing
   `try? await Task.sleep(nanoseconds: 16_666_667)` (`NativeCardController.swift:254-272`).
   No `CAAnimation`, no `CVDisplayLink`. On a 120Hz ProMotion panel this drops and doubles
   frames by construction.
4. **Each history chip is an `NSTextView` whose intrinsic size is invalidated by its own frame
   change.** `historyChip` renders `NativeSelectableText` (`NativeCardView.swift:307-310`),
   backed by `NativeSelectablePlainTextView`, whose
   `intrinsicContentSize` reads `bounds.width` *and mutates* `textContainer.containerSize`
   inside the getter (`NativeCardView.swift:1303-1311`), while `setFrameSize` calls
   `invalidateIntrinsicContentSize()` on every size change (`NativeCardView.swift:1313-1318`).
   Rows inside the strip's `ScrollView` therefore re-measure on layout passes that change their
   width — the classic measure/invalidate ping-pong. Secondary to (1)-(3) for the reported
   symptom; listed so it is not mistaken for the cause.

### Do the two symptoms share a cause
Yes, for the primary pair. Mechanism (1) produces both: the surplus window height that
`requiredPanelHeight` creates is split above/below instead of being pinned to the top, which
simultaneously clips the popover's bottom and steps the card down when a popover appears.
Mechanisms (2), (3) and (4) are separate, independent contributors to "unsmooth" only — they do
not clip anything, and this card does not touch them. They are recorded under non-goals so the
next card inherits them. The owner reports one symptom and we have one root cause that explains
it; fixing four things at once means nobody learns which one mattered. They are re-evaluated only
after he has seen this fix.

### Dead code, so it is not chased
`NativeCardController.pollLayout(for:)` (`NativeCardController.swift:297-310`) — a 60Hz
`updatePanelHeight` poll — has exactly one reference in the whole repo, its own definition
(`grep -rn "pollLayout" --exclude-dir=.git .` returns one line). It is not running. It is not
the jump.

### Legacy comparison (the port lost nothing)
`/Users/alllllenshi/Projects/jarvis-legacy` exists and contains the same app.
- Against legacy HEAD (`jarvis-legacy/desktop/inherent-swift/InherentCard/NativeCardView.swift`):
  `historyStrip`, `historyViewportHeight`, `historyChipHeight = 29.25` and the whole
  `popoverLayer` — including the same `.offset(y: pillReservedTop + selectedPopoverTop)` and the
  same missing top pin — are identical. `NativeCardController` differs only by the
  `applyCardWidth` / `cardWidth` parameterization added in 8dcfbb0.
- The `NativeSelectableText` swap (8155e10) came from `jarvis-legacy` `stash@{1}`. Diffing
  `git show "stash@{1}:desktop/inherent-swift/InherentCard/NativeCardView.swift"` against the
  current file: the ONLY deltas are the resize handle and `NativeCardModel.cardWidth` →
  `model.cardWidth`. `intrinsicContentSize`, `setFrameSize`, `historyStrip`, `historyChip` and
  `popoverLayer` are byte-identical.
- Conclusion: the port dropped no layout modifier. This is a pre-existing defect carried in
  faithfully from the legacy WIP, so "restore what the port lost" is not available as a fix.

## Replacement lead (UNCONFIRMED — not an established cause)
The reference-frame mismatch above is dead. This is where the next card should start looking,
not a diagnosis to build on directly.

(a) **The harness could never have reproduced the bug.** The committed `runPopoverScenario`
opens the popover on `history.last` — the same turn the card is already rendering — so the
popover and the card always grow together and `requiredPanelHeight` never wins the `max()` in
`targetPanelHeight`. Popover/card height divergence is inexpressible in that scenario. Any
future work here must first extend the scenario to open an OLDER, TALLER turn than the one
being rendered.

(b) **The owner's observed behaviour**, gathered after the disproof above: the card sits at the
TOP-RIGHT of the screen (so the screen-bottom clipping hypothesis is dead — it is not a low card
running off the screen edge); with a current card about 700px tall, opening ANY history entry
taller than that gets truncated at the current card's height; and the truncated text CANNOT be
scrolled — the content is gone, not merely below a short viewport with `showsIndicators: false`.
Together these say the popover's usable height is bound to the CURRENT CARD's height rather than
to its own content, in exactly the divergent case the scenario cannot express.

## Target behavior
- With a history popover open, the popover's bottom padding and its rounded bottom corner are
  fully inside the window. Nothing is cut.
- The card's top edge does not move when a popover opens or closes. Opening a popover changes
  the window's height, never the card's position within it.
- The deliberate caps still hold: the answer viewport is still capped at
  `NativePopoverSizing.maxAnswerViewportHeight` and still scrolls; the chip strip still shows
  at most 3 rows and still scrolls.
- The owner, on a freshly rebuilt binary, opens the history tab, sweeps across all chips and
  opens one, and reports no jump and no truncation.

## Affected contracts and files
- L5 surface `desktop/inherent-swift/InherentCard/NativeCardView.swift:31` — the root ZStack must
  resolve against the hosting bounds with its top pinned, so the surplus height
  `requiredPanelHeight` reserves lands below the content instead of being split.
- L5 surface `desktop/inherent-swift/InherentCard/NativeCardController.swift:216-232`,
  `:741-753` — `targetPanelHeight` / `requiredPanelHeight` must agree with wherever the popover
  actually resolves. Either the view pins to the top, or `requiredPanelHeight` stops assuming a
  panel-relative origin. Pick one owner; do not add a second correction on top of the first.
- `desktop/inherent-swift/InherentCard/NativeCardController.swift:594-617` `runPopoverScenario`
  — debug-only; its three answers are short, so the clip may be only a few points. Lengthening
  one answer to make the artifact readable is expected and allowed. It is a debug scenario, not
  product behavior.

## Boundaries and non-goals
- Layers that may change: L5 surface, `desktop/inherent-swift/` only.
- `jarvis/` and every Python package are out of scope. No other lane is in Swift right now, so
  this card owns `desktop/inherent-swift/` for its duration — but that does not extend it to
  anything else in the repo.
- **Do not touch the worktree `/Users/alllllenshi/Projects/jarvis/.claude/worktrees/realtime-live-test`.**
  It currently holds the owner's uncommitted hands-on edits (`BridgeBackend.swift`,
  `RealtimeTransportV2.swift`, port 8006 → 8009). `git status --short` there shows exactly those
  two modified files. Work in this lane's own worktree; never check out, stash, or build in that one.
- Must not change: the left-edge resizable card width and its double-click reset (8dcfbb0,
  `NativeCardView.swift:84-135`, `NativeCardController.applyCardWidth`); the auto-growing input
  and the selectable answer view (8155e10, `NativeInputTextSizing`,
  `NativeSelectableMarkdownText`, `NativeSelectableText`); Cmd+Space toggle
  (`HotkeyManager.swift`, `NativeCardController.toggleHotkey`) and Esc
  (`NativeCardView.swift:46-49`, `NativeCardModel.handleEscape`,
  `NativeCardController` keyCode 53 monitor).
- Must not change — **these are the two deliberate height clamps in this file, and neither is what
  the owner is hitting.** An implementer hunting for "a height clamp" will find one of them first;
  changing either is changing the wrong thing.
  - `NativePopoverSizing.maxAnswerViewportHeight = 410`
    (`NativeCardController.swift:745`, applied at `:765-767`, rendered at `NativeCardView.swift:538`)
    — a long popover answer is meant to scroll inside 410pt, and
    `DisplayMathTests.swift:133-142` asserts it.
  - The 3-row chip strip cap — `min(max(model.historyCount, 0), 3)`
    (`NativeCardView.swift:300-305`), rendered with `.clipped()` at `:273-286` — the strip is
    meant to scroll.
  - `DisplayManager.MAX_HEIGHT = 800` (`DisplayManager.swift:8`) — a normal popover is ~500-600pt.
  If the fix appears to need any of the three raised, that is evidence the wrong thing is being
  fixed — stop and report to the hub.

### Non-goals — this card fixes ONE thing
The reference-frame mismatch, and nothing else. The following are real, are diagnosed, and are
deliberately left alone so that the owner's verdict attributes cleanly to one change. Each is
recorded with its location so the next card inherits this work instead of re-finding it. They are
re-evaluated only after he has seen this fix.
- Jump-only: `updatePanelHeight` unconditionally cancels any in-flight height tween
  (`NativeCardController.swift:209-214`) against the history tab's interleaved 0.30/0.32/0.42s
  relayouts (`NativeCardModel.swift:439-491`).
- Jump-only: the height tween is a hand-rolled `Task.sleep(nanoseconds: 16_666_667)` loop driving
  `panel.setFrame`, not display-synced (`NativeCardController.swift:254-272`) — drops frames on a
  120Hz panel by construction. Do not refactor it into `CAAnimation`/`CVDisplayLink` here.
- Jump-only: `NativeSelectablePlainTextView.setFrameSize` calls `invalidateIntrinsicContentSize()`
  while `intrinsicContentSize` reads `bounds.width` and mutates `textContainer.containerSize`
  inside the getter (`NativeCardView.swift:1303-1318`); every history chip is one of these
  (`NativeCardView.swift:307-310`). Leave its intrinsic-size contract alone.
- Dead code, LEAVE IT: `NativeCardController.pollLayout(for:)`
  (`NativeCardController.swift:297-310`) has exactly one reference repo-wide — its own definition.
  It is unrelated to the defect, and deleting it would muddy both the diff and the
  revert-and-restore proof, which is this card's only evidence for a visual bug.
- ADR-0014 Step 0's separate open item of fixing `launcher.py` staleness detection.

## Rejected approaches
- Restore a modifier the port dropped — there is none. The history-tab layout in this tree is
  byte-identical to the legacy source it came from, verified by diffing both legacy HEAD and
  `stash@{1}`. Do not spend the run looking for it.
- Remove or raise the 410pt answer viewport cap or the 3-row strip cap — both are deliberate,
  both are load-bearing for the popover's scroll behavior, and one is covered by an existing
  test. Neither is the reported bug.
- Raise `DisplayManager.MAX_HEIGHT` — a normal popover is ~500-600pt tall, nowhere near 800.
  Raising it hides nothing and fixes nothing.
- Fix the jump mechanisms alongside the binding fix — four changes in one diff and the owner's
  verdict attributes to none of them. They are in non-goals with their locations.
- Add a Swift test that constructs one view struct and asserts on its own computed property
  (e.g. a new `XCTAssertEqual` on `requiredPanelHeight`) and call it acceptance. Same shape as a
  Python unit test; it asserts the arithmetic that is already wrong, not the pixels the owner sees.
- Build a snapshot-diff framework, add a UI-test target, or add an image-comparison dependency.
  The app already writes PNGs on `INHERENT_DEBUG_SNAPSHOT_PATH`. Use it.
- Dress the smoothness check up as an automated test. There is no honest automated assertion for
  "feels smooth". It is the owner's eyes, and the card says so.

## Acceptance evidence
- **Baseline first, never a baked number.** `bash scripts/test_inherent_swift.sh` is run BEFORE
  any edit and its raw tail is shown. At tip 9767aa6 the tree defines 178 `func test` methods
  (`grep -rh "  func test" desktop/inherent-swift/InherentCardTests/ | wc -l` → 178), but the
  printed `Executed N tests` from the pre-edit run is the baseline of record. Suite must be green
  before the first edit.

- **Rebuild, or the run is worthless.** `desktop/inherent-swift/launcher.py:36-50`
  `ensure_app_built()` returns early on `if APP_BIN.exists(): return` — there is no staleness
  check, despite the module docstring at line 4 claiming "if the .app is missing or stale". After
  any Swift edit, `python desktop/inherent-swift/launcher.py` silently launches the OLD binary.
  Every visual check must be preceded, in the same transcript, by:

      cd desktop/inherent-swift && xcodegen generate && \
        xcodebuild -project InherentCard.xcodeproj -scheme InherentCard \
          -configuration Debug -derivedDataPath build build

  `-derivedDataPath build` is required: it is the exact path `launcher.py:26` spawns from
  (`HERE/"build"/Build/Products/Debug/InherentCard.app`). The `** BUILD SUCCEEDED **` line must
  appear before any snapshot or live run is trusted.

- **Positive, height clamp — measurable, on a real artifact.** A snapshot-testing path already
  exists and is reused, not reinvented: `NativeCardController` reads
  `INHERENT_DEBUG_SNAPSHOT_PATH` / `_DELAY_MS` / `_DIR` / `_START_MS` / `_INTERVAL_MS` / `_COUNT`
  (`NativeCardController.swift:95-103`, `writeDebugSnapshot` `:334-352`,
  `scheduleDebugSnapshots` `:353-377`). `writeDebugSnapshot` captures `containerView.bounds` —
  the window's content bounds — so anything the window clips is clipped in the PNG too, which is
  precisely the artifact wanted. `INHERENT_DEBUG_FAKE_TURNS=popover`
  (`NativeCardController.swift:594-617`) seeds three turns and opens the last one's popover at
  about t=3.1s.

      INHERENT_DEBUG_FAKE_TURNS=popover \
      INHERENT_DEBUG_SNAPSHOT_PATH=/tmp/popover-before.png \
      INHERENT_DEBUG_SNAPSHOT_DELAY_MS=3600 \
      python desktop/inherent-swift/launcher.py

  Artifact: the PNG at that path. Assertion, stated in the transcript for each of the before and
  after images: whether the popover's bottom padding and rounded bottom corner are inside the
  image, or cut at its bottom border. Before the fix the bottom is cut; after the fix it is
  whole. If the scenario's short answers make the clip too small to read, lengthen one
  `runPopoverScenario` answer (debug-only) and say so.
  **If the pre-fix PNG shows no downward shift and no clipped tail, the diagnosis in this card is
  dead: stop and report to the hub.** Do not re-diagnose in-lane, and do not start editing layout
  code to see what happens. A card whose central mechanism is inferred owes the lane a defined
  exit when the inference dies, and this is it.

- **Positive, jumping — the owner's eyes, stated as such.** There is no honest automated
  assertion for this and none is invented. **Live run: required.** With a freshly rebuilt binary,
  the owner is asked to: (1) run a couple of real turns so at least three chips exist, (2) click
  the history pill to expand the strip, (3) move the cursor slowly across all three chips and
  back, (4) click one chip and read the popover to its last line, (5) press Esc / move away to
  dismiss. "Fixed" means: the card's top edge does not step down or up when a popover appears or
  disappears; the popover's last line and its bottom rounded corner are visible; sweeping across
  chips shows one continuous height change rather than a stutter. The transcript records the exact
  question put to him and his answer verbatim. The run may not self-certify this.
  Audio rule: this is layout work and must not touch audio. If any live run involves audio at all,
  the macOS system default output device is never changed.

- **Load-bearing proof.** On the finished tree: revert the fix (inverse edit or `git stash`),
  rerun the same `xcodebuild` line to `** BUILD SUCCEEDED **`, retake the snapshot to a third
  path, and state that the clip returned in that image (and, where a test was added, that it
  fails). Then restore the fix and rebuild. All three PNG paths are listed with `ls -l`.

- **Regression.** `bash scripts/test_inherent_swift.sh` again, raw tail shown, final
  `Executed M tests` quoted, with `M = N + k` and `k` stated as the number of tests this run
  added (`k = 0` is a valid answer for a pure layout fix). No existing test deleted or weakened;
  `DisplayMathTests.test_popoverSizingCapsLongAnswerViewport` and
  `test_popoverSizingExtendsPanelBelowOffsetPopover` still pass unchanged.

## Docs to sync
- none. `grep -c -i "popover" docs/spec.html` → 0 and `grep -c -i "inherent-swift" docs/spec.html`
  → 0, so the spec owns no fact about this surface. ADR-0014 mentions the history popover only in
  its Step-0 inventory and its Definition-of-done list — `docs/adr/0014-inherent-realtime-ux.md:2055`
  ("history popover ... retain their current user-visible behavior") and `:1924` (Step 0 table
  row) — neither states a layout rule, and restoring the intended un-clipped popover is exactly
  "current user-visible behavior" retained, not a contract change. If the implementation ends up
  changing an externally visible rule (for example the popover's maximum height becoming a
  documented number), the canonical owner is ADR-0014 §Step 0 and it must be updated then; the
  grep results must be shown either way.

## Open questions
(none)

## /goal condition
Done when the transcript shows ALL of the following:

1. BASELINE FIRST: the raw output of `bash scripts/test_inherent_swift.sh` run BEFORE any edit,
   with its final `Executed N tests` line quoted. N is the baseline of record. The tree defines
   178 `func test` methods at tip 9767aa6, but the printed number wins — never a baked one.
2. A root-cause statement naming the exact `path:line` that binds where the history popover
   resolves on screen, and why it disagrees with `NativePopoverSizing.requiredPanelHeight`.
3. REBUILD, shown before every visual check:
   `cd desktop/inherent-swift && xcodegen generate && xcodebuild -project InherentCard.xcodeproj
   -scheme InherentCard -configuration Debug -derivedDataPath build build`, raw, ending in
   `** BUILD SUCCEEDED **`. `launcher.py:36-50` builds ONLY when the binary is missing, so a
   launch without a preceding successful build proves nothing about the edit.
4. BEFORE/AFTER ARTIFACTS: two PNGs written by
   `INHERENT_DEBUG_FAKE_TURNS=popover INHERENT_DEBUG_SNAPSHOT_PATH=<path>
   INHERENT_DEBUG_SNAPSHOT_DELAY_MS=3600 python desktop/inherent-swift/launcher.py`,
   each shown with `ls -l`, each with a stated reading: BEFORE = the popover's bottom edge is cut
   at the image's bottom border; AFTER = the popover's bottom padding and rounded corner are
   fully inside the image. If the BEFORE image shows no downward shift and no clip, the card's
   central mechanism is dead: the run STOPS AND REPORTS TO THE HUB, re-diagnoses nothing in-lane,
   and edits no layout code.
5. LOAD-BEARING PROOF: the fix is reverted on the finished tree, the tree is REBUILT with the
   same xcodebuild line, a third snapshot is taken, the transcript states the clip returned (and
   that any added test fails); then the fix is restored and rebuilt.
6. REGRESSION: `bash scripts/test_inherent_swift.sh` raw output again, final `Executed M tests`
   quoted, with `M = N + k` and k stated as the number of tests this run added (k = 0 is valid).
   No existing test deleted or weakened.
7. OWNER CONFIRMATION, which the run CANNOT self-certify: the transcript records that the owner
   was asked, in these terms, to open the history tab on the freshly rebuilt binary, sweep the
   cursor slowly across all chips and back, click one chip and read the popover to its last line,
   then dismiss it — and to say whether the card's top edge still steps when the popover appears
   or disappears, and whether anything is still cut off at the bottom. His answer is recorded
   verbatim, or the run is recorded as blocked waiting for it. A claim that it "feels smooth"
   without his words is a failure of this condition.
8. `git status` shows a clean tree; every slice committed with the commit skill; the card's
   Progress section updated with sha + one evidence line per slice.
9. DOCUMENTATION RULE: each entry under "Docs to sync" was updated or explicitly judged
   unchanged, with the grep output that proves it. When the implementation changes a documented
   contract, invariant, ownership boundary, or externally relevant behavior, update the canonical
   document that owns that fact; do not document what the code already makes clear; do not
   duplicate a fact across documents.
10. NO file outside `desktop/inherent-swift/` is modified, and the worktree
    `.claude/worktrees/realtime-live-test` is never checked out, stashed, built in, or otherwise
    touched — it holds the owner's uncommitted test edits.
11. SCOPE: exactly one defect fixed — the reference-frame mismatch. Every item under the card's
    "must not change" and "Non-goals" lists is UNCHANGED (`maxAnswerViewportHeight`, the 3-row
    strip cap, `MAX_HEIGHT`, `NativeCardController.swift:209-214`, `:254-272`, `:297-310`,
    `NativeCardView.swift:1303-1318`), or the run stopped and reported to the hub.

Or stop after 40 turns.

## Progress
- No layout code was changed by the lane that ran this card — this update is docs-only
  (the disproof and replacement lead above); branch lane/c was left clean.
