import XCTest
@testable import InherentCard

final class FadeControllerTests: XCTestCase {
  func test_incrementReturnsMonotonic() {
    let g = FadeGeneration()
    XCTAssertEqual(g.next(), 1)
    XCTAssertEqual(g.next(), 2)
    XCTAssertEqual(g.next(), 3)
  }

  func test_isCurrent_returnsTrueOnlyForLatest() {
    let g = FadeGeneration()
    let first = g.next()
    XCTAssertTrue(g.isCurrent(first))
    _ = g.next()
    XCTAssertFalse(g.isCurrent(first), "old generation must be superseded after next()")
  }

  func test_cancelInvalidatesAll() {
    let g = FadeGeneration()
    let token = g.next()
    g.cancel()
    XCTAssertFalse(g.isCurrent(token))
  }
}
