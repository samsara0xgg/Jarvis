import XCTest
@testable import Jarvis

/// The near view settled (q = -1, z = 1, no lens): where the lanes and the scale ring land on the screen (spec-1 section 1).
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
            (R_YOU, 0, 220.6, 460.4, 3.6), (R_YOU, 60, 201.1, 360.5, 2.6), (R_YOU, 120, 210.0, 300.8, 2.1),
            (R_YOU, 240, 262.2, 239.4, 1.5), (R_YOU, 360, 336.0, 215.6, 1.3),
            (R_HER, 0, 356.7, 412.2, 3.1), (R_SC, 0, 83.5, 509.0, 4.0), (R_SC, 120, 139.1, 292.3, 2.0),
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
