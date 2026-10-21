import XCTest
@testable import Jarvis

/// The outing page's curves: where a time and a value fall, where the line breaks, the stops it stands still in, the ticks and the colours of a leg.
final class FootprintCurvesTests: XCTestCase {
    private func point(_ s: Int, _ mps: Double?) -> FootprintPoint { FootprintPoint(at_ms: Int64(s) * 1000, lat: -43.5, lng: 172.2, speed_mps: mps) }
    private func stay(_ id: String, _ a: Int, _ b: Int) -> FootprintStay {
        FootprintStay(id: id, start_ms: Int64(a) * 1000, end_ms: Int64(b) * 1000, lat: -43.5, lng: 172.2, place: nil, kept_place: nil)
    }
    private func make(_ start: Int64, _ end: Int64, stops: [FootprintStay] = [], gaps: [FootprintGap] = [], heart: FootprintHeart? = nil, points: [FootprintPoint] = []) -> FootprintOuting {
        FootprintOuting(id: "outing-\(start)", start_ms: start, end_ms: end, from_stay: nil, to_stay: nil, activity: nil, distance_m: 1000, moving_ms: 1000, max_speed_mps: nil,
                        stops: stops, gaps: gaps, heart_rate: heart, points: points)
    }
    /// 0 to 100 s; a stop from 30 to 50 s, a gap from 70 to 80 s
    private func outing(heart: FootprintHeart? = nil) -> FootprintOuting {
        let pts = [point(0, 0), point(10, 10), point(20, 20), point(25, 15), point(35, 1), point(45, 2), point(55, 12), point(70, 20), point(80, nil), point(100, 5)]
        return make(0, 100_000, stops: [stay("s", 30, 50)], gaps: [FootprintGap(start_ms: 70_000, end_ms: 80_000)], heart: heart, points: pts)
    }

    func testTimeAndXAreInversesAndClamped() {
        let c = FootprintCurves(outing(), endMs: 100_000)
        XCTAssertEqual(c.x(50_000), 190, accuracy: 1e-9)
        XCTAssertEqual(c.x(-5), 0)
        XCTAssertEqual(c.x(999_999), 380)
        XCTAssertEqual(c.ms(x: 95), 25_000)
        XCTAssertEqual(c.ms(x: -40), 0)
        XCTAssertEqual(c.ms(x: 500), 100_000)
    }

    func testTheLineBreaksInAStopAndAtTheGapAndNoLineStandsForTheStop() {
        let c = FootprintCurves(outing(), endMs: 100_000)
        XCTAssertEqual(c.bands, [FootprintCurves.Band(number: 1, startMs: 30_000, endMs: 50_000)])
        XCTAssertNil(c.kmh(at: 40_000))                                        // the points inside the stop are gone and no zero line crosses it
        XCTAssertEqual(c.kmh(at: 30_000), 0)                                   // the line comes down to zero as the stop begins
        XCTAssertEqual(c.kmh(at: 50_000), 0)                                   // and sets off from zero as it ends
        XCTAssertEqual(c.kmh(at: 25_000) ?? 0, 15 * 3.6, accuracy: 1e-9)
        XCTAssertEqual(c.runs.count, 3, "before the stop, after it up to the gap, after the gap")
        XCTAssertEqual(c.runs[0].last?.ms, 30_000)
        XCTAssertEqual(c.runs[1].first?.ms, 50_000)
        XCTAssertEqual(c.bridges.count, 1)
        XCTAssertEqual([c.bridges[0].from.ms, c.bridges[0].to.ms], [70_000, 80_000])
        XCTAssertNil(c.kmh(at: 75_000))                                        // across a gap there is no speed
        XCTAssertEqual(c.kmh(at: 80_000) ?? 0, 20 * 3.6, accuracy: 1e-9)       // no speed at a point: the one before
    }

    func testTheTopFitsTheFastestAndTheRampGoesUpTheHeight() {
        let c = FootprintCurves(outing(), endMs: 100_000)
        XCTAssertEqual(c.top, 20 * 3.6 * 1.185, accuracy: 1e-9)
        XCTAssertEqual(c.y(kmh: 0), 64)
        XCTAssertEqual(c.y(kmh: 1000), 0)
        let stops = c.rampStops()
        XCTAssertEqual(stops.first?.loc, 0)
        XCTAssertEqual(stops.last?.loc, 1)
        XCTAssertEqual(stops.last?.rgb, FootprintPaint.speed(kmh: c.top))
        XCTAssertEqual(stops.map(\.loc), stops.map(\.loc).sorted())
        XCTAssertEqual(c.gridKmh, [40, 80])
        XCTAssertEqual(stops[1].rgb, FootprintPaint.ramp[1].rgb)
    }

    func testHeartIsLeftOutWithoutSamplesAndItsHomeLineSitsLowerThanItsPeak() {
        XCTAssertFalse(FootprintCurves(outing(), endMs: 100_000).hasHeart)
        let h = FootprintHeart(avg_bpm: 88, max_bpm: 98, home_avg_bpm: 68, samples: [FootprintBeat(at_ms: 0, bpm: 80), FootprintBeat(at_ms: 50_000, bpm: 98), FootprintBeat(at_ms: 100_000, bpm: 84)])
        let c = FootprintCurves(outing(heart: h), endMs: 100_000)
        XCTAssertTrue(c.hasHeart)
        XCTAssertGreaterThan(c.y(bpm: 68), c.y(bpm: 98))
        XCTAssertGreaterThanOrEqual(c.y(bpm: 98), 0)
        XCTAssertLessThanOrEqual(c.y(bpm: 68), FootprintCurves.heartH)
        XCTAssertEqual(c.bpm(at: 25_000) ?? 0, 89, accuracy: 1e-9)
        XCTAssertNil(c.bpm(at: 200_000))
    }

    func testTicksFallEveryTwentyMinutesFromTheNextTenPastTheStart() {
        let clock = TLClock(tz: "Pacific/Auckland", startMs: 1_760_000_000_000)
        let start = clock.ms(minute: 13 * 60 + 7), end = clock.ms(minute: 14 * 60 + 17)
        let c = FootprintCurves(make(start, end), endMs: end)
        let minutes: [Double] = c.ticks(clock).map { clock.minute(ms: $0) }
        let expected: [Double] = [790, 810, 830, 850]
        XCTAssertEqual(minutes, expected)
    }

    func testALegTakesItsColoursFromTheSpeedsAlongIt() {
        let colors = FootprintCurves.legColors(from: 0, to: 80, count: 5) { t in t == 40 ? 25 / 3.6 : t == 20 ? nil : 0 }
        XCTAssertEqual(colors.count, 5)
        XCTAssertEqual(colors[2], FootprintPaint.ramp[1].rgb)                  // 25 km/h
        XCTAssertEqual(colors[1], FootprintPaint.ramp[0].rgb)                  // unknown reads as still
    }
}
