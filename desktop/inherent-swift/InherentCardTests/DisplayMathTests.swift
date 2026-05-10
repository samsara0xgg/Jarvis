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
}
