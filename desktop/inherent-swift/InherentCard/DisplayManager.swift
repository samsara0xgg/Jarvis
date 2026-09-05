import AppKit

enum DisplayManager {
  static let CARD_WIDTH: CGFloat = 360
  static let CARD_MARGIN: CGFloat = 16
  static let PILL_RESERVED_TOP: CGFloat = 38
  static let MIN_HEIGHT: CGFloat = 60
  static let MAX_HEIGHT: CGFloat = 800
  static let MAX_WIDTH: CGFloat = 900

  static func clampHeight(_ h: CGFloat) -> CGFloat { min(max(MIN_HEIGHT, ceil(h)), MAX_HEIGHT) }
  static func clampWidth(_ w: CGFloat) -> CGFloat { min(max(CARD_WIDTH, ceil(w)), MAX_WIDTH) }

  static func clampPanelHeight(_ h: CGFloat, on screen: NSScreen?) -> CGFloat {
    guard let visibleFrame = (screen ?? NSScreen.main)?.visibleFrame else {
      return clampHeight(h)
    }
    let available = max(MIN_HEIGHT, visibleFrame.height - CARD_MARGIN + PILL_RESERVED_TOP)
    return min(clampHeight(h), floor(available))
  }

  /// Adjusts panel height while keeping the top edge fixed. In Cocoa coords,
  /// that means origin.y moves as height changes while maxY stays stable.
  static func applyHeight(to bounds: NSRect, newHeight: CGFloat) -> NSRect {
    // Anchor the visible top edge: keep maxY fixed and let bottom (origin.y)
    // move so the card grows DOWN and collapses UP. The chips strip + answer
    // body are stacked downward in the DOM, so anchoring the top makes height
    // changes feel like they grow/shrink "from below" — collapse animations
    // see the bottom edge rise toward the input row, which matches the user's
    // mental model of newer chips disappearing first into the strip below.
    let topY = bounds.maxY
    return NSRect(x: bounds.origin.x, y: topY - newHeight, width: bounds.width, height: newHeight)
  }

  /// Adjusts panel width by moving x leftward so the right edge stays fixed.
  /// Used for the popover slide-in: window widens leftward while the card's
  /// visible right edge stays pinned.
  static func applyWidth(to bounds: NSRect, newWidth: CGFloat) -> NSRect {
    let rightEdge = bounds.maxX
    return NSRect(x: rightEdge - newWidth, y: bounds.origin.y, width: newWidth, height: bounds.height)
  }

  static func cursorScreen() -> NSScreen? {
    let cursor = NSEvent.mouseLocation
    return NSScreen.screens.first { $0.frame.contains(cursor) } ?? NSScreen.main
  }

  static func topRightFrame(on screen: NSScreen, width: CGFloat, height: CGFloat) -> NSRect {
    let v = screen.visibleFrame
    return NSRect(
      x: v.maxX - width - CARD_MARGIN,
      y: v.maxY - height - CARD_MARGIN + PILL_RESERVED_TOP * 2,
      width: width,
      height: height
    )
  }
}

extension DisplayManager {
  static func indexOfFrameContaining(point: CGPoint, frames: [CGRect]) -> Int {
    if let i = frames.firstIndex(where: { $0.contains(point) }) { return i }
    return 0  // fall back to primary
  }
}
