import XCTest
@testable import Jarvis

/// What he was doing, from the brain's doing lane (ADR 0302): the words of a span, the stay pieces of the bar that wear them, the head of the readout that leads with them, and that a
/// brain which serves none (or serves them badly) leaves the day as it was.
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

    func testOnlyTheDoingLaneThatWasNotPlannedMakesWordsAndAnEndlessOneGoesOn() throws {
        let r = try reply([item("s", "stay", 600, nil, home)], spans: [
            span(600, 660, "Jarvis", "work"),                                       // words
            span(660, 700, "吃饭", "meal", "stated", lane: "place"),                 // a fact span of another lane
            span(700, 720, "开会", "work", "inferred", kind: "planned"),             // what he means to do
            span(720, 740, nil, "home"),                                            // no title
            span(740, nil, "歇会儿", "rest", "inferred"),                            // no end
        ])
        let m = DayTimelineModel.build(r, now: 800)
        XCTAssertEqual(m.doing, [TLDoing(t0: 600, t1: 660, words: "做 Jarvis", when: "10:00–11:00 · 1 时"), TLDoing(t0: 740, t1: 800, words: "歇会儿", when: "12:20 起 · 1 时")], "the endless one is still going: its end is now")
        XCTAssertEqual(DayTimelineModel.build(r, now: nil).doing.last, TLDoing(t0: 740, t1: DAY, words: "歇会儿", when: "12:20 起 · 11 时 40 分"), "on another day it goes on to midnight")
    }

    func testAReplyWithoutSpansBuildsTheDayAsBefore() throws {
        let items = [item("n", "sleep", 60, 420, #""minutes": 360"#), item("s", "stay", 600, 720, home), item("m", "move", 720, 760, walk), item("t", "stay", 760, 900, #""place": "Cafe""#)]
        let none = try reply(items), empty = try reply(items, spans: [])
        XCTAssertTrue(none.spans.isEmpty)
        XCTAssertNil(none.coverage)
        let m = DayTimelineModel.build(none, now: nil)
        XCTAssertTrue(m.doing.isEmpty)
        XCTAssertEqual(m, DayTimelineModel.build(empty, now: nil), "no spans and an empty list are the same day")
        for p in m.scene.pieces { XCTAssertEqual(p.name, m.entries.first { $0.id == p.id }?.barName, p.id) }
        for e in m.entries where e.kind == .stay { XCTAssertEqual(m.readout(at: (e.t0 + (e.t1 ?? e.t0)) / 2).head, e.head) }
        XCTAssertTrue(DayTimelineModel.build(TimelineFixture.today(cal), now: TimelineFixture.now).doing.isEmpty, "the fixture the other checks lean on has none")
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

    // MARK: the bar

    /// the night, two places, the way between them, one nameless stay and a cafe: and what he was doing, for the bar and the readout to lead with
    private func day(spans: Bool) throws -> DayTimelineModel {
        let items = [
            item("n", "sleep", 60, 420, #""minutes": 360"#),
            item("s1", "stay", 600, 720, home),                                       // 120 minutes, 100 of them in a span with words
            item("m1", "move", 720, 800, walk),
            item("s2", "stay", 800, 920, ""),                                         // no name at all; 70 minutes in a span
            item("s3", "stay", 1000, 1100, #""place": "Cafe""#),                      // 40 of 100: less than half
            item("s4", "stay", 1200, 1300, #""place": "Bar""#),                       // 50 of 100: half
            item("r1", "ride", 1400, 1430, #""route": "27""#),
        ]
        let list = [span(60, 420, "睡了", "rest", "inferred"), span(420, 600, "起床吃早饭", "meal", "inferred"),
                    span(600, 700, "Jarvis", "work"), span(700, 720, nil, "home"), span(720, 800, "去咖啡馆", "errand", "inferred"),
                    span(800, 870, "吃饭", "meal", "stated"), span(870, 920, nil, "unknown"),
                    span(1000, 1040, "聊天", "social", "inferred"), span(1040, 1100, nil, "unknown"),
                    span(1200, 1250, "歇会儿", "rest", "inferred"), span(1250, 1300, nil, "unknown"),
                    span(1400, 1430, "27", "commute")]
        return DayTimelineModel.build(try reply(items, spans: spans ? list : nil), now: nil)
    }
    private func piece(_ m: DayTimelineModel, _ id: String) throws -> TLPiece { try XCTUnwrap(m.scene.pieces.first { $0.id == id }, id) }

    func testAStayPieceWearsTheWordsOfTheOneSpanThatCoversHalfOfItAndKeepsTheColourAndIconOfThePlace() throws {
        let m = try day(spans: true), plain = try day(spans: false)
        let s1 = try piece(m, "s1")
        XCTAssertEqual(s1.name, "做 Jarvis")
        XCTAssertEqual(s1.fill, try piece(plain, "s1").fill, "still the place's colour")
        XCTAssertEqual(s1.icon, try piece(plain, "s1").icon, "and icon")
        XCTAssertEqual(try piece(plain, "s1").name, "家")
        let s2 = try piece(m, "s2")
        XCTAssertNil(try piece(plain, "s2").name)
        XCTAssertEqual([s2.name, s2.icon], ["吃饭", "mappin"], "a place with no name takes the words and keeps its pin")
        XCTAssertEqual(try piece(m, "s3").name, "Cafe", "40 of 100 minutes is less than half: the place")
        XCTAssertEqual(try piece(m, "s4").name, "歇会儿", "half is enough")
    }

    func testTripsAndTheNightKeepTheirNamesWhateverTheyWereDoing() throws {
        let m = try day(spans: true), plain = try day(spans: false)
        for id in ["m1", "r1", "n"] {
            XCTAssertEqual(try piece(m, id).name, try piece(plain, id).name, id)
            XCTAssertEqual(try piece(m, id).icon, try piece(plain, id).icon, id)
        }
        XCTAssertEqual(try piece(m, "n").name, "6:00")
        XCTAssertEqual(try piece(m, "m1").name, "步行")
    }

    func testEachPieceOfAStayTheNightCutsInTwoIsJudgedOnItsOwn() throws {
        let items = [item("s0", "stay", 540, 900, #""place": "Home""#), item("n", "sleep", 570, 600, #""minutes": 30"#)]
        let m = DayTimelineModel.build(try reply(items, spans: [span(600, 900, "Jarvis", "work")]), now: nil)
        let stays = m.scene.pieces.filter { $0.id == "s0" }
        XCTAssertEqual(stays.map(\.t0), [540, 600])
        XCTAssertEqual(stays.map(\.name), ["Home", "做 Jarvis"], "the part before the night has nothing said of it")
    }

    // MARK: the readout

    func testTheHeadOpensWithWhatHeWasDoingAndTheStretchFollowsUnlessItSaysTheSame() throws {
        let m = try day(spans: true), plain = try day(spans: false)
        func head(_ t: Double) -> String { m.readout(at: t).head }
        func was(_ t: Double) -> String { plain.readout(at: t).head }
        XCTAssertEqual(head(650), "做 Jarvis · 家 · 10:00–11:40 · 1 时 40 分", "in a stay, the hours are those of what he was doing, not the stay's 10:00–12:00")
        XCTAssertTrue(was(650).hasPrefix("家 · 10:00–12:00"), was(650))
        XCTAssertEqual(m.readout(at: 650).name, "做 Jarvis")
        XCTAssertTrue(m.readout(at: 650).tail.hasPrefix(" · 家 · "))
        XCTAssertEqual(head(710), was(710), "a span with no words: as before")
        XCTAssertEqual(head(760), "去咖啡馆 · " + was(760), "on the way too")
        XCTAssertTrue(head(760).hasPrefix("去咖啡馆 · 步行 · "), head(760))
        XCTAssertEqual(head(840), "吃饭 · 没名字的地方 · 13:20–14:30 · 1 时 10 分")
        XCTAssertEqual(head(1415), was(1415), "the ride is 27 路 and so are the words: said once")
        XCTAssertTrue(head(1415).hasPrefix("27 路 · "), head(1415))
        XCTAssertEqual(head(1015), "聊天 · Cafe · 16:40–17:20 · 40 分")
        XCTAssertEqual(head(1070), was(1070), "no words at that minute, though the stay has some earlier")
        XCTAssertEqual(head(500), "起床吃早饭 · 07:00–10:00 · 3 时", "where nothing was recorded, what was said of it, with its own hours")
        XCTAssertTrue(was(500).hasPrefix("没有记录 · "), was(500))
        XCTAssertEqual(head(150), "睡了 · " + was(150))
    }

    func testTheSecondLineAndThePlaceTheDotAndNothingAheadAreAsBefore() throws {
        let items = [item("s", "stay", 600, nil, home), item("w", "work", 630, 690, #""active_s": 3000, "projects": [{"name": "Jarvis", "seconds": 3000}]"#)]
        let withSpans = DayTimelineModel.build(try reply(items, spans: [span(600, 700, "Jarvis", "work"), span(700, nil, "歇会儿", "rest", "inferred"), span(900, 960, "开会", "work", "inferred", kind: "planned")]), now: 800)
        let plain = DayTimelineModel.build(try reply(items), now: 800)
        for t in [610.0, 650, 750] {
            let a = withSpans.readout(at: t), b = plain.readout(at: t)
            XCTAssertEqual(a.elsewhere, b.elsewhere, "\(t)")
            XCTAssertEqual(a.hue, b.hue)
            XCTAssertEqual(a.strong, b.strong)
        }
        XCTAssertFalse(withSpans.readout(at: 650).elsewhere.isEmpty)
        XCTAssertEqual(withSpans.readout(at: 750).head, "歇会儿 · 家 · 11:40 起 · 1 时 40 分", "a span that goes on, at a minute before now")
        XCTAssertEqual(withSpans.readout(at: 930).head, "还没到", "what has not happened has no words, and a plan is not what he did")
    }
}
