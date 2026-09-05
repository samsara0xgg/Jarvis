import XCTest
@testable import InherentCard

final class ReconnectBackoffTests: XCTestCase {
  func test_backoffSequence() {
    var b = ReconnectBackoff()
    XCTAssertEqual(b.next(), 1.0)
    XCTAssertEqual(b.next(), 2.0)
    XCTAssertEqual(b.next(), 4.0)
    XCTAssertEqual(b.next(), 8.0)
    XCTAssertEqual(b.next(), 16.0)
    XCTAssertEqual(b.next(), 16.0, "must cap at 16s")
    XCTAssertEqual(b.next(), 16.0)
  }

  func test_resetReturnsToFirst() {
    var b = ReconnectBackoff()
    _ = b.next(); _ = b.next(); _ = b.next()
    b.reset()
    XCTAssertEqual(b.next(), 1.0)
  }
}
