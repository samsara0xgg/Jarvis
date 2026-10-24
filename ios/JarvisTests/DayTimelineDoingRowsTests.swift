import XCTest
@testable import Jarvis

/// The day's list as one row for each stretch of what he was doing (ADR 0302, ADR 0307): the rows up to now and their groups, their titles and the line under them, what an opened stretch
/// holds and what it asks him, how she knows, the bar by category, and that a brain with no stretches leaves the list as it always was.
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
                      items: [String] = [], clues: [String] = [], question: (String, [String])? = nil, kind: String = "doing", lane: String = "doing") -> String {
        func q(_ s: String?) -> String { s.map { "\"\($0)\"" } ?? "null" }
        let list = { (xs: [String]) in "[" + xs.map { "\"\($0)\"" }.joined(separator: ",") + "]" }
        let cl = "[" + clues.map { #"{"kind": "words", "at_ms": 1, "text": "\#($0)"}"# }.joined(separator: ",") + "]"
        let ask = question.map { #"{"text": "\#($0.0)", "options": \#(list($0.1))}"# } ?? "null"
        return #"{"id": "\#(id)", "lane": "\#(lane)", "kind": "\#(kind)", "start_ms": \#(at(from)), "end_ms": \#(to.map { String(at($0)) } ?? "null"), "title": \#(q(title)), "detail": \#(q(detail)), "category": "\#(category)", "basis": "\#(basis)", "confidence": "\#(confidence)", "confirmed": false, "clues": \#(cl), "question": \#(ask), "place_id": \#(q(place)), "item_ids": \#(list(items))}"#
    }
    private let home = #""place": "12 Example St", "kept_place": {"id": "p-home", "name": "家", "role": "home"}"#

    /// The made-up day at 15:00. The night; a long stay at home (a place he keeps, which the brain points to by its id) that is under three stretches; a work stretch with a picture, a talk and
    /// a Claude Code session that goes on into the next stretch; a hole nobody could say anything of; a ride; a meal he told her at a cafe he keeps no place at; and a rest with his phone, a
    /// message, a call, a song and a reminder that waits on him. After now an event and a reminder.
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
            span("d1", 420, 540, "起床吃早饭", "meal", basis: "inferred", confidence: "medium", place: "p-home", items: ["s-home"]),
            span("d2", 540, 720, "Jarvis", "work", confidence: "low", detail: "一直在改时间线", place: "p-home", items: ["s-home", "w1", "c1", "pic1", "t1", "ghost", "w1"], clues: ["Xcode 在前台", "27 路之前一直在家"]),
            span("d3", 720, 760, nil, "unknown", place: "p-home", items: ["s-home", "c1"]),
            span("d4", 760, 790, "27", "commute", items: ["r1"]),
            span("d5", 790, 830, "吃午饭", "meal", basis: "stated", items: ["s-cafe", "pay1"]),
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
        XCTAssertEqual(m.entries.map(\.id), ["n", "place-d1", "d1", "d2", "d3", "d4", "d5", "d6", "rem-rang", "now", "ev-later", "rem-due"],
                       "the stay, the work, the ride, the picture and the talk are inside a stretch, not rows; the three at home are under its name; what waits on him keeps its place; the plan is as it was; a plan span is no row")
        XCTAssertEqual(m.entries.map(\.kind), [.sleep, .stay, .segment, .segment, .segment, .segment, .segment, .segment, .reminder, .now, .event, .reminder])
        XCTAssertEqual(try entry(m, "n").title, "睡觉", "the night as it was")
        XCTAssertEqual(m.waiting, ["rem-rang"], "and the pill that counts it")
        XCTAssertFalse(m.entries.contains { $0.kind == .talk }, "a talk is never drawn")
    }

    func testARowSaysWhenWhatTheyDidWhereAndHowLong() throws {
        let m = try day()
        let d1 = try entry(m, "d1"), d2 = try entry(m, "d2"), d3 = try entry(m, "d3"), d4 = try entry(m, "d4"), d5 = try entry(m, "d5"), d6 = try entry(m, "d6")
        XCTAssertEqual([d1.time, d2.time, d3.time, d4.time, d5.time, d6.time], ["07:00", "09:00", "12:00", "12:40", "13:10", "13:50"])
        XCTAssertEqual([d1.title, d2.title, d3.title, d4.title, d5.title, d6.title], ["起床吃早饭", "做 Jarvis", "没有记录", "27 路", "吃午饭", "歇会儿"],
                       "a model's phrase or his own answer as given, the program's words as the rows say them, and none: 没有记录")
        XCTAssertEqual([d1.unknown, d3.unknown, d4.unknown], [false, true, false], "only the one with no words is muted")
        XCTAssertEqual([d1.right, d2.right, d3.right, d4.right, d5.right, d6.right], ["2 小时", "3 小时", "40 分钟", "30 分钟", "40 分钟", "1 小时 10 分"], "how long; the last goes on to now")
        XCTAssertTrue(d6.going)
        XCTAssertEqual([d1.at, d2.at, d3.at], ["家", "家", "家"], "the place he keeps that the brain points to")
        XCTAssertEqual([d1.detail, d2.detail, d3.detail], [nil, "一直在改时间线", nil], "under its name the line no longer says the place: only the model's line, where it has one")
        XCTAssertNil(d4.detail, "nowhere: the trip")
        XCTAssertEqual(d5.detail, "Cafe Lune", "a place he keeps none at: the stay that holds most of it, by the phone's first part of its label")
        XCTAssertNil(d6.detail)
        XCTAssertFalse([d1, d2, d3, d4, d5, d6].contains(where: \.unsure), "what the program saw is never unsure, though it says its confidence is low")
    }

    func testTwoStretchesOrMoreAtAPlaceHeKeepsAreAGroupUnderItsNameAndEveryOtherRowStandsAlone() throws {
        let m = try day()
        let h = try entry(m, "place-d1")
        XCTAssertTrue(h.header)
        XCTAssertEqual([h.title, h.time, h.right], ["家", "07:00", "07:00–12:40 · 5 小时 40 分"], "its name, when the first began, and its hours and length")
        XCTAssertEqual(h.base, TLMetrics.header)
        XCTAssertEqual(h.gap, TLMetrics.gap)
        XCTAssertEqual(h.actions, [.ask], "a place is put to her; it does not open")
        XCTAssertEqual([h.about?.id, h.about?.title], ["s-home", "家"])
        XCTAssertFalse(h.opens || h.expands || h.touchable)
        guard case .place(let hue) = h.rail else { return XCTFail("the place's rail from its dot") }
        XCTAssertEqual(h.color, hue)
        XCTAssertTrue(h.railOut)
        for id in ["d1", "d2", "d3"] {
            let e = try entry(m, id)
            XCTAssertEqual(e.rail, .place(hue), id)
            XCTAssertTrue(e.railIn, id)
            XCTAssertEqual(e.gap, 0, id)
            XCTAssertEqual(e.group, "p-home", id)
        }
        XCTAssertEqual([try entry(m, "d1").railOut, try entry(m, "d2").railOut, try entry(m, "d3").railOut], [true, true, false], "the rail ends at its last")
        // the trip is a group of its own on its dashed rail, which reaches through the blanks either side of it; every other row is a group of its own, with no rail
        let d4 = try entry(m, "d4"), d5 = try entry(m, "d5")
        XCTAssertEqual([d4.rail, d4.gapRail, d5.gapRail], [.trip, .trip, .trip])
        XCTAssertEqual([d5.rail, try entry(m, "d6").rail], [.none, .none])
        XCTAssertEqual(try ["d4", "d5", "d6", "rem-rang", "now", "ev-later", "rem-due"].map { try entry(m, $0).gap }, [TLMetrics.gap, TLMetrics.gap, TLMetrics.gap, TLMetrics.gap, TLMetrics.nowGap, 0, 0],
                       "12 pt above a group, 10 above the line for now, and none above what is still to come")
        // one stretch at a place is no group: its line says the place
        let one = DayTimelineModel.build(try reply([item("s-home", "stay", 420, 760, home)], spans: [span("a", 420, 500, "Jarvis", "work", place: "p-home", items: ["s-home"]),
                                                                                                    span("b", 500, 560, "歇会儿", "rest")]), now: 900)
        XCTAssertEqual(one.entries.map(\.id), ["a", "b", "now"])
        XCTAssertEqual(try entry(one, "a").detail, "家")
        XCTAssertEqual(try entry(one, "a").rail, .none)
    }

    func testAStretchSheIsNotSureOfAsksHimInOneLineAndAHoleAsksToBeFilledIn() throws {
        let m = try day()
        XCTAssertEqual(try entry(m, "d3").ask, TLAsk(text: nil, options: [], yes: nil, hole: true), "nothing was recorded: a line to fill in")
        XCTAssertNil(try entry(m, "d2").ask, "the program saw it: nothing to ask")
        let r = try reply([], spans: [
            span("guess", 420, 500, "写作业", "study", basis: "inferred", confidence: "low"),
            span("asks", 500, 560, "歇会儿", "rest", basis: "inferred", question: ("这段你在做什么？", ["休息", "刷手机"])),
            span("sure", 560, 600, "Jarvis", "work", confidence: "low"),
        ])
        let s = DayTimelineModel.build(r, now: 900)
        let guess = try entry(s, "guess"), asks = try entry(s, "asks"), sure = try entry(s, "sure")
        XCTAssertEqual([guess.unsure, asks.unsure, sure.unsure], [true, true, false], "a guess of low confidence, or one the brain asks about; never what the program saw")
        XCTAssertEqual(guess.ask, TLAsk(text: nil, options: [], yes: "写作业", hole: false), "her own words put to him, with 是")
        XCTAssertEqual(asks.ask, TLAsk(text: "这段你在做什么？", options: ["休息", "刷手机"], yes: nil, hole: false), "the brain's question and its answers")
        XCTAssertNil(sure.ask)
        XCTAssertTrue(guess.expands && asks.expands, "it opens where it stands to ask")
        XCTAssertFalse(sure.expands, "nothing inside it and nothing to ask: a tap frames it on the bar")
        XCTAssertEqual(guess.title, "写作业", "the question mark is drawn after the title, not written into it")
    }

    func testThePageOfARowSaysItsHoursHowLongAndWhere() throws {
        let m = try day()
        XCTAssertEqual(try entry(m, "d2").pageLine, "09:00–12:00 · 3 小时 · 家 · 一直在改时间线", "on a page of its own it says the place its group says in the list")
        XCTAssertEqual(try entry(m, "d4").pageLine, "12:40–13:10 · 30 分钟", "no place: the hours and the length")
        XCTAssertEqual(try entry(m, "d6").pageLine, "13:50 起 · 1 小时 10 分", "a stretch that goes on says when it began")
    }

    func testARowIsAsTallAsWhatItSays() throws {
        let m = try day()
        XCTAssertEqual(try entry(m, "d4").base, TLMetrics.row, "nothing under the title: a plain row")
        XCTAssertEqual(try entry(m, "d2").base, TLMetrics.rowDetail, "a line under it: a row with a line")
    }

    func testCategoriesAreDrawnInTheirColourAndSymbolAndACommuteWearsItsWay() throws {
        let m = try day()
        let want: [(String, RGB?, String)] = [("d1", RGB(240, 176, 79), "fork.knife"), ("d2", RGB(91, 140, 255), "briefcase"), ("d3", nil, "questionmark"),
                                              ("d4", TLPaint.trip, "bus"), ("d5", RGB(240, 176, 79), "fork.knife"), ("d6", RGB(111, 168, 200), "sofa")]
        for (id, color, symbol) in want {
            let e = try entry(m, id)
            XCTAssertEqual(e.color, color, id)
            XCTAssertEqual(e.symbol, symbol, id)
        }
        let table: [(String, String, RGB?)] = [("work", "briefcase", RGB(91, 140, 255)), ("study", "book", RGB(155, 123, 255)), ("rest", "sofa", RGB(111, 168, 200)), ("social", "person.2", RGB(233, 121, 166)),
                                               ("meal", "fork.knife", RGB(240, 176, 79)), ("home", "house", RGB(168, 159, 145)), ("sleep", "moon", TLPaint.sleep), ("exercise", "figure.run", RGB(76, 195, 138)),
                                               ("errand", "bag", RGB(232, 135, 90)), ("commute", "arrow.right", TLPaint.trip), ("unknown", "circle.dashed", nil), ("something new", "circle.dashed", nil)]
        for (name, symbol, color) in table { XCTAssertEqual(TLCategory.of(name), TLCategory(color: color, symbol: symbol), name) }
        XCTAssertFalse(table.contains { ["laptopcomputer", "iphone", "ipad"].contains($0.1) }, "what he was doing, never the device")
        // a commute by the way he went on its longest leg
        func way(_ legs: [(String, Double, Double)]) throws -> String {
            let items = legs.enumerated().map { k, l in item("m\(k)", "move", l.1, l.2, #""activity": "\#(l.0)""#) }
            return try entry(DayTimelineModel.build(try reply(items, spans: [span("c", 600, 700, "回家", "commute", items: items.indices.map { "m\($0)" })]), now: 900), "c").symbol
        }
        XCTAssertEqual(try way([("walking", 600, 620)]), "figure.walk", "walks alone")
        XCTAssertEqual(try way([("walking", 600, 610), ("automotive", 610, 650)]), "car", "a drive, the longest leg, after a short walk")
        XCTAssertEqual(try way([("walking", 600, 640), ("automotive", 640, 650)]), "figure.walk", "a long walk to a short drive")
        XCTAssertEqual(try way([("cycling", 600, 640)]), "bicycle")
        XCTAssertEqual(try entry(DayTimelineModel.build(try reply([], spans: [span("c", 600, 640, "路上", "commute")]), now: 900), "c").symbol, "arrow.right", "no trip to tell the way")
    }

    func testAStretchThatReachesPastNowEndsThereAndOneThatBeginsAfterItIsNotARow() throws {
        let r = try reply([], spans: [span("a", 420, 960, "Jarvis", "work"), span("b", 905, 960, "歇会儿", "rest", basis: "inferred")])
        let m = DayTimelineModel.build(r, now: 900)
        XCTAssertEqual(m.entries.map(\.id), ["a", "now"])
        XCTAssertEqual(try entry(m, "a").t1, 900)
        XCTAssertEqual(try entry(m, "a").right, "8 小时")
        let other = DayTimelineModel.build(r, now: nil)
        XCTAssertEqual(other.entries.map(\.id), ["a", "b"], "on another day they are as long as they are")
        XCTAssertEqual([try entry(other, "a").right, try entry(other, "b").right], ["9 小时", "55 分钟"])
    }

    // MARK: opened

    func testAnOpenedStretchHoldsTheModelsLineThenWhatIsInsideItInTimeOrder() throws {
        let m = try day()
        let d2 = try entry(m, "d2")
        XCTAssertEqual(d2.note, "一直在改时间线")
        XCTAssertEqual(d2.lines.map(\.id), ["w1", "pic1", "c1"], "the work block, the picture and the session by when they began; the stay and the talk are left out, an id that is not among the day's items is skipped, and one listed twice is once")
        XCTAssertEqual(d2.lines.map(\.title), ["在 Mac 上工作", "照片", "接上天气"])
        XCTAssertEqual(d2.lines[0].detail, "Jarvis · Xcode", "as its row says it")
        XCTAssertEqual(d2.lines[0].right, "活跃 1 小时 40 分", "it began and ended inside the stretch: what its row says")
        XCTAssertEqual(d2.lines.map(\.time), ["09:20", "10:00", "10:50"], "each line has the time it began")
        XCTAssertEqual(d2.lines.map(\.mark), [.icon("laptopcomputer"), .icon("photo.fill"), .claude(cloud: false)], "and its row's icon; a session the Claude mark")
        XCTAssertEqual(d2.lines[1].right, "", "a moment has nothing at the right")
        XCTAssertEqual(d2.lines[1].attachment, "pic-1", "and the picture its thumbnail")
        XCTAssertEqual(d2.lines.map(\.span), [560...700, 600...600, 650...720], "what a tap on it frames on the bar: its time inside the stretch")
        XCTAssertNil(try entry(m, "d1").note)
        XCTAssertEqual(try entry(m, "d1").lines, [], "a stay alone is nothing to list")
    }

    func testALastingItemThatRunsBeyondTheStretchShowsItsLengthInsideIt() throws {
        let m = try day()
        let a = try entry(m, "d2").lines.first { $0.id == "c1" }, b = try entry(m, "d3").lines.first { $0.id == "c1" }
        XCTAssertEqual(a?.right, "1 小时 10 分", "10:50 to 12:20, the stretch ends at 12:00: 70 minutes of it")
        XCTAssertEqual(b?.right, "20 分钟", "and the same session in the next: 12:00 to 12:20")
        XCTAssertEqual(b?.time, "12:00", "begun before the stretch: its time is the stretch's start")
        XCTAssertEqual(try entry(m, "d3").lines.map(\.id), ["c1"])
        XCTAssertEqual(try entry(m, "d2").lines.first { $0.id == "w1" }?.right, "活跃 1 小时 40 分", "one that does not run beyond is told as its row tells it")
    }

    func testTheKindsOfTheDevicesAreLinesWithTheirOwnWords() throws {
        let m = try day()
        let d6 = try entry(m, "d6")
        XCTAssertEqual(d6.lines.map(\.id), ["scr1", "med1", "rem-rang", "msg1", "pc1", "call1"], "by when they began; the reminder that rang is a row of its own too, and is in its stretch")
        let by = Dictionary(uniqueKeysWithValues: d6.lines.map { ($0.id, $0) })
        XCTAssertEqual([by["scr1"]?.title, by["scr1"]?.detail, by["scr1"]?.right], ["用 iPhone", "微信 22 分钟 · Safari 8 分钟", "30 分钟"], "phone use: the device, the apps and minutes")
        XCTAssertEqual([by["med1"]?.title, by["med1"]?.detail, by["med1"]?.right], ["Music", "歌名 · 歌手", "25 分钟"], "media: the app and the top title")
        XCTAssertEqual([by["msg1"]?.title, by["msg1"]?.detail, by["msg1"]?.right], ["Mia", "发 3 · 收 5", "8 条"], "messages: the conversation, what he sent and what came, and how many: the span of a burst is no time spent")
        XCTAssertEqual([by["pc1"]?.title, by["pc1"]?.detail, by["pc1"]?.right], ["Mia", "打出 · Phone", "10 分钟"], "a call on the phone: the person, which way, how long")
        XCTAssertEqual([by["call1"]?.title, by["call1"]?.right], ["通话 · FaceTime", "15 分钟"], "a call on the Mac has no person: the app and how long")
        XCTAssertEqual([by["rem-rang"]?.title, by["rem-rang"]?.detail, by["rem-rang"]?.time, by["rem-rang"]?.right], ["吃药", "响过了", "14:10", ""])
        XCTAssertEqual(by["scr1"]?.mark, .icon("iphone"))
    }

    func testClaudeCodeIsALineASessionHeTypedInByItsTitleWithTheOnesThatRanByThemselvesInOne() throws {
        let items = [
            item("c1", "code", 600, 640, #""title": "接上天气", "project": "Jarvis", "prompts": 6"#),
            item("c2", "code", 630, 700, #""project": "Jarvis", "prompts": 2"#),
            item("c3", "code", 650, 670, #""title": "第三章", "project": "论文", "prompts": 1"#),
            item("c4", "code", 610, 690, #""project": "Jarvis", "prompts": 0"#),
            item("c5", "code", 620, 625, #""project": "Jarvis", "prompts": 0"#),
        ]
        let d = try entry(DayTimelineModel.build(try reply(items, spans: [span("d", 540, 720, "Jarvis", "work", items: ["c1", "c2", "c3", "c4", "c5"])]), now: 900), "d")
        XCTAssertEqual(d.lines.map(\.title), ["接上天气", "", "", "第三章"], "each session he typed in is a line by its own title (none: the view says it is unnamed); never `Claude Code`, never the repo")
        XCTAssertEqual(d.lines.map(\.said), [.session(cloud: false, prompts: 6), .background(2), .session(cloud: false, prompts: 2), .session(cloud: false, prompts: 1)],
                       "the two that ran by themselves are one line, at the first of them")
        XCTAssertEqual(d.lines.map(\.mark), Array(repeating: .claude(cloud: false), count: 4), "the Claude mark, no cloud on the Mac's own")
        XCTAssertEqual(d.lines.map(\.detail), [nil, nil, nil, nil], "what it ran as is said only when the line is tapped")
        XCTAssertEqual(d.lines.map(\.right), ["40 分钟", "", "1 小时 10 分", "20 分钟"])
        let bridged = [item("r1", "code", 590, 650, #""title": "", "project": "bridge-cse_a", "prompts": 4, "remote_session": "cse_a""#),
                       item("r2", "code", 640, 700, #""title": "", "project": "bridge-cse_b", "prompts": 0, "remote_session": "cse_b""#),
                       item("r3", "code", 660, 680, #""title": "", "thread_title": "部署", "project": "bridge-cse_a", "prompts": 2, "remote_session": "cse_a""#)]
        let withBridges = try entry(DayTimelineModel.build(try reply(items + bridged, spans: [span("d", 540, 720, "Jarvis", "work", items: ["c1", "c2", "c3", "c4", "c5", "r1", "r2", "r3"])]), now: 900), "d")
        XCTAssertEqual(withBridges.lines.map(\.id), ["d-thread-cse_a", "c1", "d-code-alone", "c2", "d-thread-cse_b", "c3"], "one line for each claude.ai thread a bridge ran, typed in or not")
        let a = withBridges.lines[0], b = withBridges.lines[4]
        XCTAssertEqual([a.title, b.title], ["部署", ""], "the thread's own name where the brain has it; the older one has none")
        XCTAssertEqual([a.said, b.said], [.session(cloud: true, prompts: 6), .session(cloud: true, prompts: 0)])
        XCTAssertEqual(a.mark, .claude(cloud: true), "with the cloud on its mark")
        XCTAssertEqual([a.right, a.time], ["1 小时 20 分", "09:50"], "its sessions' time once, from the first of them")
        XCTAssertEqual(withBridges.lines[2].said, .background(2), "a bridge's session that ran by itself is its thread's, not one of the Mac's own")
        XCTAssertFalse(withBridges.lines.contains { $0.title.contains("bridge-cse") || ($0.detail ?? "").contains("bridge-cse") }, "no worktree's name anywhere")
        let old = try entry(DayTimelineModel.build(try reply([item("c1", "code", 600, 640, #""title": "接上天气", "project": "bridge-cse_01""#)], spans: [span("d", 540, 720, "Jarvis", "work", items: ["c1"])]), now: 900), "d")
        XCTAssertEqual(old.lines.map { [$0.title, $0.detail] }, [["接上天气", nil]], "a brain that does not say `prompts`: each session its own line, without the worktree's name")
        XCTAssertEqual(old.lines.map(\.said), [.session(cloud: false, prompts: nil)])
    }

    func testMessagesThatOnlyCameInOfOneAppAtAboutTheSameTimeAreOneLine() throws {
        let items = [
            item("m1", "messages", 840, 842, #""app": "小红书", "conversation": "A", "sent": 0, "received": 3"#),
            item("m2", "messages", 846, 848, #""app": "小红书", "conversation": "B", "sent": 0, "received": 5"#),
            item("m3", "messages", 850, 851, #""app": "小红书", "conversation": "A", "received": 4"#),
            item("m4", "messages", 845, 847, #""app": "微信", "conversation": "Mia", "sent": 2, "received": 1"#),
            item("m5", "messages", 880, 881, #""app": "小红书", "conversation": "C", "sent": 0, "received": 2"#),
        ]
        let d = try entry(DayTimelineModel.build(try reply(items, spans: [span("d", 830, 899, "歇会儿", "rest", items: ["m1", "m2", "m3", "m4", "m5"])]), now: 900), "d")
        XCTAssertEqual(d.lines.map(\.id), ["d-heard-m1", "m4", "m5"], "three bursts that only came in, each within 10 minutes of the one before, are one line; one he wrote in and one later stay lines of their own")
        XCTAssertEqual(d.lines[0].said, .received("小红书", people: 2, count: 12))
        XCTAssertEqual(d.lines.map(\.right), ["", "3 条", "2 条"], "a burst says how many, not how long")
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

    func testAStretchOffersToBeAskedAboutAndToBeSaid() throws {
        let m = try day()
        let d2 = try entry(m, "d2"), d4 = try entry(m, "d4")
        XCTAssertEqual(d2.actions, [.ask, .sayWhat], "put to her as the stretch itself, by its window (ADR 0410), and the quiet 这段在干嘛")
        XCTAssertEqual(d2.about?.kind, "doing")
        XCTAssertEqual(d2.about?.id, "d2")
        XCTAssertEqual(d2.about?.title, "做 Jarvis")
        XCTAssertEqual(d2.about?.startMs, at(540))
        XCTAssertEqual(d2.placeAbout?.id, "s-home", "the place it was at is still what its group is asked about")
        XCTAssertEqual(d4.about?.kind, "doing", "with no place, the same")
        XCTAssertTrue(d2.opens && d2.expands, "three lines inside it: it opens where it stands")
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
        XCTAssertEqual([work.name, work.icon], ["做 Jarvis", "briefcase"], "a piece wears what he was doing and its category's symbol")
        let hole = try XCTUnwrap(m.scene.pieces.first { $0.t0 == 720 })
        XCTAssertEqual(hole.fill, .blank, "drawn like where nothing was recorded")
        XCTAssertEqual(hole.icon, "questionmark", "with a question mark")
        XCTAssertNotNil(TLPaint.look(.doing(RGB(91, 140, 255))))
    }

    func testAFingerOnTheBarReadsWhatHeWasDoingItsHoursAndLength() throws {
        let m = try day()
        let r = m.readout(at: 600)
        XCTAssertEqual(r.head, "做 Jarvis · 09:00–12:00 · 3 小时")
        XCTAssertEqual(r.hue, RGB(91, 140, 255))
        XCTAssertTrue(r.strong)
        let hole = m.readout(at: 730)
        XCTAssertEqual(hole.head, "没有记录 · 12:00–12:40 · 40 分钟")
        XCTAssertNil(hole.hue)
        XCTAssertEqual(m.readout(at: 880).head, "歇会儿 · 13:50 起 · 1 小时 10 分", "the one that goes on")
        XCTAssertEqual(r.elsewhere, ["一直在改时间线"], "a stretch's own line says what else there was, in words")
        XCTAssertEqual(m.readout(at: 870).elsewhere.first, "通话 · FaceTime", "one with no line of its own: what was going on there, the most telling first")
    }

    func testTheDaysNumbersStillFindTheirThingsInsideTheStretches() throws {
        let m = try day()
        XCTAssertEqual(try entry(m, "d2").lenses, [.work, .picture, .talk], "a talk too, though it is not drawn")
        XCTAssertEqual(m.marks(of: .talk), [TLMark(t0: 610, t1: 620)])
        XCTAssertEqual(m.marks(of: .picture), [TLMark(t0: 600, t1: 600)])
        XCTAssertEqual(Set(m.chips.map(\.lens)), [.sleep, .trip, .work, .call, .talk, .payment, .picture], "the chips are the day's, not a stretch's")
        XCTAssertEqual(m.chips.first { $0.lens == .talk }?.value, "1 次")
    }

    func testTheBarStartsAtTheFirstStretch() throws {
        let early = DayTimelineModel.build(try reply([item("n", "sleep", -60, 240, "")], spans: [span("a", 250, 300, "x", "work")]), now: 900)
        XCTAssertEqual(early.span.lowerBound, 240, "the hour the first stretch begins in, when that is before six")
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
        XCTAssertEqual(stretches.filter(\.unsure).map(\.id), ["doing-835", "doing-1055"], "the rest it asks about and the homework it is not sure of")
        XCTAssertEqual(stretches.first { $0.id == "doing-835" }?.ask?.options, ["休息", "刷手机", "等车"])
        XCTAssertEqual(m.entries.filter(\.header).map(\.title), ["家", "UVic", "家"], "the morning at home, the afternoon at school and the evening at home are groups")
    }

    func testNowIsTheStretchThatEndsExactlyAtNowNotAMomentRowBegunLater() throws {
        let m = DayTimelineModel.build(TimelineFixture.today(cal, doing: true), now: TimelineFixture.now)
        let last = try XCTUnwrap(m.entries.last { $0.kind == .segment })
        XCTAssertEqual(last.t1, TimelineFixture.now)
        XCTAssertNil(m.seg(at: TimelineFixture.now), "the half-open stretch misses its own end")
        XCTAssertEqual(m.nowRow()?.id, last.id)
        XCTAssertNil(DayTimelineModel.build(TimelineFixture.today(cal, doing: true), now: nil).nowRow(), "a day before today has no now")
        let plain = DayTimelineModel.build(TimelineFixture.today(cal), now: TimelineFixture.now)
        XCTAssertEqual(plain.nowRow()?.title, "家", "a stay still going on is the stretch now is in")
    }
}
