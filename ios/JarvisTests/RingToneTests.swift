import XCTest
@testable import Jarvis

/// What the trace colours say he was doing: the activity -> colour and brightness mapping.
final class RingToneTests: XCTestCase {
    func testEachActivityGetsItsPaletteColour() throws {
        func col(_ kind: String, _ activity: String? = nil) throws -> RGB { try XCTUnwrap(RingTone.of(kind, activity: activity, active: 1000, span: 3000)).tone.col }
        XCTAssertEqual(try col("sleep"), .dial)
        XCTAssertEqual(try col("work"), .work)
        XCTAssertEqual(try col("move", "walking"), .done)
        XCTAssertEqual(try col("move", "running"), .done)
        XCTAssertEqual(try col("move", "cycling"), .done)
        XCTAssertEqual(try col("move", "automotive"), .lit)
        XCTAssertEqual(try col("ride"), .lit)
        XCTAssertEqual(try col("call"), .wait)
        XCTAssertEqual(try col("stay"), .ink)
        XCTAssertNil(RingTone.of("reminder"))
    }

    func testBrightnessFollowsEngagement() {
        let idle = RingTone.idle.lum, sleep = RingTone.sleep.lum
        let lazy = RingTone.work(active: 600, span: 6000).lum, busy = RingTone.work(active: 6000, span: 6000).lum
        XCTAssertLessThan(sleep, 1)
        XCTAssertLessThan(idle, 1)
        XCTAssertLessThan(lazy, busy)
        XCTAssertGreaterThan(busy, 1)
        XCTAssertGreaterThan(RingTone.ride.lum, RingTone.car.lum)
        XCTAssertGreaterThan(RingTone.car.lum, 1)
        XCTAssertEqual(RingTone.work(active: 9000, span: 6000).lum, busy, "more active than the span is the whole span")
    }

    func testARideAndMovesWinOverWorkAndStay() throws {
        func prio(_ k: String) throws -> Int { try XCTUnwrap(RingTone.of(k)).prio }
        XCTAssertGreaterThan(try prio("ride"), try prio("move"))
        XCTAssertGreaterThan(try prio("move"), try prio("call"))
        XCTAssertGreaterThan(try prio("call"), try prio("work"))
        XCTAssertGreaterThan(try prio("work"), try prio("stay"))
    }

    func testWidthStaysSubtle() {
        for t in [RingTone.sleep, .idle, .call, .ride, .car, .work(active: 6000, span: 6000)] {
            XCTAssertTrue((0.85...1.25).contains(t.width))
        }
    }
}
