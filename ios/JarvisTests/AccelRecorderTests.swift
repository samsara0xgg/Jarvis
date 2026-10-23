import XCTest
@testable import Jarvis

/// The phone's raw accelerometer as archive pieces (ADR 0378): which windows a wake reads, and what a piece says.
final class AccelRecorderTests: XCTestCase {
    private let w = AccelRecorder.windowMs
    /// 2026-10-23 18:00:00 UTC, on a window boundary
    private let base: Int64 = 1_792_778_400_000

    func testWindowsAreCutOnUTCBoundaries() {
        XCTAssertEqual(base % w, 0)
        XCTAssertEqual(AccelRecorder.align(base + 599_999), base)
        XCTAssertEqual(AccelRecorder.align(base + w), base + w)
    }

    func testAWindowIsReadOnlyOnceItEndedTheLagAgo() {
        // the window [base, base+10 min) ends at base+10 min, and is read from base+15 min
        XCTAssertEqual(AccelRecorder.windows(cursor: base, now: base + w + AccelRecorder.lagMs - 1), [])
        XCTAssertEqual(AccelRecorder.windows(cursor: base, now: base + w + AccelRecorder.lagMs), [base])
        XCTAssertEqual(AccelRecorder.windows(cursor: base, now: base + 4 * w), [base, base + w, base + 2 * w], "oldest first")
        XCTAssertEqual(AccelRecorder.windows(cursor: base + 2 * w, now: base + 4 * w), [base + 2 * w], "from the cursor on")
    }

    func testTheFirstRunReadsNothingUntilItsWindowEnds() {
        let now = base + 123_456
        let cursor = AccelRecorder.align(now)
        XCTAssertEqual(AccelRecorder.windows(cursor: cursor, now: now), [])
        XCTAssertEqual(AccelRecorder.windows(cursor: cursor, now: cursor + w + AccelRecorder.lagMs), [cursor])
    }

    func testNothingOlderThanThreeDaysIsAskedFor() {
        let now = base + 5 * 24 * 3600 * 1000
        let ws = AccelRecorder.windows(cursor: base, now: now)
        XCTAssertGreaterThanOrEqual(ws.first!, now - AccelRecorder.keepMs, "the recorder keeps 3 days")
        XCTAssertLessThan(ws.first! - w, now - AccelRecorder.keepMs, "and the first window inside them is read")
        XCTAssertEqual(ws.count, Int((AccelRecorder.align(now - AccelRecorder.lagMs) - ws.first!) / w))
    }

    func testARowKeepsEveryRawValue() {
        XCTAssertEqual(AccelRecorder.header, "# jarvis accel v1 rate_hz=50\nt_ms,x,y,z\n")
        XCTAssertEqual(AccelRecorder.row(t: 20, -0.0123, 0.98, 1), "20,-0.0123,0.98,1.0\n")
        let x = 0.1 + 0.2, y = 0.30000000000000004.nextUp
        let r = AccelRecorder.row(t: 0, x, y, 1e-7).dropLast().split(separator: ",")
        XCTAssertEqual(Double(r[1]), x)
        XCTAssertEqual(Double(r[2]), y, "two values one ulp apart stay apart")
        XCTAssertEqual(Double(r[3]), 1e-7)
    }

    func testAPieceIsNamedByItsWindow() {
        let p = AccelRecorder.piece(base)
        XCTAssertEqual(p.id, "accel-\(base)")
        XCTAssertEqual(p.startedMs, base)
        XCTAssertEqual(p.durationMs, 600_000)
        XCTAssertEqual(p.ext, "csv.gz")
        XCTAssertEqual(p.route, "accel")
        XCTAssertNotNil(p.id.range(of: "^[A-Za-z0-9_-]{1,64}$", options: .regularExpression), "an id the brain takes")
        XCTAssertEqual(AccelRecorder.piece(base), p, "the same window makes the same piece")
    }
}
