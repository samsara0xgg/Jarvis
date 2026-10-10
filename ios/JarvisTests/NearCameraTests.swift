import XCTest
@testable import Jarvis

/// The near view settled (q = -1, z = 1, no lens): where the lanes and the scale ring land on the screen (spec-1c section 1).
final class NearCameraTests: XCTestCase {
    private func settled() -> StarEngine {
        let e = StarEngine()
        e.F = 1000; e.z = 1; e.k = TAU / DAY; e.lensM = 1
        e.setCam(1)
        return e
    }

    func testTheCameraPutsEachPointOfTheDialWhereTheSpecSays() {
        let e = settled()
        let table: [(Double, Double, Double, Double, Double)] = [
            (R_YOU, 0, 199.9, 472.3, 6.0), (R_HER, 0, 417.8, 402.8, 4.6), (R_SC + 9, 60, 63.2, 338.7, 3.3),
        ]
        for (r, dm, x, y, ps) in table {
            XCTAssertTrue(e.P(r, e.ang(e.F + dm)))
            XCTAssertEqual(e.PX, x, accuracy: 1, "x of r \(r) at F+\(dm) min")
            XCTAssertEqual(e.PY, y, accuracy: 1, "y of r \(r) at F+\(dm) min")
            XCTAssertEqual(e.PS, ps, accuracy: 0.05, "PS of r \(r) at F+\(dm) min")
        }
    }

    func testUnPGivesBackWhatTheCameraProjected() throws {
        let e = settled()
        XCTAssertTrue(e.P(R_YOU, e.ang(e.F + 120)))
        let u = try XCTUnwrap(e.unP(e.PX, e.PY))
        XCTAssertEqual(u.1, R_YOU, accuracy: 1)
        XCTAssertEqual(wrapAngle(u.0 - e.ang(e.F + 120)), 0, accuracy: 0.01)
    }
}
