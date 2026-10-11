import XCTest
@testable import Jarvis

/// What mattered: her sentence about the day and the highlighted items, and a day without them as it always was.
final class DayLineTests: XCTestCase {
    private func reply(_ start: Int64, _ extra: String = "") throws -> DayReply {
        let day = #"{"date": "2026-10-11", "tz": "UTC", "start_ms": \#(start), "end_ms": \#(start + 86_400_000), "now_ms": \#(start + 50_000_000), "missing": [], "items": [{"id": "w1", "lane": "you", "kind": "work", "state": "done", "start_ms": \#(start + 36_000_000), "end_ms": \#(start + 40_000_000)}, {"id": "w2", "lane": "you", "kind": "work", "state": "done", "start_ms": \#(start + 41_000_000), "end_ms": \#(start + 44_000_000)}]\#(extra)}"#
        return try JSONDecoder().decode(DayReply.self, from: Data(day.utf8))
    }
    private var base: Int64 { Int64(Cal.cur.origin.timeIntervalSince1970 * 1000) }
    override func tearDown() { _ = RealDay.shared.rebuild([:], now: 0) }

    func testADayWithoutTheNewFieldsDecodesAsBefore() throws {
        let r = try reply(base)
        XCTAssertNil(r.line)
        XCTAssertEqual(r.highlights, [])
        let evs = RealDay.shared.rebuild(["d": r], now: 1200)
        XCTAssertEqual(evs.count, 2)
        XCTAssertNil(evs[0].mag, "no bigger star")
        XCTAssertNil(evs[0].spot)
        XCTAssertNil(RealDay.shared.line(0))
    }

    func testTheLineAndTheHighlightsAreDecoded() throws {
        let r = try reply(base, #", "line": {"text": "A quiet day.", "draft": true, "written_at": "2026-10-11T15:30:00+00:00"}, "highlights": [{"id": "w1", "label": "Deep work", "by": "him"}, {"id": "w2", "label": "Later", "by": "her"}]"#)
        XCTAssertEqual(r.line, DayLine(text: "A quiet day.", draft: true, written_at: "2026-10-11T15:30:00+00:00"))
        XCTAssertEqual(r.highlights.map(\.id), ["w1", "w2"])
        XCTAssertEqual(r.highlights[0].by, "him")
    }

    func testAnUnreadableLineOrHighlightCostsOnlyItself() throws {
        let r = try reply(base, #", "line": 7, "highlights": [{"id": "w1"}, {"id": "w2", "label": "Kept", "by": "her"}]"#)
        XCTAssertNil(r.line)
        XCTAssertEqual(r.highlights.map(\.id), ["w2"])
        XCTAssertEqual(r.items.count, 2)
        XCTAssertNil(try reply(base, #", "line": null, "highlights": null"#).line)
    }

    func testAHighlightedItemIsABiggerNamedStarAndTheLineBelongsToItsDay() throws {
        let r = try reply(base, #", "line": {"text": "  A quiet day. ", "draft": false, "written_at": "x"}, "highlights": [{"id": "w1", "label": "Deep work", "by": "her"}]"#)
        let evs = RealDay.shared.rebuild(["d": r], now: 1200)
        let hi = try XCTUnwrap(evs.first { $0.id == "w1" }), other = try XCTUnwrap(evs.first { $0.id == "w2" })
        XCTAssertEqual(hi.mag, 1.65)
        XCTAssertEqual(hi.s, "Deep work")
        XCTAssertEqual(hi.spot, RGB.work, "the glow is the colour of what he was doing")
        XCTAssertNil(other.mag)
        XCTAssertNil(other.spot)
        XCTAssertEqual(RealDay.shared.line(0), "A quiet day.")
        XCTAssertNil(RealDay.shared.line(1), "another day has none")
        XCTAssertNil(try { _ = RealDay.shared.rebuild(["d": try reply(base, #", "line": {"text": "  ", "draft": true}"#)], now: 1200); return RealDay.shared.line(0) }(), "an empty sentence is no line")
    }
}
