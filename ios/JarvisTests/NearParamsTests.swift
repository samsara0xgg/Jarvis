import XCTest
@testable import Jarvis

/// The near-view tuning panel's numbers (spec-1d): minutes <-> radians, and what survives a relaunch.
final class NearParamsTests: XCTestCase {
    func testGaAndTaShowAsMinutesAroundTheFocus() {
        var n = NearParams()
        XCTAssertEqual(n.gaMin, 46, accuracy: 0.5)
        XCTAssertEqual(n.taMin, 179, accuracy: 0.5)
        n.gaMin = 60; n.taMin = 150
        XCTAssertEqual(n.ga, Double.pi / 2 - 60 * 2 * Double.pi / 1440, accuracy: 1e-9)
        XCTAssertEqual(n.ta, Double.pi / 2 + 150 * 2 * Double.pi / 1440, accuracy: 1e-9)
        XCTAssertEqual(n.gaMin, 60, accuracy: 1e-9)
    }

    func testAnOverrideRoundTripsAndDeletingItRestoresTheDefaults() throws {
        let d = try XCTUnwrap(UserDefaults(suiteName: "NearParamsTests"))
        d.removePersistentDomain(forName: "NearParamsTests")
        XCTAssertEqual(NearParams.load(d), NearParams())
        var n = NearParams(); n.gh = 90; n.taMin = 150
        n.save(d)
        XCTAssertNotNil(d.data(forKey: NearParams.key))
        XCTAssertEqual(NearParams.load(d), n)
        NearParams().save(d)      // 恢复默认 removes the key
        XCTAssertNil(d.data(forKey: NearParams.key))
        XCTAssertEqual(NearParams.load(d), NearParams())
    }
}
