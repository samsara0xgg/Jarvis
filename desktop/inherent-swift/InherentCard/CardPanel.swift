import AppKit

// MARK: - State ownership contract
//
// Each panel state attribute below has a SINGLE owner. Past flash bugs
// (panel jump, cursor cycle, fade race) all came from multiple paths
// changing the same attribute. Future changes MUST respect this table.
//
//   alphaValue
//     Sole owner: FadeController. ALL visibility changes — instant
//     (showInstant / hideInstant) and animated (fadeOut / restore) — go
//     through it so they share one generation-counter cancellation model.
//     No other code path may assign panel.alphaValue.
//
//   ignoresMouseEvents
//     Primary owner: NativeCardController.updatePassthrough (cursor-based, per
//     mousemove). Secondary: setHidden forces true on hide (the passthrough
//     monitor exits early when userHidden=true, so something has to claim
//     hidden state). Drag forces false during a drag gesture. No other code
//     path may set this.
//
//   frame.size.width
//     Owner: this init only. Width is FIXED at 678 for the panel's lifetime.
//     popover visibility is now a NativeCardModel flag in NativeCardController,
//     not a setFrame call.
//
//   frame.size.height
//     Owner: NativeCardController.updatePanelHeight via DisplayManager.applyHeight
//     (top-anchored). No other code path may setFrame the height.
//
//   frame.origin
//     Owners: anchorTopRight (init / hotkey relocate) + case "movePanel"
//     (drag) + case "resetPosition". All preserve the right-anchor invariant.
//
// Why this matters: InherentCard is a SwiftUI view hosted in a native panel.
// SwiftUI layout, CALayer alpha animation, and NSWindow setFrame all have
// independent timing paths.
// When two paths both write the same attribute, they race → user-visible
// flash. This table is our discipline for staying out of those races.

final class CardPanel: NSPanel {
  override var canBecomeKey: Bool { true }
  override var canBecomeMain: Bool { false }

  /// Passive show paths (siri:open) only order the panel front. Explicit input
  /// paths (hotkey, click, paste/drop) make it key, and at that point the app
  /// must activate or macOS keeps routing keystrokes to the previous app.
  override func becomeKey() {
    super.becomeKey()
    NSLog("[panel] becomeKey: isKey=\(isKeyWindow) appActive=\(NSApp.isActive)")
    if !NSApp.isActive {
      NSApp.activate(ignoringOtherApps: true)
    }
  }

  override func resignKey() {
    super.resignKey()
    NSLog("[panel] resignKey: appActive=\(NSApp.isActive)")
  }

  init() {
    // Width is FIXED at 678 (= 360 card + 18 gap + 300 popover slot) for the
    // panel's lifetime. Right-anchored layout means the visible card sits at
    // panel.x + 318, so card visual position is identical to a 360-wide panel
    // anchored at the same right edge. The popover slot to the left of the
    // card is transparent and click-through (passthrough monitor excludes it
    // when popoverActive=false). Why fixed: any setFrame width change forced
    // an instant panel jump that webview-internal CSS layout could not
    // synchronize with → the long-running "popover hide flash". Keeping width
    // constant eliminates that flash class entirely.
    super.init(
      contentRect: NSRect(x: 0, y: 0, width: 678, height: 120),
      styleMask: [.borderless, .resizable],
      backing: .buffered,
      defer: false
    )
    self.level = .floating
    self.collectionBehavior = [
      .canJoinAllSpaces,
      .fullScreenAuxiliary,
      .stationary,  // pinned during Expose/Mission Control — the reason this is Swift, not Electron
    ]
    self.isOpaque = false
    self.backgroundColor = .clear
    self.hasShadow = false
    self.isMovableByWindowBackground = false
    self.isReleasedWhenClosed = false
    self.hidesOnDeactivate = false
    self.acceptsMouseMovedEvents = true
  }

  func anchorTopRight(of screen: NSScreen, cardMargin: CGFloat = 16, pillReservedTop: CGFloat = 38) {
    let v = screen.visibleFrame
    // Cocoa origin is bottom-left; visibleFrame.maxY is the top of the work area
    // (just below the menu bar). The window contains a 38px transparent pill
    // region above the visible card (matches `body { padding-top: 38px }` in
    // card.css). Adding `+pillReservedTop` moves the window UP so the visible
    // card sits cardMargin below the work-area top — the window itself extends
    // into the menu-bar zone to host the pill, but the visible content does not.
    let target = NSRect(
      x: v.maxX - frame.width - cardMargin,
      y: v.maxY - frame.height - cardMargin + pillReservedTop * 2,
      width: frame.width,
      height: frame.height
    )
    setFrame(target, display: true, animate: false)
  }
}

// Note: setHidden / setVisible used to live here as direct alpha mutators.
// They were moved to FadeController as showInstant() / hideInstant() so the
// generation-counter cancellation guards every alpha change uniformly. The
// old "orderOut re-anchors NSPanels to a home Space" finding from the
// Electron port still applies — neither showInstant nor hideInstant calls
// orderOut; visibility is purely alpha + ignoresMouseEvents.
