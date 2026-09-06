# Goal: popover-overflow-scrollable

## Goal
A history popover whose content is taller than the window the sizing math is
allowed to open never loses that content: the surplus stays inside a scroll
region instead of falling outside the panel.

## Why
Owner report: the card sits top-right; when a history entry is taller than the
current card (his card is around 700px) the entry is truncated and the truncated
text cannot be scrolled — the content is gone, not merely below a short viewport.

This supersedes `docs/goals/inherent-history-tab-layout.md`. That card's
mechanism — `requiredPanelHeight` under-reserving window height so the popover
shifted down — is DISPROVED and dead: three snapshots measured the popover top at
exactly `pillReservedTop + selectedTop` with 0pt surplus and no downward shift.
Do not re-open it.

## Current behavior

### The height chain, text to window (all re-verified at lane/c tip dddbb50)
- `NativePopoverSizing.textHeight` measures a string with `NSString.boundingRect`
  at a fixed width derived from `NativeCardModel.popoverWidth` (300), not from the
  resizable `cardWidth`
  (`desktop/inherent-swift/InherentCard/NativeCardController.swift:791-801`,
  `contentWidth` at `:787-789` = 300 - 22*2 = 256).
- The ANSWER is capped and scrollable. `maxAnswerViewportHeight = 410`
  (`NativeCardController.swift:745`); `answerViewportHeight(for:)` =
  `min(maxAnswerViewportHeight, answerContentHeight(for:))` (`:765-767`); the view
  reads that value at `NativeCardView.swift:513`, renders the answer inside
  `ScrollView(.vertical, showsIndicators: false)` (`:527-537`) and bounds it with
  `.frame(height: answerViewportHeight)` (`:538`).
- The QUESTION is UNCAPPED and UNSCROLLED. `questionHeight(for:)`
  (`NativeCardController.swift:769-776`) returns raw `textHeight` with no `min`,
  no cap constant. `NativeCardView.swift:515-526` renders it as a bare
  `NativeSelectableText` with a `.padding(.bottom, 12)` and a 1pt divider overlay,
  and NO `ScrollView` wrapper. `grep -rn "maxQuestion\|questionViewport" ` over
  `desktop/inherent-swift/` returns nothing.
- `popoverHeight(for:)` (`NativeCardController.swift:756-763`) sums
  `topPadding 18 + questionHeight + questionBottomPadding 12 +
  questionAnswerSpacing 12 + answerViewportHeight + bottomPadding 20`, so the
  uncapped question term flows straight through.
- `requiredPanelHeight(for:selectedTop:)` (`:748-754`) =
  `NativeCardModel.pillReservedTop (38, NativeCardModel.swift:59) + selectedTop +
  popoverHeight + panelBottomSlack (4)`. `selectedPopoverTop`
  (`NativeCardModel.swift:169-176`) is `8 + min(idx,2) * 31`, so 8 / 39 / 70.
- Reduced: **required = 104 + selectedTop + questionHeight + answerViewport**,
  worst case **174 + questionHeight + 410**.
- `targetPanelHeight()` (`NativeCardController.swift:216-224`) =
  `clampPanelHeight(max(MIN_HEIGHT, hostingView.fittingSize.height,
  requiredPanelHeight(...)), on: panel.screen)`. `fitting.height` reflects the
  CURRENT card only: the popover is placed with `.offset(...)`
  (`NativeCardView.swift:548`) and `.offset` does not contribute to a parent
  stack's size, and the root ZStack constrains width only
  (`NativeCardView.swift:31`, `.frame(width: model.panelWidth, ...)`).
- `DisplayManager.clampPanelHeight` (`DisplayManager.swift:22-28`) imposes the ONLY
  content-independent ceilings in the chain:
  `min(clampHeight(h), floor(visibleFrame.height - CARD_MARGIN 16 +
  PILL_RESERVED_TOP 38))`, with `clampHeight = min(max(60, ceil(h)), MAX_HEIGHT
  800)` (`DisplayManager.swift:8,12`). Both ceilings are ≤ 800 by construction.
- `applyPanelHeight` (`NativeCardController.swift:227-232`) →
  `DisplayManager.applyHeight` (`DisplayManager.swift:32-41`) sets the NSPanel
  frame top-edge anchored; asserted by `DisplayMathTests.swift:5-15`.

### The defect
Nothing reconciles what the sizing math ASKS FOR with what the clamp actually
GRANTS. `requiredPanelHeight` is unbounded above (the question term has no cap);
`clampPanelHeight` returns at most 800. When required exceeds the granted height,
the surplus is outside the window — and because the question has no scroll region,
it is unreachable. That reconciliation is missing regardless of which term
overflowed, which is why this card does not depend on reproducing the owner's
exact entry.

The step from "outside the window" to "silently clipped rather than scrollable"
is `hostingView.wantsLayer = true` (`NativeCardController.swift:43`): layer-backed
AppKit views clip drawn content to their own bounds. **This is inferred platform
behavior, not measured in this repo.** The card does not rest on it — see
Acceptance evidence.

### Not settled, and deliberately not resolved here
Whether the owner's specific truncation came from (A) the uncapped question term
pushing `requiredPanelHeight` past a ceiling, or (B) a ceiling binding for some
other reason, cannot be determined without his screen's `visibleFrame.height` and
his actual entry text. The card does not pretend to resolve it: the RED gate below
either reproduces a divergence in the math or kills the card.

## Target behavior
- For every `NativeHistoryTurn`, at every `selectedTop` in {8, 39, 70},
  `NativePopoverSizing.requiredPanelHeight(for:selectedTop:)` is at most
  `DisplayManager.MAX_HEIGHT`, so `DisplayManager.clampHeight` never shrinks what
  the sizing math asks for.
- The question term is bounded and its surplus is scrollable, exactly as the
  answer term already is: a cap constant plus a real `ScrollView` bounded by
  `.frame(height:)`.
- The answer's existing behavior is unchanged: `maxAnswerViewportHeight` stays
  410, `answerViewportHeight(for:)` keeps its formula, and
  `DisplayMathTests.test_popoverSizingCapsLongAnswerViewport` passes untouched.
- The two caps stay inside the budget: with `pillReservedTop 38`, max
  `selectedTop 70`, popover chrome 62 and `panelBottomSlack 4`, the fixed overhead
  is 174; with the answer cap at 410 the question cap must be ≤ 216 for the sum to
  stay under `MAX_HEIGHT`. The new assertion pins that coupling so a future bump of
  either constant trips a test instead of silently reintroducing the defect.

## Affected contracts and files
- L5 surface `desktop/inherent-swift/InherentCard/NativeCardController.swift:739-789`
  (`NativePopoverSizing`) — add a `maxQuestionViewportHeight` constant and a
  `questionViewportHeight(for:)` returning `min(maxQuestionViewportHeight,
  questionHeight(for:))`, and have `popoverHeight(for:)` sum the viewport value
  instead of the raw `questionHeight`. Keep `questionHeight(for:)` as the
  uncapped content measure, mirroring `answerContentHeight` / `answerViewportHeight`.
- L5 surface `desktop/inherent-swift/InherentCard/NativeCardView.swift:513-538` —
  read `questionViewportHeight(for: turn)` alongside the existing
  `answerViewportHeight`, wrap the question `NativeSelectableText` (`:515-522`) in
  a `ScrollView(.vertical, showsIndicators: false)` and bound it with
  `.frame(height: questionViewportHeight)`. The `.padding(.bottom, 12)` and the
  divider `.overlay` (`:523-526`) must stay OUTSIDE the scroll region, on the
  ScrollView, or the divider scrolls away with the text.
- Test `desktop/inherent-swift/InherentCardTests/DisplayMathTests.swift` — the RED
  assertion and the cap assertion land here, next to
  `test_popoverSizingCapsLongAnswerViewport` (`:133-143`).

## Boundaries and non-goals
- Layers that may change: L5 surface, `desktop/inherent-swift/` only. No Python.
  The Python hermetic suite (1044 passed / 64 deselected) is not touched and not
  run; if the lane finds itself editing Python it must stop and report why.
- Must not change: `maxAnswerViewportHeight = 410` and `answerViewportHeight(for:)`;
  the 3-row chip strip cap (`NativeCardView.swift:300-305`, `:273-286`);
  `DisplayManager.MAX_HEIGHT = 800`; `DisplayManager.clampPanelHeight`;
  `applyHeight`'s top-edge anchor; the resizable card width; the auto-growing
  input; the selectable answer view; Cmd+Space and Esc.
- Non-goals, all real and all left alone so the owner's verdict attributes to one
  change: the height-tween cancellation (`NativeCardController.swift:209-214`), the
  hand-rolled 60Hz `Task.sleep` tween (`:254-272`), dead `pollLayout(for:)`
  (`:297-310`), `NativeSelectablePlainTextView`'s intrinsic-size ping-pong
  (`NativeCardView.swift:1303-1318`), and `launcher.py` staleness detection.
- **Adjacent work, verified non-colliding — do not wander into it.**
  `127.0.0.1:8006` appears only in `BridgeBackend.swift:50,252,271,313`,
  `RealtimeTransportV2.swift:27`, `InherentCardTests/SubmitRequestTests.swift:7,52,79`,
  plus two docs (`desktop/inherent-swift/README.md:18`,
  `UPSTREAM_BASELINE.md:35`). None is in the height chain. `launcher.py` is a
  separate top-level script. Neither is in scope.
- **Owner's machine is live. Do not touch:** the daemon on port 8009 with runtime
  root `/Users/alllllenshi/.jarvis-allen-test`; the running InherentCard; the
  worktree `.claude/worktrees/realtime-live-test`. Do not stop, restart, rebuild,
  check out, stash, or write into any of them. (Note: as of drafting,
  `git -C .claude/worktrees/realtime-live-test status --short` prints nothing and
  no `8009` appears in its Swift tree — the owner may re-edit it at any time, so
  the prohibition stands regardless.)
- **BUILD STALENESS TRAP.** `desktop/inherent-swift/launcher.py:34-37`
  `ensure_app_built()` returns early on `if APP_BIN.exists(): return`, despite the
  module docstring at `:4` claiming it builds "if the .app is missing or stale".
  Any visual confirmation of a Swift change therefore requires a FORCED manual
  rebuild, or the lane will look at the OLD binary and draw a false conclusion.

## Rejected approaches
- **Raise `DisplayManager.MAX_HEIGHT`.** Moves the cliff instead of removing it:
  the screen-derived ceiling
  (`floor(visibleFrame.height - 16 + 38)`, `DisplayManager.swift:26`) still binds,
  and a tall enough entry still loses content silently. Same class of error as
  raising a timeout to hide a truncation.
- **Make the popover body ONE scroll region bounded by the granted height.**
  Structurally cleaner — one cap, one inequality, no coupled constants — but
  rejected on the code as it stands. The answer's own `ScrollView`
  (`NativeCardView.swift:527-538`) fills most of the popover, so either it is
  removed (which rewrites `answerViewportHeight` and forces
  `test_popoverSizingCapsLongAnswerViewport` at `DisplayMathTests.swift:133-143` to
  be rewritten or deleted — weakening an existing test to land a fix), or it is
  nested inside the new outer scroll view, in which case the inner view eats the
  scroll wheel over the answer area and the outer region is reachable only over the
  thin question strip — reintroducing "cannot be scrolled" for the exact symptom
  reported. Also a strictly larger diff through working code.
- **Plumb the granted panel height into the view so the popover bounds itself by
  the screen-derived ceiling.** This is the only thing that establishes the
  screen-dependent half of the invariant, but it makes view height depend on panel
  height which depends on view height — a new feedback loop and new published
  state, for a ceiling that binds only when `visibleFrame.height < 762`. Out of
  scope; recorded so the next card inherits it rather than re-deriving it.
- **Reopen the disproved shift/reserve mechanism** from
  `docs/goals/inherent-history-tab-layout.md`. Measured dead: popover top sits at
  exactly `pillReservedTop + selectedTop`, 0pt surplus.
- **Extend `runPopoverScenario` (`NativeCardController.swift:594-617`) as the
  acceptance vehicle.** It is not a test. It is `private` production debug code in
  the `InherentCard` target, reached only by `runFakeTurns()`
  (`NativeCardController.swift:398-418`) behind
  `INHERENT_DEBUG_FAKE_TURNS=popover` (`:84-91`); it makes zero `XCTAssert` calls
  and no test target references it (`grep -rn "runPopoverScenario"
  desktop/inherent-swift/InherentCardTests/` → nothing). The earlier framing that
  "the harness cannot express the divergence" was about this debug scenario, not
  about the XCTest harness — `DisplayMathTests.swift` expresses it fine. Do not
  extend it just because a previous handoff said to.
- **A snapshot/PNG artifact as the acceptance gate.** The clipping leg is inferred
  platform behavior; a PNG reading is a human judgement call, not transcript
  evidence a judge can check. Owner follow-up instead.

## Acceptance evidence

- **Baseline first, never a baked number.** `bash scripts/test_inherent_swift.sh`
  run BEFORE any edit, raw tail shown, its printed `Executed N tests` quoted as the
  baseline of record. At lane/c tip dddbb50 the tree defines 178 `func test`
  methods (`grep -rh "  func test" desktop/inherent-swift/InherentCardTests/ | wc -l`
  → 178); the printed number wins.

- **RED FIRST — hard gate, no layout edit before it.** Add ONE assertion to
  `DisplayMathTests.swift` and run the suite with NO production change:

      func test_popoverSizingNeverAsksForMoreThanTheClampCanGrant() {
        let turn = NativeHistoryTurn(
          question: Array(repeating: "这是一个很长的历史问题，需要换很多行才能显示完整。",
                          count: 40).joined(),
          answer: Array(repeating: "这是一段用于撑高历史详情的内容。",
                        count: 80).joined(separator: "\n")
        )
        let required = NativePopoverSizing.requiredPanelHeight(for: turn, selectedTop: 70)
        XCTAssertEqual(DisplayManager.clampHeight(required), required)
      }

  This compares two production functions against a real invariant — no hand-picked
  expected constant, no object driving its own methods. It is deterministic:
  entry height is a pure function of injectable text through `textHeight`, and
  `clampHeight` (`DisplayManager.swift:12`) reads no screen. `selectedTop: 70` is
  the real maximum from `NativeCardModel.selectedPopoverTop`.
  **The raw failing output must appear in the transcript before any layout code is
  touched.** If it does NOT fail on today's code, the mechanism is wrong:
  **STOP AND REPORT to the hub, change nothing, do not re-diagnose in-lane.**

- **What that assertion does and does not prove — state this in the transcript.**
  It proves the sizing math asks for a window taller than the clamp will ever
  grant, so content the popover renders is outside the panel. It does NOT prove the
  on-screen "clipped, not scrollable" pixel behavior; that leg rests on the
  inferred layer-backed AppKit clipping at `NativeCardController.swift:43` and is
  confirmed by the owner's eyes, not by this run.

- **GREEN, and the view-layer value.** After the fix, the RED assertion passes, and
  a SECOND test method — `test_popoverSizingCapsLongQuestionViewport`, named and
  shaped after the existing `test_popoverSizingCapsLongAnswerViewport`
  (`DisplayMathTests.swift:133-143`) — lands on the number the view actually hands
  to `.frame(height:)`:

      XCTAssertEqual(
        NativePopoverSizing.questionViewportHeight(for: turn),
        NativePopoverSizing.maxQuestionViewportHeight
      )

  `NativeCardView.swift` must read `questionViewportHeight(for: turn)` for its
  `.frame(height:)`, exactly as `:513`/`:538` do for the answer, so this is the
  view's real input. Nothing deeper is reachable: `NativeCardController` and
  `NativeCardView` have ZERO references anywhere in
  `desktop/inherent-swift/InherentCardTests/` (`grep -rn
  "NativeCardController\|NativeCardView" desktop/inherent-swift/InherentCardTests/`
  → no output) and are not constructible headlessly.

- **Load-bearing proof.** On the finished tree, revert only the production edit
  (keep the tests), rerun `bash scripts/test_inherent_swift.sh`, and show the RED
  assertion failing again; then restore the fix and show it green.

- **Regression.** `bash scripts/test_inherent_swift.sh` raw output, final
  `Executed M tests` quoted, `M = N + 2`. No existing test deleted or weakened;
  `test_popoverSizingCapsLongAnswerViewport`,
  `test_popoverSizingUsesContentHeightForShortAnswer` and
  `test_popoverSizingExtendsPanelBelowOffsetPopover` pass unchanged.

- **Live run: not required.** The behavior is pure layout arithmetic, no LLM and no
  external runtime.

## Docs to sync
- **none.** Greps run and their results:
  `grep -c -i "popover" docs/spec.html` → `0`.
  `grep -rn -i "MAX_HEIGHT\|clampPanelHeight\|window height\|panel height"
  docs/spec.html docs/adr/` → no output.
  `grep -rn -i "popover" docs/adr/` → one hit,
  `docs/adr/0014-inherent-realtime-ux.md:2055`, a definition-of-done line saying
  the history popover retains its "current user-visible behavior" — not a layout
  rule, and un-truncating the popover is that behavior retained, not a contract
  change. Positively, `docs/spec.html:1100` (§3.6.7 Inherent boundaries) assigns
  "layout、scroll、expanded rows、selected tab、animation state" to Inherent as
  local UI state that carries no truth — the spec deliberately does not own this
  fact. Both grep outputs must be shown in the transcript either way. If the fix
  ends up publishing an externally visible number (e.g. a documented maximum
  popover height), the canonical owner is ADR-0014 §Step 0 and it is updated then.

## Open questions
(none)

## /goal condition
Done when the transcript shows ALL of the following:

1. BASELINE: raw output of `bash scripts/test_inherent_swift.sh` run BEFORE any
   edit, with its final `Executed N tests` line quoted. N is the baseline of
   record; the tree defines 178 `func test` methods at lane/c tip dddbb50, but the
   printed number wins — never a baked one.
2. RED FIRST, and this is a hard gate. A single new assertion is added to
   `desktop/inherent-swift/InherentCardTests/DisplayMathTests.swift` with NO
   production change, comparing `NativePopoverSizing.requiredPanelHeight(for:
   longQuestionTurn, selectedTop: 70)` against `DisplayManager.clampHeight` of the
   same value, and the RAW FAILING OUTPUT of `bash scripts/test_inherent_swift.sh`
   is shown BEFORE any layout code is touched.
   EXIT CLAUSE: if that assertion does NOT fail on today's code, the mechanism is
   wrong — the run STOPS AND REPORTS TO THE HUB, changes nothing, re-diagnoses
   nothing in-lane, and edits no layout code.
3. SCOPE STATEMENT: the transcript states plainly that the assertion proves the
   sizing math asks for a taller window than the clamp will grant, and does NOT
   prove the on-screen "clipped, not scrollable" pixel behavior, which rests on
   inferred layer-backed AppKit clipping (`NativeCardController.swift:43`).
4. FIX: the question term gets a cap constant plus a real `ScrollView` bounded by
   `.frame(height:)`, mirroring the answer at `NativeCardView.swift:513`/`:527-538`.
   `NativeCardView` reads the new `questionViewportHeight(for:)` for that frame.
   The divider overlay stays outside the scroll region.
5. GREEN: raw output of `bash scripts/test_inherent_swift.sh` with the RED
   assertion now passing, plus a SECOND test method
   `test_popoverSizingCapsLongQuestionViewport` asserting
   `NativePopoverSizing.questionViewportHeight(for: turn)` equals
   `NativePopoverSizing.maxQuestionViewportHeight` for an over-tall entry.
6. LOAD-BEARING PROOF: the production edit alone is reverted (tests kept), the
   suite is rerun, the RED assertion is shown failing again, then the fix is
   restored and shown green.
7. REGRESSION: `bash scripts/test_inherent_swift.sh` raw output, final
   `Executed M tests` quoted with `M = N + 2`. No existing test deleted or
   weakened; `test_popoverSizingCapsLongAnswerViewport`,
   `test_popoverSizingUsesContentHeightForShortAnswer` and
   `test_popoverSizingExtendsPanelBelowOffsetPopover` still pass.
8. UNCHANGED: `maxAnswerViewportHeight = 410`, `answerViewportHeight(for:)`,
   `DisplayManager.MAX_HEIGHT`, `clampPanelHeight`, the 3-row chip strip cap, and
   every item under the card's Non-goals — or the run stopped and reported.
9. SCOPE: no file outside `desktop/inherent-swift/` is modified; no Python file is
   touched; the daemon on port 8009, the running InherentCard, and the worktree
   `.claude/worktrees/realtime-live-test` are never stopped, restarted, rebuilt,
   checked out, stashed, or written into.
10. `git status` shows a clean tree; each slice committed with the commit skill;
    the card's Progress section updated with sha + one evidence line per slice.
11. DOCUMENTATION RULE: each entry under "Docs to sync" was updated or explicitly
    judged unchanged, with the grep output that proves it. When the implementation
    changes a documented contract, invariant, ownership boundary, or externally
    relevant behavior, update the canonical document that owns that fact; do not
    document what the code already makes clear; do not duplicate a fact across
    documents.

Or stop after 30 turns.

## Progress
- Owner follow-up (NOT a blocker, NOT an acceptance item): after a FORCED manual
  rebuild — `cd desktop/inherent-swift && xcodegen generate && xcodebuild -project
  InherentCard.xcodeproj -scheme InherentCard -configuration Debug -derivedDataPath
  build build` ending in `** BUILD SUCCEEDED **`, because
  `launcher.py:34-37` only builds when the binary is MISSING — the owner opens a
  history entry taller than his card and confirms the text is reachable by
  scrolling rather than gone. Record his words verbatim here when he answers.

