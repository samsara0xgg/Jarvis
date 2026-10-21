import XCTest
import MapKit
@testable import Jarvis

/// The footprint map's pure pieces: the colour of each point, which points are lit at a held minute, and the frame that covers everything.
final class FootprintMapTests: XCTestCase {
    private func pt(_ ms: Int64, _ v: Double?) -> FootprintMapPoint {
        FootprintMapPoint(coord: CLLocationCoordinate2D(latitude: -43.5, longitude: 172.2), ms: ms, speedMps: v)
    }

    func testPointColorsFollowTheRampAndCarryAMissingSpeed() {
        let c = FootprintMapMath.pointColors([pt(0, nil), pt(1, 0), pt(2, 25 / 3.6), pt(3, nil), pt(4, 100)])
        XCTAssertEqual(c[0], FootprintPaint.ramp[0].rgb)        // nothing known before the first speed: it takes the first known (0)
        XCTAssertEqual(c[1], FootprintPaint.ramp[0].rgb)
        XCTAssertEqual(c[2], FootprintPaint.ramp[1].rgb)
        XCTAssertEqual(c[3], c[2])                              // no speed here: the one before
        XCTAssertEqual(c[4], FootprintPaint.ramp[3].rgb)        // 80 and above holds the last stop
    }

    func testLitCountSplitsByTime() {
        let pts = [pt(10, 1), pt(20, 1), pt(30, 1), pt(40, 1)]
        XCTAssertEqual(FootprintMapMath.litCount(pts, ms: 5), 0)
        XCTAssertEqual(FootprintMapMath.litCount(pts, ms: 20), 2)
        XCTAssertEqual(FootprintMapMath.litCount(pts, ms: 25), 2)
        XCTAssertEqual(FootprintMapMath.litCount(pts, ms: 999), 4)
    }

    func testFitCoversRoutesPlacesAndNow() {
        let route = FootprintMapRoute(id: "r", points: [pt(0, 1), pt(1, 1)], gaps: [])
        let far = CLLocationCoordinate2D(latitude: -43.6, longitude: 172.5)
        let view = FootprintMapView(routes: [route], places: [FootprintMapPlace(id: "p", coord: far, discRadius: 0, tint: .white, pin: .place, label: nil)],
                                    now: FootprintMapNow(coord: CLLocationCoordinate2D(latitude: -43.4, longitude: 172.1), label: ""))
        let r = PlaceFit.region(view.fitCoordinates)
        XCTAssertEqual(view.fitCoordinates.count, 4)
        for c in view.fitCoordinates {
            XCTAssertLessThanOrEqual(abs(c.latitude - r.center.latitude), r.span.latitudeDelta / 2)
            XCTAssertLessThanOrEqual(abs(c.longitude - r.center.longitude), r.span.longitudeDelta / 2)
        }
        XCTAssertGreaterThanOrEqual(PlaceFit.region([far]).span.latitudeDelta, PlaceFit.minSpan)      // a single point still gets a few streets
    }
}
