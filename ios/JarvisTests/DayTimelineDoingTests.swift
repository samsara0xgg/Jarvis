import XCTest
@testable import Jarvis

/// What he was doing, from the brain's doing lane (ADR 0302): the words of a span, and that a brain which serves none (or serves them badly) leaves the day as it was. The list of
/// stretches built from them is DayTimelineDoingRowsTests.
final class DayTimelineDoingTests: XCTestCase {
    private var cal: Cal { Cal.cur }
    private var base: Int64 { Int64(cal.origin.timeIntervalSince1970 * 1000) }
    private func at(_ minute: Double) -> Int64 { base + Int64(minute * 60_000) }

    private func reply(_ items: [String], spans: [String]? = nil, _ extra: String = "") throws -> DayReply {
        let spanText = spans.map { #", "spans": [\#($0.joined(separator: ","))]"# } ?? ""
        let day = #"{"date": "2026-10-08", "tz": "\#(Cal.cur.cal.timeZone.identifier)", "start_ms": \#(base), "end_ms": \#(base + 86_400_000), "now_ms": \#(at(1440)), "missing": [], "items": [\#(items.joined(separator: ","))]\#(spanText)\#(extra)}"#
        return try JSONDecoder().decode(DayReply.self, from: Data(day.utf8))
    }
    private func item(_ id: String, _ kind: String, _ from: Double, _ to: Double?, _ more: String = "") -> String {
        #"{"id": "\#(id)", "lane": "you", "kind": "\#(kind)", "state": "done", "start_ms": \#(at(from)), "end_ms": \#(to.map { String(at($0)) } ?? "null")\#(more.isEmpty ? "" : ", " + more)}"#
    }
    /// a span of the doing lane as the brain serves it (`to` nil: it goes on)
    private func span(_ from: Double, _ to: Double?, _ title: String?, _ category: String = "unknown", _ basis: String = "observed", lane: String = "doing", kind: String = "doing") -> String {
        let t = title.map { "\"\($0)\"" } ?? "null"
        return #"{"id": "doing-\#(Int(from))", "lane": "\#(lane)", "kind": "\#(kind)", "start_ms": \#(at(from)), "end_ms": \#(to.map { String(at($0)) } ?? "null"), "title": \#(t), "detail": null, "category": "\#(category)", "basis": "\#(basis)", "confidence": "low", "confirmed": false, "clues": [], "question": null, "place_id": null, "item_ids": []}"#
    }
    private func decoded(_ s: String) throws -> DaySpan { try JSONDecoder().decode(DaySpan.self, from: Data(s.utf8)) }
    private func words(_ title: String?, _ category: String, _ basis: String) throws -> String? {
        DayTimelineModel.doingWords(try decoded(span(600, 660, title, category, basis)))
    }
    private let walk = #""activity": "walking""#
    private let home = #""place": "12 Example St", "kept_place": {"id": "p-home", "name": "家", "role": "home"}"#

    // MARK: the words

    func testWhatAModelWroteOrHeAnsweredIsShownAsGivenWhateverItsCategory() throws {
        XCTAssertEqual(try words("改论文第三章", "work", "inferred"), "改论文第三章", "not 做 on top of a phrase")
        XCTAssertEqual(try words("吃午饭", "meal", "stated"), "吃午饭")
        XCTAssertEqual(try words("27", "commute", "inferred"), "27", "not the ride's words: a phrase is a phrase")
        XCTAssertEqual(try words("  去学校  ", "commute", "stated"), "去学校", "trimmed")
    }

    func testWhatTheProgramCutIsPutInTheWordsTheRowsUseByCategory() throws {
        XCTAssertEqual(try words("Jarvis", "work", "observed"), "做 Jarvis", "a project or a session")
        XCTAssertEqual(try words("27", "commute", "observed"), "27 路", "a ride is its route, as its row says it")
        XCTAssertEqual(try words("14 → 7", "commute", "observed"), "14→7 路", "with the arrow written as the rows write it")
        XCTAssertEqual(try words("FaceTime", "unknown", "observed"), "FaceTime 通话", "the app of a call")
        XCTAssertEqual(try words("running", "exercise", "observed"), "跑步", "the workout as the rows call it")
        XCTAssertEqual(try words("HKWorkoutActivityTypeCycling", "exercise", "observed"), "骑车")
        XCTAssertEqual(try words("Pilates", "exercise", "observed"), "Pilates", "a workout the rows have no word for keeps its own")
        XCTAssertEqual(try words("睡眠", "sleep", "observed"), "睡觉")
        XCTAssertEqual(try words("在家", "home", "observed"), "在家", "any other: the title")
    }

    func testASpanWithNoTitleOrABlankOneHasNoWords() throws {
        for basis in ["observed", "inferred", "stated"] {
            for category in ["work", "commute", "exercise", "sleep", "unknown", "home"] {
                XCTAssertNil(try words(nil, category, basis), "\(basis) \(category)")
                XCTAssertNil(try words("   ", category, basis), "\(basis) \(category) blank")
            }
        }
    }

    // MARK: the model

    func testAReplyWithoutSpansBuildsTheDayAsBefore() throws {
        let items = [item("n", "sleep", 60, 420, #""minutes": 360"#), item("s", "stay", 600, 720, home), item("m", "move", 720, 760, walk), item("t", "stay", 760, 900, #""place": "Cafe""#)]
        let none = try reply(items), empty = try reply(items, spans: [])
        XCTAssertTrue(none.spans.isEmpty)
        XCTAssertNil(none.coverage)
        let m = DayTimelineModel.build(none, now: nil)
        XCTAssertEqual(m, DayTimelineModel.build(empty, now: nil), "no spans and an empty list are the same day")
        XCTAssertTrue(m.entries.allSatisfy { $0.kind != .segment }, "the day's records, as always")
        for p in m.scene.pieces { XCTAssertEqual(p.name, m.entries.first { $0.id == p.id }?.barName, p.id) }
        for e in m.entries where e.kind == .stay { XCTAssertEqual(m.readout(at: (e.t0 + (e.t1 ?? e.t0)) / 2).head, e.head) }
        XCTAssertTrue(DayTimelineModel.build(TimelineFixture.today(cal), now: TimelineFixture.now).entries.allSatisfy { $0.kind != .segment }, "the fixture the other checks lean on has none")
    }

    func testSpansAndCoverageThatAreNotTheShapeCostOnlyThemselves() throws {
        let items = [item("s", "stay", 600, 720, home)]
        let r = try reply(items, spans: [span(600, 660, "Jarvis", "work"), #"{"id": 7, "lane": "doing"}"#, #"{"id": "x", "lane": "doing", "kind": "doing", "start_ms": "soon"}"#,
                                         #"{"id": "y", "lane": "doing", "kind": "doing", "start_ms": 1, "end_ms": 2, "title": 5, "category": ["x"], "basis": null}"#],
                          #", "coverage": {"waking_start_ms": 1, "waking_end_ms": 9, "waking_ms": "long", "filled_ms": 4}"#)
        XCTAssertEqual(r.items.count, 1)
        XCTAssertEqual(r.spans.map(\.id), ["doing-600", "y"], "two went; the one with a title that is no title stays, and says nothing")
        XCTAssertNil(DayTimelineModel.doingWords(r.spans[1]))
        XCTAssertEqual(r.coverage?.waking_start_ms, 1)
        XCTAssertNil(r.coverage?.waking_ms, "a value that is not a number is nothing")
        XCTAssertEqual(r.coverage?.filled_ms, 4)
        // not a list at all, and a coverage that is not an object: the day is still the day
        let bad = try JSONDecoder().decode(DayReply.self, from: Data(#"{"date": "2026-10-08", "tz": "UTC", "start_ms": 0, "end_ms": 1, "now_ms": 0, "missing": [], "items": [], "spans": "none", "coverage": 3}"#.utf8))
        XCTAssertTrue(bad.spans.isEmpty)
        XCTAssertNil(bad.coverage)
    }
}
