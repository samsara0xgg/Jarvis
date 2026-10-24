import XCTest
@testable import Jarvis

/// What the iPad's stage shows of today with no row picked: the stretch now is in, and where now is in none, the last stretch before it (never nothing).
final class PadStageTests: XCTestCase {
    private var cal: Cal { Cal.cur }
    private var base: Int64 { Int64(cal.origin.timeIntervalSince1970 * 1000) }
    private func at(_ minute: Double) -> Int64 { base + Int64(minute * 60_000) }

    private func reply(_ items: [String], spans: [String], now: Double) throws -> DayReply {
        let day = #"{"date": "2026-10-08", "tz": "\#(Cal.cur.cal.timeZone.identifier)", "start_ms": \#(base), "end_ms": \#(base + 86_400_000), "now_ms": \#(at(now)), "missing": [], "items": [\#(items.joined(separator: ","))], "spans": [\#(spans.joined(separator: ","))]}"#
        return try JSONDecoder().decode(DayReply.self, from: Data(day.utf8))
    }
    private func span(_ id: String, _ from: Double, _ to: Double?, _ title: String, _ category: String) -> String {
        #"{"id": "\#(id)", "lane": "doing", "kind": "doing", "start_ms": \#(at(from)), "end_ms": \#(to.map { String(at($0)) } ?? "null"), "title": "\#(title)", "detail": null, "category": "\#(category)", "basis": "observed", "confidence": "high", "confirmed": false, "clues": [], "question": null, "place_id": "p-home", "item_ids": ["s-home"]}"#
    }
    private let home = #"{"id": "s-home", "lane": "you", "kind": "stay", "state": "done", "start_ms": 0, "end_ms": null, "place": "12 Example St", "kept_place": {"id": "p-home", "name": "家", "role": "home"}}"#

    /// His evening on 10-23: at home all day, the stretches of what he did end at 23:15, and now is 23:27 in the hole after them, with nothing waiting.
    func testInAHoleAfterTheLastStretchTheStageShowsThatStretchAsJustBefore() throws {
        let items = [home.replacingOccurrences(of: #""start_ms": 0"#, with: #""start_ms": \#(at(480))"#)]
        let spans = [span("d1", 480, 1000, "Jarvis", "work"), span("d2", 1000, 1395, "Yana", "work")]
        let m = DayTimelineModel.build(try reply(items, spans: spans, now: 1407), now: 1407)
        XCTAssert(m.nowRow() == nil || m.nowRow()?.kind == .now, "now is in no stretch, as on his iPad")
        let shown = try XCTUnwrap(PadStage.shown(m), "today's stage is never blank")
        XCTAssertEqual(shown.e.id, "d2", "the last stretch begun before now")
        XCTAssertFalse(shown.now, "said as 刚才, not 现在")
    }

    func testInAStretchTheStageShowsItAsNowAndADayBeforeTodayShowsNothing() throws {
        let items = [home.replacingOccurrences(of: #""start_ms": 0"#, with: #""start_ms": \#(at(480))"#)]
        let spans = [span("d1", 480, 1000, "Jarvis", "work"), span("d2", 1000, nil, "Yana", "work")]
        let m = DayTimelineModel.build(try reply(items, spans: spans, now: 1100), now: 1100)
        let shown = try XCTUnwrap(PadStage.shown(m))
        XCTAssertEqual(shown.e.id, "d2")
        XCTAssertTrue(shown.now)
        XCTAssertNil(PadStage.shown(DayTimelineModel.build(try reply(items, spans: spans, now: 1100), now: nil)))
    }
}
