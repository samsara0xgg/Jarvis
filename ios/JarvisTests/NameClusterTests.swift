import XCTest
@testable import Jarvis

/// Close up, stars within 14 points of each other share one name, the first of them (by placing order) leading.
final class NameClusterTests: XCTestCase {
    func testNearbyStarsJoinTheFirstOne() {
        let g = nameClusters([(100, 100), (108, 106), (300, 300), (110, 100), (305, 304)], 14)
        XCTAssertEqual(g, [[0, 1, 3], [2, 4]])
    }

    func testAlonePointsStandAlone() {
        XCTAssertEqual(nameClusters([(0, 0), (20, 0)], 14), [[0], [1]])
    }
}
