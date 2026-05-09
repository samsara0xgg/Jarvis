import XCTest
@testable import InherentCard

final class DisplayMathTests: XCTestCase {
  func test_resizeKeepsTopRightAnchor() {
    let original = NSRect(x: 100, y: 200, width: 360, height: 120)
    let resized = DisplayManager.applyHeight(to: original, newHeight: 200)
    XCTAssertEqual(resized.width, 360)
    XCTAssertEqual(resized.height, 200)
    XCTAssertEqual(resized.maxX, original.maxX, "right edge must stay fixed")
    // y is the bottom in Cocoa coords; growing height keeps the existing y the
    // same, which means the top moves UP. That matches the existing card.css
    // expectation (card grows downward visually as content arrives).
    XCTAssertEqual(resized.origin.y, original.origin.y)
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
}
