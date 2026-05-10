import XCTest
@testable import InherentCard

final class DisplayMathTests: XCTestCase {
  func test_resizeKeepsTopRightAnchor() {
    let original = NSRect(x: 100, y: 200, width: 360, height: 120)
    let resized = DisplayManager.applyHeight(to: original, newHeight: 200)
    XCTAssertEqual(resized.width, 360)
    XCTAssertEqual(resized.height, 200)
    XCTAssertEqual(resized.maxX, original.maxX, "right edge must stay fixed")
    // Height is top-anchored: maxY stays fixed and origin.y moves down when
    // the panel grows, matching the DOM stack expanding downward from the row.
    XCTAssertEqual(resized.maxY, original.maxY)
    XCTAssertEqual(resized.origin.y, original.maxY - resized.height)
  }

  func test_setWidthAnchorsRightEdge() {
    let original = NSRect(x: 100, y: 200, width: 360, height: 120)
    let widened = DisplayManager.applyWidth(to: original, newWidth: 678)
    XCTAssertEqual(widened.maxX, original.maxX, "right edge must stay fixed when widening")
    XCTAssertEqual(widened.width, 678)
    XCTAssertEqual(widened.origin.y, original.origin.y)
    XCTAssertEqual(widened.height, original.height)
  }

  func test_clampHeight() {
    XCTAssertEqual(DisplayManager.clampHeight(50), 60)   // floor
    XCTAssertEqual(DisplayManager.clampHeight(900), 800) // ceil
    XCTAssertEqual(DisplayManager.clampHeight(200), 200)
  }

  func test_clampWidth() {
    XCTAssertEqual(DisplayManager.clampWidth(100), 360)  // floor (CARD_WIDTH)
    XCTAssertEqual(DisplayManager.clampWidth(1000), 900) // ceil
    XCTAssertEqual(DisplayManager.clampWidth(500), 500)
  }

  func test_hitRegionsMatchFixedPanelGeometry() {
    let panel = NSRect(x: 100, y: 200, width: 678, height: 204)
    let regions = NativeCardHitTest.regions(for: panel)

    XCTAssertEqual(regions.card, NSRect(x: 418, y: 200, width: 360, height: 166))
    XCTAssertEqual(regions.popover, NSRect(x: 100, y: 200, width: 300, height: 166))
    XCTAssertEqual(regions.pill, NSRect(x: 530, y: 366, width: 136, height: 35))
  }

  func test_hitTestKeepsTransparentPopoverSlotClickThrough() {
    let panel = NSRect(x: 100, y: 200, width: 678, height: 204)

    XCTAssertFalse(NativeCardHitTest.shouldIgnoreMouse(
      at: NSPoint(x: 600, y: 250),
      panelFrame: panel,
      popoverVisible: false
    ))
    XCTAssertFalse(NativeCardHitTest.shouldIgnoreMouse(
      at: NSPoint(x: 598, y: 383),
      panelFrame: panel,
      popoverVisible: false
    ))
    XCTAssertTrue(NativeCardHitTest.shouldIgnoreMouse(
      at: NSPoint(x: 250, y: 250),
      panelFrame: panel,
      popoverVisible: false
    ))
    XCTAssertFalse(NativeCardHitTest.shouldIgnoreMouse(
      at: NSPoint(x: 250, y: 250),
      panelFrame: panel,
      popoverVisible: true
    ))
    XCTAssertTrue(NativeCardHitTest.shouldIgnoreMouse(
      at: NSPoint(x: 409, y: 250),
      panelFrame: panel,
      popoverVisible: true
    ))
  }

  func test_hitTestHonorsRoundedCardCorners() {
    let panel = NSRect(x: 100, y: 200, width: 678, height: 204)

    XCTAssertTrue(NativeCardHitTest.shouldIgnoreMouse(
      at: NSPoint(x: 419, y: 201),
      panelFrame: panel,
      popoverVisible: false
    ))
    XCTAssertFalse(NativeCardHitTest.shouldIgnoreMouse(
      at: NSPoint(x: 448, y: 230),
      panelFrame: panel,
      popoverVisible: false
    ))
  }
}
