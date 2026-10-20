import XCTest
@testable import Jarvis

/// The day's list as one row for each stretch of what he was doing (ADR 0302, ADR 0307): the rows up to now, their titles and the line under them, the counts, what an opened stretch holds,
/// how she knows, the bar by category, and that a brain with no stretches leaves the list as it always was.
final class DayTimelineDoingRowsTests: XCTestCase {
    private var cal: Cal { Cal.cur }
    private var base: Int64 { Int64(cal.origin.timeIntervalSince1970 * 1000) }
    private func at(_ minute: Double) -> Int64 { base + Int64(minute * 60_000) }

    private func reply(_ items: [String], spans: [String]? = nil) throws -> DayReply {
        let spanText = spans.map { #", "spans": [\#($0.joined(separator: ","))]"# } ?? ""
        let day = #"{"date": "2026-10-08", "tz": "\#(Cal.cur.cal.timeZone.identifier)", "start_ms": \#(base), "end_ms": \#(base + 86_400_000), "now_ms": \#(at(900)), "missing": [], "items": [\#(items.joined(separator: ","))]\#(spanText)}"#
        return try JSONDecoder().decode(DayReply.self, from: Data(day.utf8))
    }
    private func item(_ id: String, _ kind: String, _ from: Double, _ to: Double?, _ more: String = "", lane: String = "you") -> String {
        #"{"id": "\#(id)", "lane": "\#(lane)", "kind": "\#(kind)", "state": "done", "start_ms": \#(at(from)), "end_ms": \#(to.map { String(at($0)) } ?? "null")\#(more.isEmpty ? "" : ", " + more)}"#
    }
    /// a stretch of the doing lane as the brain serves it (`to` nil: it goes on)
    private func span(_ id: String, _ from: Double, _ to: Double?, _ title: String?, _ category: String, basis: String = "observed", confidence: String = "high", detail: String? = nil, place: String? = nil,
                      items: [String] = [], clues: [String] = [], kind: String = "doing", lane: String = "doing") -> String {
        func q(_ s: String?) -> String { s.map { "\"\($0)\"" } ?? "null" }
        let list = { (xs: [String]) in "[" + xs.map { "\"\($0)\"" }.joined(separator: ",") + "]" }
        let cl = "[" + clues.map { #"{"kind": "words", "at_ms": 1, "text": "\#($0)"}"# }.joined(separator: ",") + "]"
        return #"{"id": "\#(id)", "lane": "\#(lane)", "kind": "\#(kind)", "start_ms": \#(at(from)), "end_ms": \#(to.map { String(at($0)) } ?? "null"), "title": \#(q(title)), "detail": \#(q(detail)), "category": "\#(category)", "basis": "\#(basis)", "confidence": "\#(confidence)", "confirmed": false, "clues": \#(cl), "question": null, "place_id": \#(q(place)), "item_ids": \#(list(items))}"#
    }
    private let home = #""place": "12 Example St", "kept_place": {"id": "p-home", "name": "家", "role": "home"}"#

    /// The made-up day at 15:00. The night; a long stay at home that is under three stretches; a work stretch with a picture, a talk and a Claude Code session that goes on into the next
    /// stretch; a hole nobody could say anything of; a ride; a meal he told her; and a rest with his phone, a message, a call, a song and a reminder that waits on him. After now an event and a reminder.
    private func day() throws -> DayTimelineModel {
        let items = [
            item("n", "sleep", 60, 420, #""minutes": 360"#),
            item("s-home", "stay", 420, 760, home),
            item("s-cafe", "stay", 790, 830, #""place": "Cafe Lune, Fort St, Victoria""#),
            item("w1", "work", 560, 700, #""active_s": 6000, "projects": [{"name": "Jarvis", "seconds": 5000}], "apps": [{"name": "Xcode", "seconds": 4000}]"#),
            item("c1", "code", 650, 740, #""title": "接上天气", "project": "Jarvis", "account": "a""#),
            item("pic1", "picture", 600, 600, #""attachment": "pic-1", "note": "黑板""#),
            item("t1", "talk", 610, 620, #""lane": "her", "turns": 4"#),
            item("e0", "event", 540, 570, #""title": "站会""#),
            item("r1", "ride", 760, 790, #""route": "27", "board_stop": "A", "alight_stop": "B""#),
            item("pay1", "payment", 800, 800, #""amount": "12.40", "currency": "CAD", "direction": "out", "counterparty": "Cafe Lune""#),
            item("scr1", "screen", 840, 880, #""device": "iPhone", "active_s": 1800, "apps": [{"name": "微信", "bundle": "x", "minutes": 22}, {"name": "Safari", "bundle": "y", "minutes": 8}]"#),
            item("med1", "media", 850, 880, #""app": "Music", "active_s": 1500, "titles": [{"title": "歌名", "artist": "歌手"}]"#),
            item("msg1", "messages", 855, 870, #""app": "Messages", "conversation": "Mia", "sent": 3, "received": 5"#),
            item("pc1", "phone_call", 860, 870, #""app": "Phone", "person": "Mia", "direction": "sent", "duration_s": 600"#),
            item("call1", "call", 865, 880, #""app": "FaceTime""#),
            item("rem-rang", "reminder", 850, nil, #""lane": "her", "text": "吃药", "fired": true, "acknowledged": false"#),
            item("ev-later", "event", 1000, 1060, #""title": "陶艺课""#),
            item("rem-due", "reminder", 1100, nil, #""lane": "her", "text": "给 Mia 回电话", "fired": false"#),
        ]
        let spans = [
            span("d1", 420, 540, "起床吃早饭", "meal", basis: "inferred", confidence: "medium", place: "s-home", items: ["s-home"]),
            span("d2", 540, 720, "Jarvis", "work", confidence: "low", detail: "一直在改时间线", place: "s-home", items: ["s-home", "w1", "c1", "pic1", "t1", "ghost", "w1"], clues: ["Xcode 在前台", "27 路之前一直在家"]),
            span("d3", 720, 760, nil, "unknown", place: "s-home", items: ["s-home", "c1"]),
            span("d4", 760, 790, "27", "commute", items: ["r1"]),
            span("d5", 790, 830, "吃午饭", "meal", basis: "stated", place: "s-cafe", items: ["s-cafe", "pay1"]),
            span("d6", 830, nil, "歇会儿", "rest", basis: "inferred", items: ["scr1", "med1", "msg1", "pc1", "call1", "rem-rang"]),
            span("p1", 1000, 1060, "陶艺课", "social", basis: "observed", kind: "planned"),
            span("f1", 100, 200, "facts", "work", lane: "place"),
        ]
        return DayTimelineModel.build(try reply(items, spans: spans), now: 900)
    }
    private func entry(_ m: DayTimelineModel, _ id: String) throws -> TLEntry { try XCTUnwrap(m.entries.first { $0.id == id }, id) }

    // MARK: the rows

    func testTheListIsTheNightThenARowForEachStretchThenNowThenWhatIsToCome() throws {
        let m = try day()
        XCTAssertEqual(m.entries.map(\.id), ["n", "d1", "d2", "d3", "d4", "d5", "d6", "rem-rang", "now", "ev-later", "rem-due"],
                       "the stay, the work, the ride, the picture and the talk are inside a stretch, not rows; what waits on him keeps its place; the plan is as it was; a plan span is no row")
        XCTAssertEqual(m.entries.map(\.kind), [.sleep, .segment, .segment, .segment, .segment, .segment, .segment, .reminder, .now, .event, .reminder])
        XCTAssertEqual(try entry(m, "n").title, "睡觉", "the night as it was")
        XCTAssertEqual(m.waiting, ["rem-rang"], "and the pill that counts it")
        XCTAssertFalse(m.entries.contains { $0.kind == .talk }, "a talk is never drawn")
    }

    func testARowSaysWhenWhatTheyDidWhereAndHowLong() throws {
        let m = try day()
        let d1 = try entry(m, "d1"), d2 = try entry(m, "d2"), d3 = try entry(m, "d3"), d4 = try entry(m, "d4"), d5 = try entry(m, "d5"), d6 = try entry(m, "d6")
        XCTAssertEqual([d1.time, d2.time, d3.time, d4.time, d5.time, d6.time], ["07:00", "09:00", "12:00", "12:40", "13:10", "13:50"])
        XCTAssertEqual([d1.title, d2.title, d3.title, d4.title, d5.title, d6.title], ["起床吃早饭", "做 Jarvis", "不知道在干嘛", "27 路", "吃午饭", "歇会儿"],
                       "a model's phrase or his own answer as given, the program's words as the rows say them, and none: 不知道在干嘛")
        XCTAssertEqual([d1.unknown, d3.unknown, d4.unknown], [false, true, false], "only the one with no words is muted")
        XCTAssertEqual([d1.right, d2.right, d3.right, d4.right, d5.right, d6.right], ["2 时", "3 时", "40 分", "30 分", "40 分", "1 时 10 分"], "how long; the last goes on to now")
        XCTAssertTrue(d6.going)
        XCTAssertEqual(d1.detail, "家", "the place the brain points to; none when it points to none")
        XCTAssertEqual(d2.detail, "家 · 不太确定", "and not sure when the brain says so")
        XCTAssertEqual(d3.detail, "家")
        XCTAssertNil(d4.detail)
        XCTAssertEqual(d5.detail, "Cafe Lune", "a place that has no name he gave is the phone's first part of its label")
        XCTAssertNil(d6.detail)
    }

    func testThePageOfARowSaysItsHoursHowLongAndWhere() throws {
        let m = try day()
        XCTAssertEqual(try entry(m, "d2").pageLine, "09:00–12:00 · 3 时 · 家 · 不太确定")
        XCTAssertEqual(try entry(m, "d4").pageLine, "12:40–13:10 · 30 分", "no place: the hours and the length")
        XCTAssertEqual(try entry(m, "d6").pageLine, "13:50 起 · 1 时 10 分", "a stretch that goes on says when it began")
    }

    func testTheCountsAreThePicturesPaymentsCallsAndMessagesInsideInThatOrder() throws {
        let m = try day()
        XCTAssertEqual(try entry(m, "d2").counts, TLCounts(pictures: 1, payments: 0, calls: 0, messages: 0), "the talk is not counted, a work block and a session are not told in numbers")
        XCTAssertEqual(try entry(m, "d5").counts, TLCounts(pictures: 0, payments: 1, calls: 0, messages: 0))
        let d6 = try entry(m, "d6")
        XCTAssertEqual(d6.counts, TLCounts(pictures: 0, payments: 0, calls: 2, messages: 1), "a call on the Mac and one on the phone are both calls")
        XCTAssertEqual(d6.counts.shown.map(\.symbol), ["phone.fill", "message.fill"])
        XCTAssertEqual(d6.counts.shown.map(\.n), [2, 1])
        XCTAssertTrue(try entry(m, "d1").counts.isEmpty)
        XCTAssertEqual(try entry(m, "d4").base, TLMetrics.row, "nothing under the title: a plain row")
        XCTAssertEqual(try entry(m, "d6").base, TLMetrics.rowDetail, "icons under it: a row with a line")
    }

    func testCategoriesAreDrawnInTheirColourAndSymbolAndACommuteWearsItsWay() throws {
        let m = try day()
        let want: [(String, RGB?, String)] = [("d1", RGB(240, 176, 79), "fork.knife"), ("d2", RGB(91, 140, 255), "laptopcomputer"), ("d3", nil, "questionmark"),
                                              ("d4", TLPaint.trip, "bus.fill"), ("d5", RGB(240, 176, 79), "fork.knife"), ("d6", RGB(111, 168, 200), "sofa.fill")]
        for (id, color, symbol) in want {
            let e = try entry(m, id)
            XCTAssertEqual(e.color, color, id)
            XCTAssertEqual(e.symbol, symbol, id)
        }
        let table: [(String, String, RGB?)] = [("study", "book.fill", RGB(155, 123, 255)), ("sleep", "moon.fill", TLPaint.sleep), ("exercise", "figure.run", RGB(76, 195, 138)), ("errand", "bag.fill", RGB(232, 135, 90)),
                                               ("social", "person.2.fill", RGB(233, 121, 166)), ("home", "house.fill", RGB(168, 159, 145)), ("unknown", "questionmark", nil), ("something new", "questionmark", nil)]
        for (name, symbol, color) in table { XCTAssertEqual(TLCategory.of(name), TLCategory(color: color, symbol: symbol), name) }
    }

    func testAStretchThatReachesPastNowEndsThereAndOneThatBeginsAfterItIsNotARow() throws {
        let r = try reply([], spans: [span("a", 420, 960, "Jarvis", "work"), span("b", 905, 960, "歇会儿", "rest", basis: "inferred")])
        let m = DayTimelineModel.build(r, now: 900)
        XCTAssertEqual(m.entries.map(\.id), ["a", "now"])
        XCTAssertEqual(try entry(m, "a").t1, 900)
        XCTAssertEqual(try entry(m, "a").right, "8 时")
        let other = DayTimelineModel.build(r, now: nil)
        XCTAssertEqual(other.entries.map(\.id), ["a", "b"], "on another day they are as long as they are")
        XCTAssertEqual([try entry(other, "a").right, try entry(other, "b").right], ["9 时", "55 分"])
    }

    // MARK: opened

    func testAnOpenedStretchHoldsTheModelsLineThenWhatIsInsideItInTimeOrder() throws {
        let m = try day()
        let d2 = try entry(m, "d2")
        XCTAssertEqual(d2.note, "一直在改时间线")
        XCTAssertEqual(d2.lines.map(\.id), ["w1", "pic1", "c1"], "the work block, the picture and the session by when they began; the stay and the talk are left out, an id that is not among the day's items is skipped, and one listed twice is once")
        XCTAssertEqual(d2.lines.map(\.title), ["在 Mac 上工作", "照片", "接上天气"])
        XCTAssertEqual(d2.lines[0].detail, "Jarvis · Xcode", "as its row says it")
        XCTAssertEqual(d2.lines[0].right, "活跃 1 时 40 分", "it began and ended inside the stretch: what its row says")
        XCTAssertEqual(d2.lines[1].right, "10:00", "a moment has its time")
        XCTAssertEqual(d2.lines[1].attachment, "pic-1", "and the picture its thumbnail")
        XCTAssertNil(try entry(m, "d1").note)
        XCTAssertEqual(try entry(m, "d1").lines, [], "a stay alone is nothing to list")
    }

    func testALastingItemThatRunsBeyondTheStretchShowsItsLengthInsideIt() throws {
        let m = try day()
        let a = try entry(m, "d2").lines.first { $0.id == "c1" }, b = try entry(m, "d3").lines.first { $0.id == "c1" }
        XCTAssertEqual(a?.right, "1 时 10 分", "10:50 to 12:20, the stretch ends at 12:00: 70 minutes of it")
        XCTAssertEqual(b?.right, "20 分", "and the same session in the next: 12:00 to 12:20")
        XCTAssertEqual(try entry(m, "d3").lines.map(\.id), ["c1"])
        XCTAssertEqual(try entry(m, "d2").lines.first { $0.id == "w1" }?.right, "活跃 1 时 40 分", "one that does not run beyond is told as its row tells it")
    }

    func testTheKindsOfTheDevicesAreLinesWithTheirOwnWords() throws {
        let m = try day()
        let d6 = try entry(m, "d6")
        XCTAssertEqual(d6.lines.map(\.id), ["scr1", "med1", "rem-rang", "msg1", "pc1", "call1"], "by when they began; the reminder that rang is a row of its own too, and is in its stretch")
        let by = Dictionary(uniqueKeysWithValues: d6.lines.map { ($0.id, $0) })
        XCTAssertEqual([by["scr1"]?.title, by["scr1"]?.detail, by["scr1"]?.right], ["用 iPhone", "微信 22 分 · Safari 8 分", "30 分"], "phone use: the device, the apps and minutes")
        XCTAssertEqual([by["med1"]?.title, by["med1"]?.detail, by["med1"]?.right], ["Music", "歌名 · 歌手", "25 分"], "media: the app and the top title")
        XCTAssertEqual([by["msg1"]?.title, by["msg1"]?.detail, by["msg1"]?.right], ["Mia", "发 3 · 收 5", "15 分"], "messages: the conversation, what he sent and what came")
        XCTAssertEqual([by["pc1"]?.title, by["pc1"]?.detail, by["pc1"]?.right], ["Mia", "打出 · Phone", "10 分"], "a call on the phone: the person, which way, how long")
        XCTAssertEqual([by["call1"]?.title, by["call1"]?.right], ["通话 · FaceTime", "15 分"], "a call on the Mac has no person: the app and how long")
        XCTAssertEqual([by["rem-rang"]?.title, by["rem-rang"]?.detail, by["rem-rang"]?.right], ["吃药", "响过了", "14:10"])
        XCTAssertEqual(by["scr1"]?.symbol, "iphone")
    }

    func testNothingElseIsALineOfTheOldListWhenThereAreNoStretches() throws {
        let items = [item("n", "sleep", 60, 420, #""minutes": 360"#), item("s", "stay", 420, 700, home),
                     item("scr1", "screen", 840, 880, #""device": "iPhone", "active_s": 1800"#), item("msg1", "messages", 855, 870, #""conversation": "Mia", "sent": 3, "received": 5"#),
                     item("pc1", "phone_call", 860, 870, #""person": "Mia", "direction": "sent", "duration_s": 600"#), item("med1", "media", 850, 880, #""app": "Music""#), item("a1", "away_note", 700, 720, #""title": "午睡""#)]
        let plain = DayTimelineModel.build(try reply(items), now: 900)
        XCTAssertEqual(plain.entries.map(\.id), ["n", "s", "now"], "an older brain's day, or one it cannot serve: the records, as always, and not the kinds that are only ever inside a stretch")
        let others = DayTimelineModel.build(try reply(items, spans: [span("p1", 1000, 1060, "x", "work", kind: "planned"), span("f1", 100, 200, "x", "work", lane: "place"), span("u", 420, 500, "x", "work", kind: "gap")]), now: 900)
        XCTAssertEqual(others, plain, "a plan, a fact span and a kind the phone does not know are no stretches")
    }

    // MARK: how she knows

    func testHowSheKnowsIsWhoSaidItAndWhatSheWentBy() throws {
        let m = try day()
        XCTAssertEqual(try entry(m, "d2").knows, "记录里看到的：Xcode 在前台；27 路之前一直在家", "the program's: from his records, with the clues joined")
        XCTAssertEqual(try entry(m, "d1").knows, "她猜的", "a model's guess, and no clues said")
        XCTAssertEqual(try entry(m, "d5").knows, "你说的")
        let r = try reply([], spans: [span("x", 420, 500, "歇会儿", "rest", basis: "inferred", clues: ["沙发上的照片", "手机用了一会儿"])])
        XCTAssertEqual(try entry(DayTimelineModel.build(r, now: 900), "x").knows, "她猜的：沙发上的照片；手机用了一会儿")
    }

    // MARK: what it offers

    func testAStretchOffersToBeAskedAboutAndToBeSaidButNotToBeStarred() throws {
        let m = try day()
        let d2 = try entry(m, "d2"), d4 = try entry(m, "d4")
        XCTAssertEqual(d2.actions, [.ask, .sayWhat], "put to her as the place the brain points to, and the quiet 这段在干嘛")
        XCTAssertEqual(d2.about?.id, "s-home")
        XCTAssertEqual(d2.about?.title, "做 Jarvis")
        XCTAssertEqual(d2.about?.startMs, at(540))
        XCTAssertEqual(d4.about?.id, "r1", "with no place, the first thing inside it the brain can be told about")
        XCTAssertFalse(d2.starrable)
        XCTAssertTrue(d2.opens)
        XCTAssertEqual(try entry(m, "rem-rang").actions.contains(.acknowledge), true, "what waits on him keeps its actions")
        XCTAssertEqual(try entry(m, "d3").actions.contains(.sayWhat), true, "and so does the hole nobody could say anything of")
    }

    // MARK: the bar and the readout

    func testTheBarIsTheNightAndTheStretchesByCategoryAndAHoleWhereNobodyCouldSay() throws {
        let m = try day()
        XCTAssertEqual(m.segs.map(\.id), ["n", "d1", "d2", "d3", "d4", "d5", "d6"])
        XCTAssertEqual(m.segs.map(\.kind), [.sleep, .doing(RGB(240, 176, 79)), .doing(RGB(91, 140, 255)), .doing(nil), .doing(TLPaint.trip), .doing(RGB(240, 176, 79)), .doing(RGB(111, 168, 200))])
        let fills = m.scene.pieces.map(\.fill)
        XCTAssertEqual(fills.first, .sleep)
        XCTAssertTrue(fills.contains(.doing(RGB(91, 140, 255))))
        XCTAssertFalse(m.scene.pieces.contains { $0.id == "s-home" }, "no place pieces")
        let work = try XCTUnwrap(m.scene.pieces.first { $0.id == "d2" })
        XCTAssertEqual([work.name, work.icon], ["做 Jarvis", "laptopcomputer"], "a piece wears what he was doing and its category's symbol")
        let hole = try XCTUnwrap(m.scene.pieces.first { $0.t0 == 720 })
        XCTAssertEqual(hole.fill, .blank, "drawn like where nothing was recorded")
        XCTAssertNil(hole.icon)
        XCTAssertNotNil(TLPaint.look(.doing(RGB(91, 140, 255))))
    }

    func testOneOffThingsOfThePastAreDotsOnTheBarAndWhatIsToComeIsNot() throws {
        let m = try day()
        XCTAssertEqual(m.scene.dots, [TLDot(lens: .picture, t: 600), TLDot(lens: .payment, t: 800)],
                       "a picture and a payment he made; the talk, the call and the work have a length, a message or a reminder has no chip")
        let pay = #""amount": "5.00", "currency": "CAD", "direction": "out""#
        let later = DayTimelineModel.build(try reply([item("p-past", "payment", 800, 800, pay), item("p-later", "payment", 1000, 1000, pay)]), now: 900)
        XCTAssertEqual(later.scene.dots, [TLDot(lens: .payment, t: 800)], "after now there is no dot: what is planned is drawn as it is")
        let money = #""amount": "5.00", "currency": "CAD", "direction": "in""#
        XCTAssertTrue(DayTimelineModel.build(try reply([item("p-in", "payment", 800, 800, money)]), now: 900).scene.dots.isEmpty, "money in is in no chip")
    }

    func testTheBarCanBeDrawnFromWhereHeWasAndTheListIsTheSame() throws {
        let m = try day()
        XCTAssertTrue(m.hasPlaces)
        let by = m.byPlace
        XCTAssertEqual(by.entries.filter { $0.kind != .stay && $0.kind != .trip }.map(\.id), m.entries.map(\.id), "nothing is added to the list's rows but the places for names")
        XCTAssertEqual(m.scene(places: false), m.scene, "what he was doing is the day's own bar")
        let ids = by.scene.pieces.map(\.id)
        XCTAssertEqual(Array(ids.prefix(3)), ["n", "s-home", "r1"], "the night, the stay, the ride")
        XCTAssertTrue(ids.contains("s-cafe"))
        XCTAssertFalse(ids.contains("d2"), "no stretches of what he was doing")
        let stay = try XCTUnwrap(by.scene.pieces.first { $0.id == "s-home" })
        XCTAssertEqual([stay.name, stay.icon], ["家", "house.fill"], "the stay wears its place and its symbol")
        guard case .place(_, let kept) = stay.fill else { return XCTFail("a place colour") }
        XCTAssertTrue(kept)
        XCTAssertEqual(by.scene.pieces.first { $0.id == "r1" }?.fill, .trip)
        XCTAssertEqual(by.readout(at: 450).head, "家 · 07:00–12:40 · 5 时 40 分", "a finger reads the stay, not the stretch of work")
        XCTAssertEqual(by.stretch(at: 450), "s-home")
        XCTAssertEqual(m.stretch(at: 450), "d1")
        XCTAssertEqual(by.scene.dots, m.scene.dots, "the dots are the same either way")
        // a day of records already is drawn from where he was: nothing to switch
        let records = DayTimelineModel.build(try reply([item("s", "stay", 420, 760, home)]), now: 900)
        XCTAssertFalse(records.hasPlaces)
        XCTAssertEqual(records.byPlace, records)
    }

    func testAFingerOnTheBarReadsWhatHeWasDoingItsHoursAndLength() throws {
        let m = try day()
        let r = m.readout(at: 600)
        XCTAssertEqual(r.head, "做 Jarvis · 09:00–12:00 · 3 时")
        XCTAssertEqual(r.hue, RGB(91, 140, 255))
        XCTAssertTrue(r.strong)
        let hole = m.readout(at: 730)
        XCTAssertEqual(hole.head, "不知道在干嘛 · 12:00–12:40 · 40 分")
        XCTAssertNil(hole.hue)
        XCTAssertEqual(m.readout(at: 880).head, "歇会儿 · 13:50 起 · 1 时 10 分", "the one that goes on")
        XCTAssertTrue(m.readout(at: 600).elsewhere.contains { $0.hasPrefix("Jarvis") }, "what else was going on is still told")
    }

    func testTheDaysNumbersStillFindTheirThingsInsideTheStretches() throws {
        let m = try day()
        XCTAssertEqual(try entry(m, "d2").lenses, [.work, .picture, .talk], "a talk too, though it is not drawn")
        XCTAssertEqual(m.marks(of: .talk), [TLMark(t0: 610, t1: 620)])
        XCTAssertEqual(m.marks(of: .picture), [TLMark(t0: 600, t1: 600)])
        XCTAssertEqual(Set(m.chips.map(\.lens)), [.sleep, .trip, .work, .call, .talk, .payment, .picture], "the chips are the day's, not a stretch's")
        XCTAssertEqual(m.chips.first { $0.lens == .talk }?.value, "1 次")
    }

    func testTheBarStartsAtTheFirstStretchAndAnItemInsideOneHasItsStarOnTheBar() throws {
        let early = DayTimelineModel.build(try reply([item("n", "sleep", -60, 240, "")], spans: [span("a", 250, 300, "x", "work")]), now: 900)
        XCTAssertEqual(early.span.lowerBound, 240, "the hour the first stretch begins in, when that is before six")
        let json = #"{"date": "2026-10-08", "tz": "\#(Cal.cur.cal.timeZone.identifier)", "start_ms": \#(base), "end_ms": \#(base + 86_400_000), "now_ms": \#(at(900)), "missing": [], "items": [\#(item("w1", "work", 560, 700, #""active_s": 6000"#))], "highlights": [{"id": "w1", "label": "", "by": "him"}], "spans": [\#(span("d2", 540, 720, "Jarvis", "work", items: ["w1"]))]}"#
        let starred = try JSONDecoder().decode(DayReply.self, from: Data(json.utf8))
        XCTAssertEqual(DayTimelineModel.build(starred, now: 900).stars.map(\.t), [560])
    }
    // MARK: the made-up day of the debug scene

    func testTheMadeUpDayOfTheDebugSceneIsStretchesBackToBackThatListOnlyItemsTheDayHas() throws {
        let r = TimelineFixture.today(cal, doing: true)
        let m = DayTimelineModel.build(r, now: TimelineFixture.now)
        let stretches = m.entries.filter { $0.kind == .segment }
        XCTAssertEqual(stretches.count, 13)
        XCTAssertEqual(stretches.first?.t0, 545, "from when he woke")
        for (a, b) in zip(stretches, stretches.dropFirst()) { XCTAssertEqual(a.t1, b.t0, "\(a.id) then \(b.id)") }
        XCTAssertEqual(stretches.last?.t1, TimelineFixture.now, "to now")
        let ids = Set(r.items.map(\.id))
        for s in r.spans { for id in s.item_ids ?? [] { XCTAssertTrue(ids.contains(id), "\(s.id) lists \(id)") } }
        XCTAssertEqual(r.spans.filter { $0.item_ids?.contains("stay-9") == true }.count, 4, "one stay under four stretches")
        XCTAssertEqual(r.spans.filter { $0.item_ids?.contains("code-s1") == true }.count, 2, "and a session that begins in one and ends in the next")
        XCTAssertEqual(m.waiting, ["reminder-demo-2", "event-demo-1"], "what waits on him is a row of its own")
        XCTAssertEqual(stretches.filter(\.unknown).map(\.id), ["doing-1130"])
        XCTAssertEqual(stretches.map(\.title).filter { $0 == "歇一会儿" }.count, 1)
        XCTAssertTrue(stretches.contains { $0.detail?.contains("不太确定") == true })
        XCTAssertEqual(stretches.first { $0.id == "doing-835" }?.counts, TLCounts(pictures: 0, payments: 0, calls: 1, messages: 1))
    }
}
