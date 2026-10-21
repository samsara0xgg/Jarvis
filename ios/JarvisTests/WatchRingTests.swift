import XCTest
@testable import Jarvis

final class WatchRingTests: XCTestCase {
    private let c = CGPoint(x: 100, y: 100)

    private func block(_ t0: Double, _ t1: Double) -> WatchDay.Block { .init(t0: t0, t1: t1, rgb: 0xFFFFFF, title: "", sub: "") }

    func testMinutesToAngles() {
        XCTAssertEqual(WatchRing.angle(0), 0)
        XCTAssertEqual(WatchRing.angle(360), .pi / 2, accuracy: 1e-9)          // 6:00 is a quarter turn clockwise: the right
        XCTAssertEqual(WatchRing.angle(720), .pi, accuracy: 1e-9)
        XCTAssertEqual(WatchRing.angle(1440), 2 * .pi, accuracy: 1e-9)
        XCTAssertEqual(WatchRing.angle(2000), 2 * .pi, accuracy: 1e-9)          // past the day's end it stops
        XCTAssertEqual(WatchRing.angle(-5), 0)
    }

    func testPointsOnTheCircle() {
        let top = WatchRing.point(minute: 0, centre: c, radius: 50)
        XCTAssertEqual(top.x, 100, accuracy: 1e-9); XCTAssertEqual(top.y, 50, accuracy: 1e-9)
        let right = WatchRing.point(minute: 360, centre: c, radius: 50)
        XCTAssertEqual(right.x, 150, accuracy: 1e-9); XCTAssertEqual(right.y, 100, accuracy: 1e-9)
        let bottom = WatchRing.point(minute: 720, centre: c, radius: 50)
        XCTAssertEqual(bottom.x, 100, accuracy: 1e-9); XCTAssertEqual(bottom.y, 150, accuracy: 1e-9)
        let left = WatchRing.point(minute: 1080, centre: c, radius: 50)       // the now dot at 18:00
        XCTAssertEqual(left.x, 50, accuracy: 1e-9); XCTAssertEqual(left.y, 100, accuracy: 1e-9)
    }

    func testPointBackToMinute() {
        for m in stride(from: 0.0, to: 1440, by: 37) {
            XCTAssertEqual(WatchRing.minute(at: WatchRing.point(minute: m, centre: c, radius: 60), centre: c), m, accuracy: 1e-6)
        }
    }

    func testArcsSkipEmptyBlocksAndKeepIndex() {
        let arcs = WatchRing.arcs([block(0, 360), block(400, 400), block(720, 1080)])
        XCTAssertEqual(arcs.map(\.index), [0, 2])
        XCTAssertEqual(arcs[0].start, 0)
        XCTAssertEqual(arcs[0].end, .pi / 2, accuracy: 1e-9)
        XCTAssertEqual(arcs[1].start, .pi, accuracy: 1e-9)
        XCTAssertEqual(arcs[1].end, 1.5 * .pi, accuracy: 1e-9)
    }

    func testHitFindsTheBlockUnderATouch() {
        let blocks = [block(0, 360), block(360, 720), block(1000, 1010)]
        // on the ring at 3:00 -> block 0; at 9:00 -> block 1
        XCTAssertEqual(WatchRing.hit(WatchRing.point(minute: 180, centre: c, radius: 80), centre: c, radius: 80, band: 12, blocks: blocks), 0)
        XCTAssertEqual(WatchRing.hit(WatchRing.point(minute: 540, centre: c, radius: 84), centre: c, radius: 80, band: 12, blocks: blocks), 1)
        // a short block is found from a little beside it, a long empty stretch is not a block
        XCTAssertEqual(WatchRing.hit(WatchRing.point(minute: 1015, centre: c, radius: 80), centre: c, radius: 80, band: 12, blocks: blocks), 2)
        XCTAssertNil(WatchRing.hit(WatchRing.point(minute: 880, centre: c, radius: 80), centre: c, radius: 80, band: 12, blocks: blocks))
    }

    func testHitIgnoresTouchesOffTheBand() {
        let blocks = [block(0, 1440)]
        XCTAssertNil(WatchRing.hit(c, centre: c, radius: 80, band: 12, blocks: blocks))                                              // on her
        XCTAssertNil(WatchRing.hit(WatchRing.point(minute: 100, centre: c, radius: 50), centre: c, radius: 80, band: 12, blocks: blocks))   // inside the ring
        XCTAssertNil(WatchRing.hit(WatchRing.point(minute: 100, centre: c, radius: 100), centre: c, radius: 80, band: 12, blocks: blocks))  // outside it
    }

    func testTheNowDotIsNoWiderThanTheRing() {
        for t in [3.0, 4, 10.8, 20] {
            let d = WatchRing.dot(thickness: t)
            XCTAssertEqual(d.radius * 2, t, accuracy: 1e-9)
            XCTAssertGreaterThan(d.radius, d.edge, "something white is left inside the edge")
        }
    }

    func testATickIsJustInsideTheRingsInnerEdgeAtItsMinute() {
        let radius = 80.0, thickness = 10.0
        for m in [0.0, 360, 1320, 1330] {
            let t = WatchRing.tick(minute: m, centre: c, radius: radius, thickness: thickness)
            let inner = radius - thickness / 2
            let a = hypot(Double(t.from.x - c.x), Double(t.from.y - c.y)), b = hypot(Double(t.to.x - c.x), Double(t.to.y - c.y))
            XCTAssertLessThan(a + t.width / 2, inner, "its cap stays clear of the ring")
            XCTAssertGreaterThan(a, inner - 4, "and close to it")
            XCTAssertLessThan(b, a, "it points inward")
            XCTAssertEqual(WatchRing.minute(at: t.from, centre: c), m, accuracy: 1e-6)
            XCTAssertEqual(WatchRing.minute(at: t.to, centre: c), m, accuracy: 1e-6)
        }
    }

    func testATickTenMinutesAfterNowDoesNotTouchTheDot() {
        let radius = 80.0, thickness = 10.0
        let dot = WatchRing.dot(thickness: thickness)
        let now = WatchRing.point(minute: 1310, centre: c, radius: radius)
        let t = WatchRing.tick(minute: 1320, centre: c, radius: radius, thickness: thickness)
        for p in [t.from, t.to] {
            XCTAssertGreaterThan(hypot(Double(p.x - now.x), Double(p.y - now.y)), dot.radius + t.width / 2, "the tick stays beside the dot, never under it")
        }
    }

    func testNowMinuteAdvancesWithTheClockAndStopsAtMidnight() {
        let made = Date(timeIntervalSince1970: 1_000_000)
        let day = WatchDay(date: "d", language: "zh", now: 1310, made: made)
        XCTAssertEqual(WatchRing.nowMinute(day, at: made), 1310)
        XCTAssertEqual(WatchRing.nowMinute(day, at: made.addingTimeInterval(600)), 1320, accuracy: 1e-9)
        XCTAssertEqual(WatchRing.nowMinute(day, at: made.addingTimeInterval(-60)), 1310)
        XCTAssertEqual(WatchRing.nowMinute(day, at: made.addingTimeInterval(86_400)), 1440)
    }
}
