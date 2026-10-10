import XCTest
@testable import Jarvis

/// Close up, a name grows with its star's perspective scale in half points, and the more it needs him the larger and brighter it is.
final class NearNameStyleTests: XCTestCase {
    private let scales = stride(from: 0.4, through: 8, by: 0.05).map { $0 }

    func testSizeGrowsTowardYouInHalfPointsWithinBounds() {
        for r in -1...3 {
            var last = 0.0
            for ps in scales {
                let n = StarEngine.nameStyle(r, ps).size
                XCTAssertGreaterThanOrEqual(n, last, "rank \(r) ps \(ps)")
                XCTAssertTrue((9...20).contains(n), "rank \(r) ps \(ps)")
                XCTAssertEqual((n * 2).rounded(), n * 2, "rank \(r) ps \(ps)")
                last = n
            }
        }
    }

    func testTheSizeVisiblyChangesAcrossTheScreen() {
        // on screen PS runs from about 1.1 at the horizon to 6 at the camera
        for r in -1...3 { XCTAssertGreaterThanOrEqual(StarEngine.nameStyle(r, 6).size - StarEngine.nameStyle(r, 1.4).size, 5, "rank \(r)") }
    }

    func testTheMoreImportantIsNeverSmallerOrDimmer() {
        for ps in scales {
            for r in -1..<3 {
                let a = StarEngine.nameStyle(r, ps), b = StarEngine.nameStyle(r + 1, ps)
                XCTAssertGreaterThanOrEqual(a.size, b.size, "rank \(r) ps \(ps)")
                XCTAssertGreaterThanOrEqual(a.alpha, b.alpha, "rank \(r) ps \(ps)")
                XCTAssertGreaterThanOrEqual(a.weight, b.weight, "rank \(r) ps \(ps)")
            }
        }
    }

    func testFarNamesAreDimmerButStay() {
        for r in -1...3 {
            var last = 0.0
            for ps in scales {
                let a = StarEngine.nameStyle(r, ps).alpha
                XCTAssertGreaterThanOrEqual(a, last, "rank \(r) ps \(ps)")
                XCTAssertGreaterThanOrEqual(a, 0.4, "rank \(r) ps \(ps)")
                XCTAssertLessThanOrEqual(a, 1, "rank \(r) ps \(ps)")
                last = a
            }
        }
    }
}
