import XCTest
@testable import InherentCard

final class DisplayManagerLookupTests: XCTestCase {
  func test_findScreenContainingPoint_first() {
    let a = NSRect(x: 0,    y: 0, width: 1920, height: 1080)
    let b = NSRect(x: 1920, y: 0, width: 1920, height: 1080)
    let cursor = CGPoint(x: 100, y: 100)
    let frames = [a, b]
    let idx = DisplayManager.indexOfFrameContaining(point: cursor, frames: frames)
    XCTAssertEqual(idx, 0)
  }

  func test_findScreenContainingPoint_second() {
    let a = NSRect(x: 0,    y: 0, width: 1920, height: 1080)
    let b = NSRect(x: 1920, y: 0, width: 1920, height: 1080)
    let cursor = CGPoint(x: 2500, y: 500)
    XCTAssertEqual(DisplayManager.indexOfFrameContaining(point: cursor, frames: [a, b]), 1)
  }

  func test_findScreenContainingPoint_offScreenFallsBackToFirst() {
    let a = NSRect(x: 0, y: 0, width: 1920, height: 1080)
    let cursor = CGPoint(x: 5000, y: 5000)
    XCTAssertEqual(DisplayManager.indexOfFrameContaining(point: cursor, frames: [a]), 0)
  }
}
