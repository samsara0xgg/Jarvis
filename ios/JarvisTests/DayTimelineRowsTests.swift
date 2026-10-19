import XCTest
@testable import Jarvis

/// The rows of the day, one for every kind the brain serves, in groups by the place and runs by the trip: the short-stop rule, who waits on him, what opening a row offers,
/// how long until what is coming, the new data, and that an item the phone cannot make sense of costs that item and nothing else.
final class DayTimelineRowsTests: XCTestCase {
    private var cal: Cal { Cal.cur }
    private var base: Int64 { Int64(cal.origin.timeIntervalSince1970 * 1000) }
    private func at(_ minute: Double) -> Int64 { base + Int64(minute * 60_000) }
    private let now = TimelineFixture.now

    private func reply(_ items: String, _ extra: String = "") throws -> DayReply {
        let day = #"{"date": "2026-10-08", "tz": "\#(Cal.cur.cal.timeZone.identifier)", "start_ms": \#(base), "end_ms": \#(base + 86_400_000), "now_ms": \#(at(now)), "missing": [], "items": [\#(items)]\#(extra)}"#
        return try JSONDecoder().decode(DayReply.self, from: Data(day.utf8))
    }
    private func build(_ r: DayReply, now: Double? = TimelineFixture.now, acked: Set<String> = [], renamed: [String: String] = [:]) -> DayTimelineModel {
        DayTimelineModel.build(r, now: now, topic: { $0 == "talk-1" ? "明天下午有什么安排？" : nil }, acked: acked, renamed: renamed)
    }
    /// one item as the brain serves it: its fields after the common ones are `more` (as JSON, without the braces)
    private func item(_ id: String, _ kind: String, _ from: Double, _ to: Double?, _ more: String = "") -> String {
        #"{"id": "\#(id)", "lane": "you", "kind": "\#(kind)", "state": "done", "start_ms": \#(at(from)), "end_ms": \#(to.map { String(at($0)) } ?? "null")\#(more.isEmpty ? "" : ", " + more)}"#
    }
    private func entry(_ m: DayTimelineModel, _ id: String) throws -> TLEntry { try XCTUnwrap(m.entries.first { $0.id == id }, id) }
    private let walk = #""activity": "walking""#
    private let home = #""place": "12 Example St", "kept_place": {"id": "p-home", "name": "家", "role": "home"}"#

    // MARK: a short stop is a row of the trip

    func testAStopUnderFifteenMinutesInNoPlaceHeKeepsBetweenTwoWaysIsARowOfTheTripAndNotAPlace() throws {
        let r = try reply([
            item("m1", "move", 600, 610, walk),
            item("A", "stay", 610, 617),                                                                                         // 7 minutes, no name at all
            item("m2", "move", 617, 625, walk),
            item("B", "stay", 625, 633, #""place": "Shell, Fort St, Victoria""#),                                                 // 8 minutes, a label from the phone only
            item("r1", "ride", 633, 650, #""route": "14""#),
            item("C", "stay", 650, 655, home),                                                                                   // 5 minutes, but a place he keeps: always a place
            item("m3", "move", 655, 660, walk),
            item("D", "stay", 660, 675, #""place": "Cafe""#),                                                                     // 15 minutes on the dot: a place
            item("m4", "move", 675, 680, walk),
            item("E", "stay", 680, 685, #""place": "Kiosk""#),                                                                    // a place follows it, not a way
            item("F", "stay", 685, 700, #""place": "Library""#),
            item("G", "stay", 700, 705, #""place": "Bench""#),                                                                    // a place came before it
            item("m5", "move", 705, 710, walk),
        ].joined(separator: ","))
        let m = build(r, now: nil)
        XCTAssertEqual(m.entries.map(\.kind), [.trip, .pause, .trip, .pause, .trip, .stay, .trip, .stay, .trip, .stay, .stay, .stay, .trip])
        let a = try entry(m, "A"), b = try entry(m, "B")
        XCTAssertEqual([a.title, b.title], ["停了 7 分", "停了 8 分"])
        XCTAssertEqual([a.detail, b.detail], [nil, "Shell"], "what the phone called it, if it did")
        XCTAssertEqual(a.time, "10:10")
        XCTAssertFalse(a.opens, "nothing to open on a stop")
        XCTAssertNil(a.lens)
        XCTAssertNil(a.about)
        XCTAssertEqual(a.rail, .trip)
        XCTAssertTrue(a.railIn && a.railOut, "it sits on the trip's rail between two legs")
        XCTAssertEqual(a.gap, 0, "inside the run")
        XCTAssertEqual(try entry(m, "m1").gap, 0, "the first row of the day has nothing above it to be apart from")
        XCTAssertEqual(m.entries.filter { $0.kind == .stay }.map(\.id), ["C", "D", "E", "F", "G"])
        // the bar still shows where he stopped, and a finger there reads it
        XCTAssertTrue(m.segs.contains { $0.id == "A" })
        XCTAssertEqual(m.readout(at: 613).head, "没名字的地方 · 10:10–10:17 · 7 分")
    }

    func testAStopStillGoingIsNeverARowOfTheTrip() throws {
        // today, begun 5 minutes ago and not ended: it is where he is now
        let r = try reply([item("m1", "move", 1280, 1300, walk), item("S", "stay", 1305, nil, #""place": "Bench""#)].joined(separator: ","))
        XCTAssertEqual(build(r).entries.map(\.kind), [.trip, .stay, .now])
        XCTAssertTrue(try entry(build(r), "S").going)
    }

    func testTheFragmentedDayIsFifteenStaysFourOfThemStopsOnTheWayElevenMovesAndTwoRides() throws {
        let m = DayTimelineModel.build(TimelineFixture.fragmented(cal), now: nil)
        XCTAssertEqual(m.entries.filter { $0.kind == .stay }.count, 11)
        XCTAssertEqual(m.entries.filter { $0.kind == .pause }.count, 4, "the stays under 15 minutes in no place he keeps, between two ways")
        XCTAssertEqual(m.entries.filter { $0.kind == .trip }.count, 13)
        XCTAssertEqual(m.entries.filter { $0.ride }.count, 2)
        XCTAssertEqual(m.segs.filter { if case .place = $0.kind { return true }; return false }.count, 15, "the bar still has all fifteen places")
        XCTAssertEqual(m.entries.filter { $0.kind == .stay && $0.kept }.map(\.title), ["家", "UVic", "家"])
        XCTAssertEqual(m.entries.filter { $0.kind == .pause }.map(\.title), ["停了 7 分", "停了 8 分", "停了 9 分", "停了 9 分"])
        XCTAssertEqual(m.entries.filter { $0.kind == .pause }.map(\.detail), [nil, "Yates St", nil, "Beach Dr"])
        // every leg and every stop is on a rail, and a run of them is one rail with a blank above its first row
        XCTAssertTrue(m.entries.filter { $0.kind == .trip || $0.kind == .pause }.allSatisfy { $0.rail == .trip })
        let first = try entry(m, "move-1"), second = try entry(m, "move-3")
        XCTAssertFalse(first.railIn || first.railOut, "a single leg between two places")
        XCTAssertEqual(first.gap, TLMetrics.gap)
        XCTAssertEqual([second.railIn, second.railOut], [false, true], "the first of a run of three")
        XCTAssertTrue(m.entries.allSatisfy { $0.base >= 44 || $0.kind == .now })
        XCTAssertEqual(m.entries.count, 11 + 4 + 13, "nothing else on the day")
    }

    func testADayOfTalksAlone() throws {
        let m = DayTimelineModel.build(TimelineFixture.talkOnly(cal), now: nil)
        XCTAssertEqual(m.entries.map(\.kind), [.talk, .talk, .talk])
        XCTAssertEqual(m.entries.map(\.gap), [0, 0, 0], "no group, no blank")
        XCTAssertTrue(m.segs.isEmpty)
        XCTAssertEqual(m.chips.map { $0.word + " " + $0.value }, ["和她 3 次"])
        XCTAssertEqual(m.span, 360...DAY, "no night: 06:00")
        XCTAssertEqual(m.readout(at: 780).head, "没有记录 · 06:00–24:00 · 18 时")
        XCTAssertEqual(m.readout(at: 780).second, "和她说话 · 12 句")
    }

    // MARK: groups, rails and the blank between them

    func testAPlaceIsAGroupWithARailInItsHueThatRunsThroughWhatHappenedThere() throws {
        let m = build(TimelineFixture.today(cal))
        let hue = RGB(255, 168, 150)
        let stay = try entry(m, "stay-1"), first = try entry(m, "reminder-demo-3"), last = try entry(m, "work-demo-1")
        XCTAssertEqual([stay.rail, first.rail, last.rail], [.place(hue), .place(hue), .place(hue)])
        XCTAssertEqual([stay.railIn, stay.railOut], [false, true], "from its dot down")
        XCTAssertEqual([first.railIn, first.railOut], [true, true])
        XCTAssertEqual([last.railIn, last.railOut], [true, false], "to its last row")
        XCTAssertEqual([stay.group, first.group, last.group], [nil, "stay-1", "stay-1"])
        // a place with nothing in it has no rail below its dot; one he does not keep has the neutral hue
        XCTAssertEqual(try entry(m, "stay-7").railOut, false)
        XCTAssertEqual(try entry(m, "stay-5").rail, .place(RGB(196, 204, 238)))
        // the blank: above a place, above the first leg of a run, not inside a group or a run
        XCTAssertEqual(m.entries.filter { $0.gap > 0 }.map(\.id), ["stay-1", "move-3", "stay-5", "move-6", "stay-7", "ride-8", "stay-9", "ride-12", "stay-13"])
        XCTAssertEqual(try entry(m, "move-4").gap, 0)
        XCTAssertEqual(try entry(m, "event-demo-1").gap, 0, "after the line for now there are no groups")
    }

    func testWhatHappenedWhileHeWasOnTheWayStandsOnTheTripsRailAndIsNotAPlacesRow() throws {
        let r = try reply([item("s1", "stay", 500, 600, home), item("r", "ride", 600, 660, #""route": "27""#), item("t", "talk", 620, 630, #""turns": 3"#),
                           item("s2", "stay", 660, 720, #""place": "School""#), item("p", "payment", 700, 700, #""amount": "3.00", "currency": "CAD", "direction": "out""#)].joined(separator: ","))
        let m = build(r, now: nil)
        XCTAssertEqual(m.entries.map(\.id), ["s1", "r", "t", "s2", "p"])
        let t = try entry(m, "t")
        XCTAssertTrue(t.onWay)
        XCTAssertNil(t.group)
        XCTAssertEqual(t.rail, .trip, "on the ride's rail, which ends with the ride")
        XCTAssertTrue(t.railIn)
        XCTAssertFalse(t.railOut)
        XCTAssertEqual(try entry(m, "r").railOut, true)
        XCTAssertEqual(t.gap, 0)
        XCTAssertEqual(try entry(m, "s2").gap, TLMetrics.gap)
        let p = try entry(m, "p")
        XCTAssertEqual(p.group, "s2", "a payment while he was in the school is the school's")
        XCTAssertFalse(p.onWay)
    }

    func testATripSaysWhereItBeganAndWhereItWentByThePlacesNextToIt() throws {
        let r = try reply([item("h", "stay", 500, 600, home), item("w", "move", 600, 610, walk), item("s", "stay", 610, 700, #""place": "School, Main St""#),
                           item("w2", "move", 700, 715, walk), item("r", "ride", 715, 740, #""route": "27""#), item("x", "stay", 740, 800),
                           item("w3", "move", 800, 812, walk)].joined(separator: ","))
        let m = build(r, now: nil)
        XCTAssertEqual(try entry(m, "w").more, ["家 → School"], "where it went: its length is already at the right")
        XCTAssertEqual(try entry(m, "w2").more, ["从 School 出发"], "the next leg of the run is where the bus is")
        XCTAssertEqual(try entry(m, "w3").more, [], "no name on the place before it, and no place after: nothing more to say")
        XCTAssertEqual(try entry(m, "r").more, [], "a ride says its own stops, and this one has none")
    }

    // MARK: who waits on him

    func testWhatWaitsOnHimIsAnUnconfirmedMailEventThatHasNotEndedAndAReminderThatRangAndIsNotAnswered() throws {
        let m = build(TimelineFixture.today(cal))
        XCTAssertEqual(m.waiting, ["reminder-demo-2", "event-demo-1"], "two, in the order of the day")
        for id in ["reminder-demo-2", "event-demo-1"] { XCTAssertTrue(try entry(m, id).waiting, id) }
        for id in ["reminder-demo-1", "reminder-demo-3", "event-demo-2", "payment-demo-1", "stay-13"] { XCTAssertFalse(try entry(m, id).waiting, id) }
        // what he has just answered stops waiting before the brain says so, and the row says so in its words
        let done = build(TimelineFixture.today(cal), acked: ["reminder-demo-2"])
        XCTAssertEqual(done.waiting, ["event-demo-1"])
        XCTAssertEqual(try entry(done, "reminder-demo-2").detail, "你知道了")
        XCTAssertEqual(try entry(done, "reminder-demo-2").actions, [.ask, .mark], "and offers nothing more to answer")
        // an event: from mail, not confirmed, and not over (an event with no end is over when it began)
        func waits(_ more: String, from: Double = 1320, to: Double? = 1350, now: Double? = TimelineFixture.now) throws -> Bool {
            let r = try reply(item("e", "event", from, to, #""title": "X", "# + more))
            return try entry(build(r, now: now), "e").waiting
        }
        XCTAssertTrue(try waits(#""source": "mail", "confirmed": false"#))
        XCTAssertTrue(try waits(#""source": "mail", "confirmed": false"#, to: nil), "no end, not begun")
        XCTAssertFalse(try waits(#""source": "mail", "confirmed": false"#, from: 1000, to: nil), "no end, begun: it is over when it began")
        XCTAssertFalse(try waits(#""source": "mail", "confirmed": false"#, from: 1200, to: 1250), "over")
        XCTAssertTrue(try waits(#""source": "mail", "confirmed": false"#, from: 1280, to: 1340), "on, and not over")
        XCTAssertFalse(try waits(#""source": "mail", "confirmed": true"#))
        XCTAssertFalse(try waits(#""source": "mail""#), "not said either way")
        XCTAssertFalse(try waits(#""source": "plan", "confirmed": false"#))
        XCTAssertFalse(try waits(#""source": "said", "confirmed": false"#))
        XCTAssertFalse(try waits(#""confirmed": false"#), "a calendar event")
        XCTAssertFalse(try waits(#""source": "mail", "confirmed": false"#, now: nil), "another day has no wait")
        // a reminder: rang and not acknowledged, said so by the brain
        func rings(_ more: String) throws -> Bool { try entry(build(try reply(item("r", "reminder", 600, nil, #""text": "X", "# + more)), now: nil), "r").waiting }
        XCTAssertTrue(try rings(#""fired": true, "acknowledged": false"#))
        XCTAssertFalse(try rings(#""fired": true, "acknowledged": true"#))
        XCTAssertFalse(try rings(#""fired": false, "acknowledged": false"#))
        XCTAssertFalse(try rings(#""fired": true"#), "the brain did not say he had not answered")
    }

    // MARK: what opening a row offers

    func testOpeningARowOffersOnlyWhatWorksToday() throws {
        let m = build(TimelineFixture.today(cal))
        func actions(_ id: String) throws -> [TLAction] { try entry(m, id).actions }
        XCTAssertEqual(try actions("talk-1"), [.openTalk, .ask, .mark])
        XCTAssertEqual(try actions("stay-1"), [.rename, .ask, .mark], "a place he keeps")
        XCTAssertEqual(try actions("stay-5"), [.name, .ask, .mark], "a place he does not keep is named, not renamed")
        XCTAssertEqual(try actions("reminder-demo-2"), [.acknowledge, .ask, .mark], "rang, and not answered")
        XCTAssertEqual(try actions("reminder-demo-1"), [.ask, .mark], "not yet")
        XCTAssertEqual(try actions("reminder-demo-3"), [.ask, .mark], "answered")
        XCTAssertEqual(try actions("work-demo-2"), [.summarize, .ask, .mark])
        XCTAssertEqual(try actions("event-demo-1"), [.ask, .mark])
        XCTAssertEqual(try actions("call-demo-1"), [.ask, .mark, .unmark], "she picked it: he can make it his or put it away")
        XCTAssertEqual(try actions("picture-pic-demo-1"), [.ask, .mark])
        XCTAssertEqual(try actions("sleep-0"), [.ask, .mark])
        XCTAssertEqual(try actions("move-3"), [.ask, .mark])
        // the host takes a ride, a bus, a payment and a chat with Claude as what a turn is about too
        for id in ["ride-8", "payment-demo-1", "payment-demo-2", "claude-demo-1", "departure-demo-1"] {
            XCTAssertEqual(try actions(id), [.ask, .mark], id)
        }
        // and does not take a run, but it can be starred; the row for several sessions and the line for now are not items the brain has
        XCTAssertEqual(try actions("workout-demo-1"), [.mark])
        for id in ["code-group-stay-1", "code-group-stay-13", "now"] {
            XCTAssertEqual(try actions(id), [], id)
        }
        // a place whose id could not stand in a path is not renamed, though he keeps it
        let odd = try reply(item("s", "stay", 600, 700, #""place": "X", "kept_place": {"id": "a/b", "name": "家", "role": "home"}"#))
        XCTAssertEqual(try entry(build(odd, now: nil), "s").actions, [.ask, .mark])
        let route = try reply(item("s", "stay", 600, 700, #""place": "X", "kept_place": {"id": "undo", "name": "家", "role": "home"}"#))
        XCTAssertEqual(try entry(build(route, now: nil), "s").actions, [.ask, .mark], "its id is the brain's own route")
        // an event from his mail has an @ in its id (`commitment-<hex>@<day>`), which the host takes; it is his to say it is his or is not
        let mail = try reply(item("commitment-ab12@2026-10-08", "event", 1320, 1350, #""title": "X", "source": "mail", "confirmed": false"#))
        XCTAssertEqual(try entry(build(mail), "commitment-ab12@2026-10-08").actions, [.confirm, .deny, .ask, .mark])
        XCTAssertEqual(try entry(build(mail), "commitment-ab12@2026-10-08").about?.id, "commitment-ab12@2026-10-08")
        XCTAssertTrue(try entry(build(mail), "commitment-ab12@2026-10-08").waiting, "and it waits on him")
    }

    func testARowOpensWhenThereIsSomethingToShowAndNeverOtherwise() throws {
        let m = build(TimelineFixture.today(cal))
        for id in ["stay-1", "move-3", "ride-8", "talk-1", "picture-pic-demo-1", "event-demo-1", "reminder-demo-1", "departure-demo-1", "work-demo-1", "call-demo-1", "payment-demo-1",
                   "sleep-0", "code-group-stay-1", "code-s1"] {
            if let e = m.entries.first(where: { $0.id == id }) { XCTAssertTrue(e.opens, id) }
        }
        XCTAssertFalse(try entry(m, "now").opens)
        // a workout says everything in its line, and still opens: to its star, which every item of the brain's day has
        XCTAssertTrue(try entry(m, "workout-demo-1").opens)
        XCTAssertTrue(try entry(m, "claude-demo-1").opens)
        let bare = try reply([item("s", "stay", 600, 700), item("pi", "picture", 700, 700, #""id": "x""#), item("c", "call", 710, 720), item("w", "work", 725, 740)].joined(separator: ","))
        let b = build(bare, now: nil)
        XCTAssertEqual(try entry(b, "s").actions, [.ask, .mark], "a place with no name, no map and nothing kept has only her to be handed to, and its star")
        let bareStay = try entry(b, "s")
        XCTAssertTrue(bareStay.opens)
        XCTAssertTrue(bareStay.more.isEmpty)
        XCTAssertNil(bareStay.place)
        XCTAssertTrue(try entry(b, "pi").opens, "a picture opens to the picture")
        XCTAssertTrue(try entry(b, "c").opens, "a call can be handed to her")
        XCTAssertTrue(try entry(b, "w").opens, "so can work")
    }

    // MARK: what is coming

    func testWhatIsStillToComeWithinTwoHoursSaysHowLongUntilIt() throws {
        let r = try reply([
            item("e1", "event", 1315, 1340, #""title": "A""#), item("e2", "event", 1340, 1360, #""title": "B""#), item("e3", "event", 1380, 1400, #""title": "C""#),
            item("e4", "event", 1430, 1439, #""title": "D""#), item("e5", "event", 1431, 1439, #""title": "E""#),
            item("rem", "reminder", 1340, nil, #""text": "R", "fired": false"#),
            item("dep", "departure", 1330, 1330, #""title": "12 · A", "leave_at_ms": \#(at(1320))"#),
        ].joined(separator: ","))
        let m = build(r)
        func right(_ id: String) throws -> String { try entry(m, id).right }
        XCTAssertEqual(try right("e1"), "还有 5 分")
        XCTAssertEqual(try right("e2"), "还有 30 分")
        XCTAssertEqual(try right("e3"), "还有 1 时 10 分")
        XCTAssertEqual(try right("e4"), "还有 2 时", "two hours on the dot")
        XCTAssertEqual(try right("e5"), "8 分", "a minute beyond: its own length")
        XCTAssertEqual(try right("rem"), "还有 30 分")
        XCTAssertEqual(try right("dep"), "22:00 出门", "a bus keeps its own words")
        XCTAssertEqual(m.entries.filter(\.soon).map(\.id), ["e1", "e2", "rem", "e3", "e4"], "within two hours, in the order of the day; the bus keeps its own words")
        XCTAssertTrue(try entry(m, "e5").later && !(try entry(m, "e5").soon))
        // it counts up to the next minute, never down to zero
        XCTAssertEqual(try entry(build(r, now: 1310.4), "e1").right, "还有 5 分", "4.6 minutes is 5")
        XCTAssertEqual(try entry(build(r, now: 1314.5), "e1").right, "还有 1 分", "half a minute is a minute")
        // what is over, and another day, say nothing of the kind
        XCTAssertFalse(try entry(build(r, now: nil), "e1").soon)
        XCTAssertEqual(try entry(build(r, now: nil), "e1").right, "25 分")
        XCTAssertFalse(try entry(build(r, now: 1400), "e1").later)
    }

    // MARK: what is handed to her

    func testAnAboutIsOnlyForAKindTheHostKnowsAndAnIdItWouldAccept() throws {
        let kinds = ["reminder", "event", "stay", "move", "sleep", "work", "call", "talk", "picture", "ride", "departure", "payment", "claude_chat", "claude_day"]
        let r = try reply(kinds.map { item("ok-\($0)", $0, 600, 610, #""text": "x", "title": "x""#) }.joined(separator: ","))
        XCTAssertEqual(Set(build(r, now: nil).entries.compactMap { $0.about?.kind }), Set(kinds))
        let no = ["workout", "code"]
        let r2 = try reply(no.map { item("no-\($0)", $0, 600, 610, #""text": "x", "title": "x""#) }.joined(separator: ","))
        let m2 = build(r2, now: nil)
        XCTAssertEqual(m2.entries.count, no.count, "every one of them is a row")
        XCTAssertTrue(m2.entries.allSatisfy { $0.about == nil })
        for id in ["a b", "é", String(repeating: "a", count: 257), "a#b", "a?b"] {
            XCTAssertNil(try entry(build(try reply(item(id, "talk", 600, 610)), now: nil), id).about, id)
        }
        for id in ["a-b_c.d:e=f+g/h|i", "commitment-ab12@2026-10-08", String(repeating: "a", count: 256)] {
            XCTAssertNotNil(try entry(build(try reply(item(id, "talk", 600, 610)), now: nil), id).about, id)
        }
    }

    // MARK: Claude Code

    func testSessionsOfClaudeCodeInOnePlaceAreOneRowThatOpensToThemAndOneAloneIsAnOrdinaryRow() throws {
        func code(_ n: Int, _ from: Double, _ to: Double, _ acct: String = "a", recap: String? = nil) -> String {
            item("code-s\(n)", "code", from, to, #""title": "T\#(n)", "project": "Jarvis", "account": "\#(acct)", "session_id": "s\#(n)"\#(recap.map { #", "detail": "\#($0)""# } ?? "")"#)
        }
        let r = try reply([item("s1", "stay", 600, 700, home), code(1, 610, 640, recap: "做完了"), code(2, 630, 660, "b"), code(3, 670, 690),
                           item("s2", "stay", 800, 900, #""place": "Lab""#), code(4, 810, 830), code(5, 950, 970)].joined(separator: ","))
        let m = build(r, now: nil)
        XCTAssertEqual(m.entries.map(\.id), ["s1", "code-group-s1", "s2", "code-s4", "code-s5"])
        let g = try entry(m, "code-group-s1")
        XCTAssertEqual(g.title, "Claude Code · 3 个会话")
        XCTAssertEqual(g.detail, "T3 · T2", "the two newest")
        XCTAssertEqual(g.right, "1 时 10 分", "the union of 610–660 and 670–690, not the sum of the three")
        XCTAssertEqual(g.sessions.map(\.title), ["T1", "T2", "T3"])
        XCTAssertEqual(g.sessions.map(\.account), ["A", "B", "A"])
        XCTAssertEqual(g.sessions.map(\.project), ["Jarvis", "Jarvis", "Jarvis"])
        XCTAssertEqual(g.sessions.map(\.recap), ["做完了", nil, nil])
        XCTAssertEqual(g.sessions.map(\.right), ["30 分", "30 分", "20 分"])
        XCTAssertEqual(g.sessions.map(\.time), ["10:10", "10:30", "11:10"])
        XCTAssertTrue(g.holdsSessions && g.opens)
        XCTAssertEqual(g.lens, .work)
        XCTAssertEqual(g.group, "s1")
        XCTAssertEqual(g.symbol, "terminal.fill")
        XCTAssertEqual(m.marks(of: .work), [TLMark(t0: 610, t1: 640), TLMark(t0: 630, t1: 660), TLMark(t0: 670, t1: 690), TLMark(t0: 810, t1: 830), TLMark(t0: 950, t1: 970)])
        let one = try entry(m, "code-s4"), alone = try entry(m, "code-s5")
        XCTAssertEqual([one.title, one.detail, one.right], ["T4", "Jarvis · A", "20 分"])
        XCTAssertFalse(one.holdsSessions)
        XCTAssertEqual([one.group, alone.group], ["s2", nil], "one in a place is that place's row; one in no place stands alone")
        XCTAssertEqual(try entry(m, "code-s4").sessions.count, 1)
        // they are lit with the Mac's work, and are not added to it
        XCTAssertTrue(m.chips.isEmpty)
        // a finger on the bar tells each of them
        XCTAssertEqual(m.readout(at: 635).elsewhere, ["Claude Code · T1", "Claude Code · T2"])
        XCTAssertEqual(m.readout(at: 698).elsewhere, ["Claude Code · T3"], "eight minutes after it ended: within ten")
        XCTAssertEqual(m.readout(at: 705).elsewhere, [], "fifteen: not")
        // a recap opens a single session to its words
        XCTAssertEqual(try entry(build(try reply(code(9, 610, 620, recap: "R")), now: nil), "code-s9").more, ["R"])
    }

    func testASessionWithNoTitleIsClaudeCodeAloneInItsRowItsGlanceAndTheReadoutAndAGroupDoesNotNameItsLineAfterIt() throws {
        // the title the brain leaves out, leaves empty or fills with blanks: a session nobody named (the project and the account are still said)
        func code(_ n: Int, _ from: Double, _ to: Double, _ title: String?) -> String {
            item("code-s\(n)", "code", from, to, #""project": "Jarvis", "account": "b", "session_id": "s\#(n)""# + (title.map { #", "title": "\#($0)""# } ?? ""))
        }
        // alone
        let alone = build(try reply([code(1, 610, 640, ""), code(2, 700, 730, nil), code(3, 800, 830, "  "), code(4, 900, 930, "T4")].joined(separator: ",")), now: nil)
        for id in ["code-s1", "code-s2", "code-s3"] {
            let e = try entry(alone, id)
            XCTAssertEqual([e.title, e.glance, e.detail], ["Claude Code", "Claude Code", "Jarvis · B"], id)
            XCTAssertEqual(e.sessions.map(\.name), ["Claude Code"], id)
            XCTAssertEqual(e.sessions.map(\.title), [nil], id)
        }
        XCTAssertEqual([try entry(alone, "code-s4").title, try entry(alone, "code-s4").glance], ["T4", "Claude Code · T4"], "a title still goes after it")
        XCTAssertEqual(alone.readout(at: 620).elsewhere, ["Claude Code"])
        XCTAssertEqual(alone.readout(at: 910).elsewhere, ["Claude Code · T4"])
        // in a group: each line of it, and the readout, say the same of one with no title; the group's own line names the two newest that have one
        let r = try reply([item("s1", "stay", 600, 700, home), code(1, 610, 640, "T1"), code(2, 630, 660, ""), code(3, 670, 690, nil), code(4, 675, 695, "T4")].joined(separator: ","))
        let m = build(r, now: nil)
        let g = try entry(m, "code-group-s1")
        XCTAssertEqual(g.title, "Claude Code · 4 个会话")
        XCTAssertEqual(g.detail, "T4 · T1", "the empty and the missing are skipped, not counted among the two")
        XCTAssertEqual(g.sessions.map(\.title), ["T1", nil, nil, "T4"])
        XCTAssertEqual(g.sessions.map(\.name), ["T1", "Claude Code", "Claude Code", "T4"], "an opened one with no title shows Claude Code")
        XCTAssertEqual(g.sessions.map(\.project), ["Jarvis", "Jarvis", "Jarvis", "Jarvis"])
        XCTAssertEqual(m.readout(at: 635).elsewhere, ["Claude Code · T1", "Claude Code"])
        XCTAssertEqual(m.readout(at: 680).elsewhere, ["Claude Code", "Claude Code · T4"])
        // none of them has a title: the group says its count and no line under it
        let bare = try entry(build(try reply([item("s1", "stay", 600, 700, home), code(1, 610, 640, nil), code(2, 630, 660, "")].joined(separator: ",")), now: nil), "code-group-s1")
        XCTAssertEqual(bare.title, "Claude Code · 2 个会话")
        XCTAssertNil(bare.detail)
    }

    func testASessionThatMatteredStaysARowOfItsOwnWithItsStarWhileTheOthersAreOneRow() throws {
        func code(_ n: Int, _ from: Double, _ to: Double) -> String {
            item("code-s\(n)", "code", from, to, #""title": "T\#(n)", "project": "Jarvis", "account": "a", "session_id": "s\#(n)""#)
        }
        let rows = [item("s1", "stay", 600, 700, home), code(1, 610, 640), code(2, 630, 660), code(3, 670, 690), code(4, 680, 695)].joined(separator: ",")
        let m = build(try reply(rows, #", "highlights": [{"id": "code-s2", "label": "Fixed the bug", "by": "him"}]"#), now: nil)
        XCTAssertEqual(m.entries.map(\.id), ["s1", "code-group-s1", "code-s2"])
        XCTAssertEqual(try entry(m, "code-group-s1").sessions.map(\.title), ["T1", "T3", "T4"], "the marked one is not folded into the group")
        let marked = try entry(m, "code-s2")
        XCTAssertTrue(marked.lit)
        XCTAssertEqual(marked.title, "Fixed the bug")
        XCTAssertFalse(marked.holdsSessions)
        XCTAssertEqual(marked.group, "s1", "still in its place")
        XCTAssertEqual(m.stars, [TLStar(id: "code-s2", t: 630)], "and its star is on the bar")
        // two sessions where one mattered: neither is folded; with none marked they are one row
        let two = [item("s1", "stay", 600, 700, home), code(1, 610, 640), code(2, 630, 660)].joined(separator: ",")
        XCTAssertEqual(build(try reply(two, #", "highlights": [{"id": "code-s1", "label": "", "by": "her"}]"#), now: nil).entries.map(\.id), ["s1", "code-s1", "code-s2"])
        XCTAssertEqual(build(try reply(two), now: nil).entries.map(\.id), ["s1", "code-group-s1"])
    }

    // MARK: the new data

    func testTheWeatherIsWordedAndAnUnknownNameIsLeftUnsaidWithTheTemperatures() throws {
        // the Mac's table: a name is read without case or separators, and a condition it has no word for (`frigid`, `hot`) leaves only the temperatures
        let words: [(String?, String?)] = [("mostly cloudy", "多云"), ("partly cloudy", "多云"), ("cloudy", "阴"), ("mostly clear", "少云"), ("clear", "晴"), ("rain", "雨"),
                                           ("heavy rain", "雨"), ("drizzle", "小雨"), ("sunShowers", "小雨"), ("snow", "雪"), ("flurries", "雪"), ("hail", "雪"), ("foggy", "雾"), ("haze", "雾"),
                                           ("blowing_dust", "雾"), ("windy", "风"), ("thunderstorms", "雷雨"), ("strong storms", "雷雨"), ("hurricane", "雷雨"), ("Tropical Storm", "雷雨"),
                                           ("hot", nil), ("frigid", nil), ("Sunny", nil), ("showers", nil), ("mystery weather", nil), ("", nil), (nil, nil)]
        for (c, w) in words { XCTAssertEqual(DayTimelineModel.weatherWords(c)?.word, w, c ?? "nil") }
        func weather(_ json: String) throws -> TLWeather? { build(try reply("", ", \"weather\": " + json)).weather }
        XCTAssertEqual(try weather(#"{"condition": "mostly cloudy", "high": 14.4, "low": 8, "sunrise_ms": 1, "sunset_ms": 2}"#)?.text, "多云 14° / 8°")
        XCTAssertEqual(try weather(#"{"condition": "mostly cloudy", "high": 14.4, "low": 8}"#)?.symbol, "cloud.fill")
        XCTAssertEqual(try weather(#"{"condition": "frigid", "high": 14, "low": 8}"#)?.text, "14° / 8°", "no word for it: only the temperatures")
        XCTAssertEqual(try weather(#"{"condition": "clear", "high": 21}"#)?.text, "晴 21°")
        XCTAssertEqual(try weather(#"{"condition": "clear"}"#)?.text, "晴")
        XCTAssertEqual(try weather(#"{"high": -3.6, "low": -9}"#)?.text, "-4° / -9°")
        XCTAssertNil(try weather(#"{"condition": "weird"}"#), "nothing at all to say")
        XCTAssertNil(try weather("null"))
        XCTAssertNil(try weather(#"[1, 2]"#), "not the shape")
        XCTAssertNil(try weather(#"{"condition": 5, "high": "hot", "low": null}"#))
        XCTAssertNil(build(try reply("")).weather, "a brain that serves none")
    }

    func testTheSunRisesAndSetsOnTheBarWhereThoseTimesAreOnThisDay() throws {
        let m = build(TimelineFixture.today(cal))
        XCTAssertEqual(m.sun, TLSun(rise: 454, set: 1091))
        let y = DayTimelineModel.build(TimelineFixture.yesterday(cal), now: nil)
        XCTAssertEqual(y.sun, TLSun(rise: 453, set: 1092))
        XCTAssertNil(build(try reply("", #", "weather": {"condition": "clear", "high": 1, "low": 0}"#)).sun)
        let far = try reply("", ", \"weather\": {\"sunrise_ms\": \(at(-500)), \"sunset_ms\": \(at(1500))}")
        XCTAssertNil(build(far).sun, "both are on other days")
        XCTAssertEqual(build(try reply("", ", \"weather\": {\"sunrise_ms\": \(at(400)), \"sunset_ms\": 5}")).sun, TLSun(rise: 400, set: nil))
    }

    func testTheBodyValuesAreQuietNumbersAfterTheChipsAndANullIsLeftOut() throws {
        func body(_ json: String) throws -> [String] { build(try reply("", ", \"summary\": " + json)).body.map { $0.prefix + $0.value + $0.suffix } }
        XCTAssertEqual(try body(#"{"steps": 8240, "resting_hr": 58, "hrv": 42}"#), ["8,240 步", "静息 58", "HRV 42"])
        XCTAssertEqual(try body(#"{"steps": 8240.6, "resting_hr": null, "hrv": 42.4}"#), ["8,241 步", "HRV 42"])
        XCTAssertEqual(try body(#"{"steps": 0}"#), ["0 步"], "a day of no steps is told")
        XCTAssertEqual(try body(#"{"steps": null, "resting_hr": null, "hrv": null}"#), [])
        XCTAssertEqual(try body("null"), [])
        XCTAssertEqual(try body(#"{"steps": "many", "resting_hr": -5, "hrv": 0}"#), [], "not numbers, or not values")
        XCTAssertEqual(try body("5"), [])
        XCTAssertEqual(build(try reply("")).body, [])
    }

    func testTheNightIsTotaledByItsStagesAndOnlyTheStretchInsideItIsCounted() throws {
        let m = build(TimelineFixture.today(cal))
        let sleep = try XCTUnwrap(try entry(m, "sleep-0").sleep)
        XCTAssertEqual([sleep.deep, sleep.core, sleep.rem, sleep.awake], [115, 232, 120, 2])
        XCTAssertEqual(sleep.summary, "深睡 1:55 · 核心 3:52 · REM 2:00 · 醒 2 次")
        XCTAssertEqual(sleep.short, "深睡 1:55 · REM 2:00")
        XCTAssertEqual(try entry(m, "sleep-0").glance, "深睡 1:55 · REM 2:00")
        XCTAssertEqual(sleep.stages.count, 12)
        XCTAssertEqual(sleep.stages.first, TLStage(kind: .core, t0: 55, t1: 100), "the night's own stretch: the time in bed before it is cut")
        XCTAssertEqual(sleep.stages.last, TLStage(kind: .awake, t0: 530, t1: 545))
        // a stage that runs past the night's two ends counts only inside it; a stage this phone does not know is not drawn
        let r = try reply(item("n", "sleep", 100, 200, #""stages": [{"stage": "deep", "start_ms": \#(at(60)), "end_ms": \#(at(130))}, {"stage": "rem", "start_ms": \#(at(190)), "end_ms": \#(at(260))}, {"stage": "levitating", "start_ms": \#(at(130)), "end_ms": \#(at(140))}, {"stage": "awake", "start_ms": \#(at(250)), "end_ms": \#(at(260))}]"#))
        let n = try XCTUnwrap(try entry(build(r, now: nil), "n").sleep)
        XCTAssertEqual([n.deep, n.rem, n.awake], [30, 10, 0])
        XCTAssertEqual(n.summary, "深睡 30 分 · REM 10 分")
        XCTAssertEqual(n.stages, [TLStage(kind: .deep, t0: 100, t1: 130), TLStage(kind: .unspecified, t0: 130, t1: 190), TLStage(kind: .rem, t0: 190, t1: 200)], "cut to the night, and what no stage covers said so")
        // no stages: no hypnogram and nothing to add to the readout
        let none = try entry(build(try reply(item("n", "sleep", 100, 200)), now: nil), "n")
        XCTAssertNil(none.sleep)
        XCTAssertNil(none.glance)
        XCTAssertEqual(TLStage.Kind(name: "in_bed"), .inBed)
        XCTAssertNil(TLStage.Kind(name: "asleep"))
    }

    func testAWorkoutIsNamedForWhatItWasAndSaysOnlyWhatIsThere() throws {
        let m = build(TimelineFixture.today(cal))
        let w = try entry(m, "workout-demo-1")
        XCTAssertEqual([w.title, w.symbol, w.detail, w.right, w.time], ["跑步", "figure.run", "3.2 公里 · 240 千卡 · 平均心率 142", "35 分", "17:00"])
        XCTAssertEqual(w.glance, "跑步 · 3.2 公里")
        XCTAssertEqual(w.lens, .workout)
        XCTAssertEqual(w.group, "stay-9", "the school's, as he was there")
        XCTAssertEqual(w.more, [], "its facts are its line")
        let names: [(String, String)] = [("running", "跑步"), ("Outdoor Run", "跑步"), ("walking", "步行"), ("cycling", "骑车"), ("biking", "骑车"), ("hiking", "徒步"), ("swimming", "游泳"),
                                         ("functional_strength_training", "力量训练"), ("yoga", "瑜伽"), ("rowing", "运动"), ("", "运动")]
        for (activity, title) in names { XCTAssertEqual(DayTimelineModel.workoutWords(activity).0, title, activity) }
        let part = try reply(item("a", "workout", 600, 640, #""activity": "walking", "distance_m": 800"#) + "," + item("b", "workout", 700, 705, #""activity": "yoga", "energy_kcal": 12.4, "avg_hr": 0"#) + "," + item("c", "workout", 800, 801))
        let rows = build(part, now: nil).entries
        XCTAssertEqual(rows.map(\.detail), ["800 米", "12 千卡", nil])
        XCTAssertEqual(rows.map(\.title), ["步行", "瑜伽", "运动"])
    }

    func testWhatARideSaysWhenItIsOpened() throws {
        let r = try reply(item("a", "ride", 600, 630, #""route": "27 → 14", "board_stop": "A", "alight_stop": "B", "credit": "BC Transit", "ended_reason": "said""#) + "," +
                          item("b", "ride", 700, 730, #""route": "7", "ended_reason": "something new""#) + "," + item("c", "ride", 800, 830, #""route": "9", "credit": " ", "ended_reason": "replaced""#))
        let m = build(r, now: nil)
        XCTAssertEqual(try entry(m, "a").title, "27→14 路", "the arrow without its spaces, as the Mac writes it")
        XCTAssertEqual(try entry(m, "a").more, ["实时：BC Transit", "你说了下车"], "both stops are in its line")
        XCTAssertEqual(try entry(m, "b").more, [], "an ending it has no words for is not guessed at")
        XCTAssertEqual(try entry(m, "c").more, ["换了另一趟车"], "a credit of blanks is none")
    }

    func testARowWhoseWordsAreCutShortOpensToSayThemWholeAndOneWhoseWordsFitDoesNot() throws {
        func chat(_ text: String, _ more: String = #""messages": 14"#) throws -> TLEntry {
            try entry(build(try reply(item("c", "claude_chat", 600, 640, #""text": "\#(text)", "# + more)), now: nil), "c")
        }
        let fits = try chat("积分换元的符号问题")
        XCTAssertFalse(fits.cutShort)
        XCTAssertEqual(fits.more, [], "it has nothing to say that its line does not")
        XCTAssertEqual(fits.actions, [.ask, .mark], "it opens only to be handed to her and to be starred")
        let cut = try chat(String(repeating: "积分换元的符号问题", count: 3))
        XCTAssertTrue(cut.cutShort)
        XCTAssertTrue(cut.opens, "it opens to say its title whole")
        XCTAssertEqual(cut.more, [], "and has nothing to add to it")
        // the amber 等你 takes room from the title: what fits alone is cut beside it
        let edge = String(repeating: "字", count: 17)                       // 255 pt at 15: under the 288 a reminder has, over what is left beside the badge
        func reminder(_ more: String) throws -> TLEntry { try entry(build(try reply(item("r", "reminder", 600, nil, #""text": "\#(edge)", "fired": true, "# + more)), now: nil), "r") }
        let known = try reminder(#""acknowledged": true"#), wait = try reminder(#""acknowledged": false"#)
        XCTAssertFalse(known.waiting || known.cutShort)
        XCTAssertTrue(wait.waiting && wait.cutShort, "beside the badge it no longer fits")
        // a place, a pause and the line for now are never cut short
        let m = build(TimelineFixture.today(cal))
        for e in m.entries where e.kind == .stay || e.kind == .now || e.kind == .pause { XCTAssertFalse(e.cutShort, e.id) }
        // the words are measured once, when the day is built: a row's flag is what measuring it now says, and a row that opens for nothing else opens by it
        for e in m.entries { XCTAssertEqual(e.cut, e.cutShort, e.id) }
        XCTAssertTrue(cut.cut && !fits.cut && wait.cut && !known.cut)
        // the measure: ten wide characters at 15 pt are 150
        XCTAssertEqual(tlWidth(String(repeating: "字", count: 10), size: 15), 150, accuracy: 1.5)
        XCTAssertGreaterThan(tlWidth("Hillside Centre", size: 12), 60)
    }

    // MARK: the words of the other rows

    func testEventsSayWhereTheyCameFromAndPrivateOnesSayNoMoreThanThat() throws {
        let r = try reply([item("e1", "event", 600, 660, #""title": "A", "place": "Room 1""#), item("e2", "event", 700, 760, #""title": "B", "source": "mail", "confirmed": true"#),
                           item("e3", "event", 800, 860, #""title": "C", "source": "said""#), item("e4", "event", 900, 960, #""title": "D", "source": "plan""#),
                           item("e5", "event", 1000, 1060, #""title": "E", "source": "elsewhere""#), item("e6", "event", 1100, 1160, #""title": "F", "place": "Clinic", "private": true, "source": "mail", "confirmed": false"#),
                           item("e7", "event", 1200, 1260, #""title": "G", "place": "Clinic", "private": true, "source": "mail", "confirmed": true"#)].joined(separator: ","))
        let m = build(r, now: nil)
        XCTAssertEqual(m.entries.map(\.detail), ["日历 · Room 1", "邮件 · 你确认了", "你说的", "计划", nil, nil, nil], "that he has said it is his is in the line")
        // opened, the hours (the row has when it began); where it came from and whether it is his are in its line, a private one's has none, so it says them here
        XCTAssertEqual(m.entries.map(\.more), [["10:00–11:00"], ["11:40–12:40"], ["13:20–14:20"], ["15:00–16:00"], ["16:40–17:40"], ["18:20–19:20", "来自邮件"], ["20:00–21:00", "来自邮件", "你确认了"]])
        XCTAssertEqual(try entry(m, "e6").title, "私事")
    }

    func testRemindersAndDeparturesAndCallsAndWorkSayTheirWordsInFull() throws {
        let r = try reply([item("r1", "reminder", 1400, nil, #""text": "给 Mia 回电话", "fired": false"#), item("r2", "reminder", 600, nil, #""text": "吃药", "fired": true, "acknowledged": false"#),
                           item("d1", "departure", 1400, 1400, #""title": "12 · Hillside", "route": "12", "board_stop": "Hillside", "to": "UVic", "leave_at_ms": \#(at(1390))"#),
                           item("c1", "call", 600, 618, #""app": "FaceTime""#), item("w1", "work", 650, 780, #""active_s": 7800, "projects": [{"name": "Jarvis", "seconds": 6000}, {"name": "论文", "seconds": 1800}], "apps": [{"name": "Xcode", "seconds": 4000}]"#)].joined(separator: ","))
        let m = build(r)
        XCTAssertEqual(try entry(m, "r1").more, [], "a reminder's text and state are its title and line")
        XCTAssertEqual(try entry(m, "r2").more, [])
        XCTAssertEqual(try entry(m, "d1").more, ["路线 12", "在 Hillside 上车"], "where to and when to leave are its line and its value")
        XCTAssertEqual(try entry(m, "c1").title, "通话 · FaceTime")
        XCTAssertEqual(try entry(m, "c1").more, [], "the app is its title and the length its value")
        let w = try entry(m, "w1")
        XCTAssertEqual(w.work?.projects, [TLTop(name: "Jarvis", minutes: 100, share: 1), TLTop(name: "论文", minutes: 30, share: 0.3)])
        XCTAssertEqual(w.work?.apps, [TLTop(name: "Xcode", minutes: 67, share: 1)])
        XCTAssertEqual([w.work?.active, w.work?.total], [130, 130])
        XCTAssertEqual(w.more, ["活跃 2 时 10 分 · 共 2 时 10 分"])
    }

    func testWorkListsTheFiveBiggestOfEachAndTheStayIsOnTheMapOnlyWhereTheCoordinatesAre() throws {
        let many = (1...8).map { #"{"name": "P\#($0)", "seconds": \#($0 * 600)}"# }.joined(separator: ",")
        let r = try reply(item("w", "work", 600, 700, #""projects": [\#(many)]"#) + "," +
                          item("s1", "stay", 720, 800, #""place": "A", "lat": 48.46, "lng": -123.31"#) + "," + item("s2", "stay", 810, 820, #""place": "B", "lat": 123, "lng": 0"#) + "," +
                          item("s3", "stay", 830, 840, #""place": "C", "lat": 48.4"#) + "," + item("s4", "stay", 850, 860, #""kept_place": {"id": "p-x", "name": "X", "role": "other"}"#))
        let m = build(r, now: nil)
        XCTAssertEqual(try entry(m, "w").work?.projects.map(\.name), ["P8", "P7", "P6", "P5", "P4"])
        XCTAssertEqual(try entry(m, "w").work?.projects.first?.share, 1)
        XCTAssertEqual(try entry(m, "s1").place?.onMap, true)
        XCTAssertEqual(try entry(m, "s2").place?.onMap, false, "a latitude off the globe is no place on it")
        XCTAssertEqual(try entry(m, "s2").place?.text, "B")
        XCTAssertEqual(try entry(m, "s3").place?.onMap, false, "half a point is no point")
        XCTAssertEqual(try entry(m, "s4").place?.keptId, "p-x")
        XCTAssertEqual(try entry(m, "s4").place?.onMap, false)
    }

    func testAKeptPlaceRenamedJustNowWearsTheNewNameBeforeTheBrainSaysSo() throws {
        let m = build(TimelineFixture.today(cal), renamed: ["p-home": "我的家"])
        let home = try entry(m, "stay-1")
        XCTAssertEqual(home.title, "我的家")
        XCTAssertEqual(home.more, ["12 Example St, Town", "常去的地方 · 我的家"])
        XCTAssertEqual(home.place?.keptName, "我的家")
        XCTAssertEqual(home.head, "我的家 · 09:05–11:25 · 2 时 20 分")
        XCTAssertEqual(home.barName, "我的家")
        XCTAssertEqual(try entry(m, "stay-13").title, "我的家", "every stay in that place")
        XCTAssertEqual(try entry(m, "stay-9").title, "UVic", "and no other")
        XCTAssertEqual(build(TimelineFixture.today(cal), renamed: ["p-home": "  "]).entries.first { $0.id == "stay-1" }?.title, "家", "a name of blanks is none")
    }

    // MARK: an item the phone cannot make sense of

    func testAnItemThePhoneCannotMakeSenseOfCostsThatItemAndNothingElse() throws {
        let broken = [
            #"{"id": "no-start", "lane": "you", "kind": "stay", "state": "done", "end_ms": 1}"#,
            #"{"id": "text-start", "lane": "you", "kind": "stay", "state": "done", "start_ms": "soon", "end_ms": 1}"#,
            #"{"id": "no-kind", "lane": "you", "state": "done", "start_ms": \#(at(600))}"#,
            #"{"id": "text-end", "lane": "you", "kind": "stay", "state": "done", "start_ms": \#(at(600)), "end_ms": "later"}"#,
            #"{"id": "", "lane": "you", "kind": "stay", "state": "done", "start_ms": \#(at(600)), "end_ms": \#(at(610))}"#,
            item("backwards", "talk", 700, 650, #""turns": 3"#),
            #"{"id": "far", "lane": "you", "kind": "stay", "state": "done", "start_ms": 9007199254740991, "end_ms": null}"#,
            #"{"id": "far-end", "lane": "you", "kind": "stay", "state": "done", "start_ms": \#(at(600)), "end_ms": 9007199254740991}"#,
            #"{"id": "min", "lane": "you", "kind": "stay", "state": "done", "start_ms": -9223372036854775808, "end_ms": null}"#,
            item("twice", "stay", 800, 810), item("twice", "talk", 820, 830),
            item("now", "talk", 840, 850),
            item("flight", "flight", 600, 700),
            #"[1, 2]"#, #""text""#, "null", "7",
        ]
        let odd = [
            item("odd-stay", "stay", 720, 740, #""place": 5, "kept_place": 7, "lat": "north", "lng": [1], "accuracy_m": {}"#),
            item("odd-work", "work", 745, 790, #""projects": "oops", "apps": [{"name": 5}, {"name": "Xcode", "seconds": "long"}, 3], "active_s": "lots""#),
            item("odd-sleep", "sleep", 30, 120, #""minutes": "long", "stages": [{"stage": 5}, {"stage": "rem", "start_ms": 1, "end_ms": 0}, {"stage": "deep", "start_ms": \#(at(40)), "end_ms": \#(at(60))}, "x"]"#),
            item("odd-pay", "payment", 800, 800, #""amount": {"x": 1}, "currency": 5, "direction": ["in"], "counterparty": 7"#),
            item("odd-talk", "talk", 810, 820, #""turns": "many""#),
            item("odd-rem", "reminder", 830, nil, #""fired": "yes", "acknowledged": 1, "text": 9"#),
            item("odd-workout", "workout", 840, 870, #""distance_m": "far", "energy_kcal": null, "avg_hr": [140]"#),
            item("odd-code", "code", 880, 900, #""title": 5, "account": 1, "project": [1]"#),
            item("odd-ride", "ride", 910, 920, #""route": 27, "board_stop": ["A"], "credit": 1, "ended_reason": {}"#),
            item("odd-picture", "picture", 930, 930, #""note": 5, "attachment": 7"#),
            item("odd-event", "event", 940, 950, #""title": 5, "source": 5, "confirmed": "no", "private": "maybe""#),
            item("odd-dep", "departure", 960, 960, #""leave_at_ms": "soon", "to": 7"#),
            item("odd-call", "call", 970, 980, #""app": 5"#),
            item("odd-claude", "claude_chat", 990, 1000, #""messages": "14", "text": 5"#),
            item("odd-day", "claude_day", 1010, 1020, #""chats": "three", "text": ["a"]"#),
            item("odd-huge", "work", 1030, 1040, #""active_s": 1e300, "projects": [{"name": "X", "seconds": 1e300}]"#),
            item("odd-money", "payment", 1050, 1050, #""amount": 1e300, "currency": "CAD", "direction": "out""#),
            item("odd-lat", "stay", 1060, 1070, #""lat": 1e300, "lng": -1e300, "place": "Far""#),
        ]
        let good = item("good", "stay", 600, 700, #""place": "Home""#)
        let extras = #", "weather": {"condition": 5, "high": "hot", "low": null, "sunrise_ms": "x"}, "summary": {"steps": "many", "hrv": null}, "highlights": [5, {"id": 1}, {"id": "odd-talk", "label": 7}, {"id": "good", "label": "ok"}], "labels": "x", "line": {"text": 5}"#
        let r = try reply((broken + odd + [good]).joined(separator: ","), extras)
        for now in [nil, TimelineFixture.now] as [Double?] {
            let m = build(r, now: now)
            let ids = m.entries.filter { $0.kind != .now }.map(\.id).sorted()
            XCTAssertEqual(ids, (["good", "twice"] + ["odd-stay", "odd-work", "odd-sleep", "odd-pay", "odd-talk", "odd-rem", "odd-workout", "odd-code", "odd-ride", "odd-picture", "odd-event",
                                                        "odd-dep", "odd-call", "odd-claude", "odd-day", "odd-huge", "odd-money", "odd-lat"]).sorted(),
                           "the first of the two with one id stays, and every item that could be read is a row")
            XCTAssertNil(m.weather)
            XCTAssertEqual(m.body, [])
            XCTAssertNil(m.line)
            XCTAssertEqual(m.stars.map(\.id), ["good"], "a mark on an item that is there")
            // none of them is a trap for what reads the model
            for t in stride(from: -50.0, to: 1500.0, by: 7.0) + [.nan, .infinity, -.infinity, 1e300] {
                _ = m.readout(at: t); _ = m.lit(at: t); _ = m.entry(at: t); _ = m.stretch(at: t)
            }
            _ = m.scene; _ = m.offsets; _ = m.restY(room: 564); _ = m.height; _ = m.marks(of: .work)
        }
        let m = build(r, now: nil)
        func e(_ id: String) throws -> TLEntry { try entry(m, id) }
        XCTAssertEqual([try e("odd-stay").title, try e("odd-stay").place == nil ? "none" : "some"], ["没名字的地方", "none"])
        XCTAssertEqual(try e("odd-stay").actions, [.ask, .mark], "nothing to show of it: only her, and its star")
        XCTAssertEqual([try e("odd-work").title, try e("odd-work").detail], ["在 Mac 上工作", "Xcode"])
        XCTAssertEqual(try e("odd-work").work?.apps, [TLTop(name: "Xcode", minutes: 1, share: 0)])
        XCTAssertEqual(try e("odd-sleep").right, "1 时 30 分", "no minutes: the span")
        XCTAssertEqual(try e("odd-sleep").sleep?.summary, "深睡 20 分", "of the stages, the one that was one")
        XCTAssertEqual([try e("odd-pay").title, try e("odd-pay").right], ["付款", ""])
        XCTAssertNil(try e("odd-talk").detail)
        XCTAssertEqual(try e("odd-rem").title, "提醒")
        XCTAssertFalse(try e("odd-rem").waiting)
        XCTAssertEqual([try e("odd-workout").title, try e("odd-workout").detail], ["运动", nil])
        XCTAssertEqual([try e("odd-code").title, try e("odd-code").detail], ["Claude Code", nil])
        XCTAssertEqual([try e("odd-ride").title, try e("odd-ride").detail], ["公交", nil])
        XCTAssertEqual([try e("odd-picture").detail, try e("odd-picture").attachment], [nil, "odd-picture"])
        XCTAssertEqual([try e("odd-event").title, try e("odd-event").detail], ["日程", "日历"])
        XCTAssertFalse(try e("odd-event").waiting)
        XCTAssertEqual([try e("odd-dep").title, try e("odd-dep").right], ["公交", ""])
        XCTAssertEqual(try e("odd-call").title, "通话")
        XCTAssertEqual([try e("odd-claude").title, try e("odd-claude").right], ["Claude 对话", "10 分"])
        XCTAssertEqual([try e("odd-day").title, try e("odd-day").detail], ["Claude 日报", nil])
        XCTAssertEqual(try e("odd-money").right, "", "an amount too big to be an amount is none")
        XCTAssertEqual(try e("odd-lat").place?.onMap, false)
        XCTAssertEqual(try e("twice").kind, .stay)
        XCTAssertEqual(try e("odd-huge").right, "活跃 1 时 40 分" == "" ? "" : try e("odd-huge").right)
    }

    func testATimeThatIsNotATimeReadsAsMidnightAndANumberThatIsNotANumberIsZero() {
        XCTAssertEqual(clock(.nan), "00:00")
        XCTAssertEqual(clock(.infinity), "00:00")
        XCTAssertEqual(clock(-.infinity), "00:00")
        for huge in [1e300, -1e300, 1e18, -1e18] { XCTAssertNotNil(clock(huge).range(of: #"^\d\d:\d\d$"#, options: .regularExpression), "\(huge)") }
        XCTAssertEqual(clock(570), "09:30")
        XCTAssertEqual(clock(-30), "23:30")
        XCTAssertEqual(clock(1440), "00:00")
        XCTAssertEqual([tlEnd(1440), tlEnd(1439), tlEnd(1560), tlEnd(0)], ["24:00", "23:59", "次日 02:00", "00:00"])
        XCTAssertEqual([tlInt(.nan), tlInt(.infinity), tlInt(1e300), tlInt(-1e300), tlInt(2.5), tlInt(-2.5), tlInt(3.4)], [0, 0, 1_000_000_000, -1_000_000_000, 3, -3, 3])
        XCTAssertEqual(tlDur(-5), "0 分")
        XCTAssertEqual(tlHM(-5), "0 分")
        XCTAssertEqual(tlGroup(Int.min), "-9,223,372,036,854,775,808")
        XCTAssertEqual(tlMoney(Int.min, "CAD"), "-$92,233,720,368,547,758.08")
    }

    // MARK: what arrives

    func testTheNewFieldsAreReadAndAPartOfAnotherShapeCostsOnlyItself() throws {
        let r = try reply([
            item("n", "sleep", 30, 120, #""minutes": 85, "stages": [{"stage": "core", "start_ms": \#(at(30)), "end_ms": \#(at(90))}, {"stage": 5}, 7]"#),
            item("w", "workout", 600, 640, #""activity": "running", "text": "Outdoor Run", "energy_kcal": 240.5, "distance_m": 3200, "avg_hr": 142"#),
            item("c", "code", 650, 700, #""title": "T", "project": "Jarvis", "account": "a", "session_id": "s1", "detail": "recap""#),
            item("r", "ride", 710, 730, #""route": "27", "credit": "BC Transit", "ended_reason": "left_vehicle""#),
            item("s", "stay", 740, 760, #""lat": 48.46, "lng": -123.31, "accuracy_m": 12.5, "place": "X""#),
            item("bad", "workout", 770, 780, #""energy_kcal": "lots", "distance_m": [1], "stages": "no""#),
        ].joined(separator: ","), #", "weather": {"condition": "clear", "high": 14.2, "low": 8, "sunrise_ms": 1, "sunset_ms": 2}, "summary": {"steps": 8240, "resting_hr": null, "hrv": 42}"#)
        XCTAssertEqual(r.items.count, 6, "no item is lost")
        XCTAssertEqual(r.items[0].stages, [SleepStage(stage: "core", start_ms: at(30), end_ms: at(90))], "of the stages, the ones that are")
        XCTAssertEqual([r.items[1].activity, r.items[1].text], ["running", "Outdoor Run"])
        XCTAssertEqual([r.items[1].energy_kcal, r.items[1].distance_m, r.items[1].avg_hr], [240.5, 3200, 142])
        XCTAssertEqual([r.items[2].title, r.items[2].project, r.items[2].account, r.items[2].session_id, r.items[2].detail], ["T", "Jarvis", "a", "s1", "recap"])
        XCTAssertEqual([r.items[3].credit, r.items[3].ended_reason], ["BC Transit", "left_vehicle"])
        XCTAssertEqual([r.items[4].lat, r.items[4].lng, r.items[4].accuracy_m], [48.46, -123.31, 12.5])
        XCTAssertNil(r.items[5].energy_kcal)
        XCTAssertNil(r.items[5].distance_m)
        XCTAssertNil(r.items[5].stages)
        XCTAssertEqual(r.weather?.condition, "clear")
        XCTAssertEqual(r.weather?.high, 14.2)
        XCTAssertEqual(r.weather?.low, 8)
        XCTAssertEqual([r.summary?.steps, r.summary?.hrv], [8240, 42])
        XCTAssertNil(r.summary?.resting_hr)
        // a brain that serves none of the new things
        let old = try reply(item("a", "stay", 1, 2))
        XCTAssertNil(old.weather)
        XCTAssertNil(old.summary)
        XCTAssertNil(old.items[0].stages)
        XCTAssertNil(old.items[0].lat)
        // an envelope whose new parts are of other shapes is still the day
        let odd = try reply(item("a", "stay", 1, 2), #", "weather": [1], "summary": "x""#)
        XCTAssertNil(odd.weather)
        XCTAssertNil(odd.summary)
        XCTAssertEqual(odd.items.count, 1)
    }

    func testAnItemIsReadWhateverItsFieldsAreLikeAsLongAsItHasAnIdAKindAndATime() throws {
        let r = try reply([
            #"{"id": "a", "lane": "you", "kind": "stay", "state": "done", "start_ms": 1}"#,
            #"{"id": "b", "lane": "you", "kind": "stay", "state": "done", "start_ms": 1, "end_ms": null, "place": ["x"], "minutes": "x", "turns": 1.5, "fired": 3, "projects": {"a": 1}, "kept_place": {"id": 1}}"#,
            #"{"id": "c", "lane": "you", "kind": "work", "state": "done", "start_ms": 1, "projects": [{"name": "A", "seconds": null}, {"seconds": 5}], "apps": null}"#,
            #"{"lane": "you", "kind": "stay", "state": "done", "start_ms": 1}"#,
            #"{"id": "e", "lane": "you", "kind": "stay", "state": "done", "start_ms": "1"}"#,
            "[]", "5",
        ].joined(separator: ","))
        XCTAssertEqual(r.items.map(\.id), ["a", "b", "c"])
        XCTAssertNil(r.items[1].place)
        XCTAssertNil(r.items[1].minutes)
        XCTAssertNil(r.items[1].projects)
        XCTAssertNil(r.items[1].kept_place)
        XCTAssertEqual(r.items[2].projects?.map(\.name), ["A"], "the project with no name is no project")
        XCTAssertEqual(r.items[2].projects?.first?.seconds, 0)
    }

    // MARK: the bar: the night's stages and the sun

    func testTheNightIsShadedByBlocksThatDoNotOverlapWhereTwoStagesMeetTheHigherRankingOneShowsAndWhatNoStageCoversIsUnspecified() {
        func st(_ k: TLStage.Kind, _ a: Double, _ b: Double) -> TLStage { TLStage(kind: k, t0: a, t1: b) }
        // two sources overlap from 30 to 50: deep outranks core; nothing covers 90 to 120
        XCTAssertEqual(TLStage.blocks([st(.core, 0, 50), st(.deep, 30, 70), st(.rem, 70, 90)], in: 10, 120),
                       [st(.core, 10, 30), st(.deep, 30, 70), st(.rem, 70, 90), st(.unspecified, 90, 120)])
        // it is the rank that decides and not which began later: awake > rem > deep > core > unspecified > in bed
        XCTAssertEqual(TLStage.blocks([st(.rem, 0, 60), st(.deep, 30, 70)], in: 0, 80), [st(.rem, 0, 60), st(.deep, 60, 70), st(.unspecified, 70, 80)])
        XCTAssertEqual(TLStage.blocks([st(.deep, 0, 50), st(.core, 20, 70)], in: 0, 70), [st(.deep, 0, 50), st(.core, 50, 70)])
        XCTAssertEqual(TLStage.blocks([st(.core, 0, 100), st(.awake, 40, 50)], in: 0, 100), [st(.core, 0, 40), st(.awake, 40, 50), st(.core, 50, 100)])
        XCTAssertEqual(TLStage.blocks([st(.inBed, 0, 100), st(.core, 20, 60)], in: 0, 100), [st(.inBed, 0, 20), st(.core, 20, 60), st(.inBed, 60, 100)], "the time in bed shows only where nothing else does")
        XCTAssertEqual(TLStage.blocks([st(.inBed, 0, 100), st(.unspecified, 0, 50)], in: 0, 100), [st(.unspecified, 0, 50), st(.inBed, 50, 100)])
        XCTAssertEqual(TLStage.blocks([st(.core, 0, 30), st(.core, 20, 60)], in: 0, 60), [st(.core, 0, 60)], "two of a kind are one block")
        XCTAssertEqual(TLStage.blocks([st(.deep, 0, 10), st(.deep, 10, 20)], in: 0, 20), [st(.deep, 0, 20)], "neighbours of a kind are one block")
        XCTAssertEqual(TLStage.blocks([], in: 0, 20), [st(.unspecified, 0, 20)])
        XCTAssertEqual(TLStage.blocks([st(.deep, 100, 200)], in: 0, 20), [st(.unspecified, 0, 20)], "a stage outside is not in it")
        XCTAssertEqual(TLStage.blocks([st(.deep, 0, 20)], in: 5, 5), [])
        XCTAssertEqual(TLStage.blocks([st(.deep, 0, 20)], in: .nan, 5), [])
        XCTAssertEqual(TLStage.blocks([st(.deep, 0, 20)], in: 0, .infinity), [])
    }

    func testTwoSourcesOfOneNightAreCountedOnceAndTheTotalsAndTheBarReadOneTimeline() throws {
        func st(_ k: String, _ a: Double, _ b: Double) -> String { #"{"stage": "\#(k)", "start_ms": \#(at(a)), "end_ms": \#(at(b))}"# }
        // an early night with no stages (so the bar begins at 06:00), and an afternoon sleep 840 to 1040 that the watch and a bedside app both saw, with the time in bed under all of it
        let stages = [st("in_bed", 830, 1050), st("core", 840, 900), st("core", 870, 930), st("deep", 890, 910), st("rem", 960, 1000), st("rem", 980, 1020), st("awake", 935, 940), st("awake", 937, 945)]
        let r = try reply([item("n0", "sleep", 0, 300), item("n", "sleep", 840, 1040, #""stages": [\#(stages.joined(separator: ","))]"#)].joined(separator: ","))
        let m = build(r, now: nil)
        let sleep = try XCTUnwrap(try entry(m, "n").sleep)
        XCTAssertEqual([sleep.deep, sleep.core, sleep.rem, sleep.awake], [20, 70, 60, 1], "an hour seen twice is an hour, and two awake stages that overlap are one time awake")
        XCTAssertEqual(sleep.summary, "深睡 20 分 · 核心 1:10 · REM 1:00 · 醒 1 次")
        XCTAssertEqual(sleep.stages, [TLStage(kind: .core, t0: 840, t1: 890), TLStage(kind: .deep, t0: 890, t1: 910), TLStage(kind: .core, t0: 910, t1: 930), TLStage(kind: .inBed, t0: 930, t1: 935),
                                      TLStage(kind: .awake, t0: 935, t1: 945), TLStage(kind: .inBed, t0: 945, t1: 960), TLStage(kind: .rem, t0: 960, t1: 1020), TLStage(kind: .inBed, t0: 1020, t1: 1040)])
        // the bar shades the same blocks that the totals add up
        let piece = try XCTUnwrap(m.scene.pieces.first { $0.id == "n" })
        let shaded = TLStage.blocks(piece.stages, in: piece.t0, piece.t1)
        XCTAssertEqual(shaded, sleep.stages)
        func minutes(_ k: TLStage.Kind) -> Int { Int(shaded.filter { $0.kind == k }.reduce(0) { $0 + $1.t1 - $1.t0 }) }
        XCTAssertEqual([minutes(.deep), minutes(.core), minutes(.rem)], [sleep.deep, sleep.core, sleep.rem])
    }

    func testTheNightStagesAreDrawnAsTheSpecSays() {
        let sleep = TLPaint.sleep
        XCTAssertEqual([TLPaint.look(.deep).alpha, TLPaint.look(.core).alpha, TLPaint.look(.rem).alpha, TLPaint.look(.awake).alpha, TLPaint.look(.inBed).alpha, TLPaint.look(.unspecified).alpha],
                       [1, 0.78, 0.8, 0.25, 0.25, 0.6])
        XCTAssertEqual(TLPaint.look(.deep).rgb, sleep)
        XCTAssertEqual(TLPaint.look(.rem).rgb, RGB(150, 160, 235), "the lighter blue")
        XCTAssertEqual(TLPaint.look(.awake).rgb, sleep, "the ground shows through it")
    }

    func testOnlyTheNightsPieceOfTheBarCarriesStagesAndTheSunComesWithTheScene() throws {
        let m = build(TimelineFixture.today(cal))
        let pieces = m.scene.pieces
        let night = try XCTUnwrap(pieces.first { $0.fill == .sleep })
        XCTAssertEqual(night.stages.count, 12)
        XCTAssertEqual(night.stages, try XCTUnwrap(try entry(m, "sleep-0").sleep).stages)
        XCTAssertTrue(pieces.filter { $0.fill != .sleep }.allSatisfy { $0.stages.isEmpty })
        XCTAssertEqual(m.scene.sun, TLSun(rise: 454, set: 1091))
        // a night with no stages is one flat piece, and a day with no weather has no sun
        let r = try reply(item("n", "sleep", 300, 480))
        XCTAssertTrue(try XCTUnwrap(build(r, now: nil).scene.pieces.first { $0.fill == .sleep }).stages.isEmpty)
        XCTAssertNil(build(r, now: nil).scene.sun)
    }

    func testTheSunsGlyphIsDrawnOnlyWhereItOverlapsNoHoursLabelAndOtherwiseItIsATick() throws {
        // the labels of 06 09 12 15 18 21 24 on a bar of 380 pt from 06:00 (to a whole point here): centres 0, 63, 127, 190, 253, 317 and, right-aligned, 374
        let hours: [Double] = [0, 63, 127, 190, 253, 317, 374]
        XCTAssertTrue(TLGeo.sunGlyphFits(33, hours: hours, width: 380), "far from every label: its glyph")
        XCTAssertFalse(TLGeo.sunGlyphFits(257, hours: hours, width: 380), "just after 18: the hours win, a tick only")
        XCTAssertFalse(TLGeo.sunGlyphFits(250, hours: hours, width: 380), "just before it")
        XCTAssertFalse(TLGeo.sunGlyphFits(253, hours: hours, width: 380), "on it exactly")
        XCTAssertTrue(TLGeo.sunGlyphFits(265, hours: hours, width: 380), "12 pt from a label is clear: the glyph and the label touch and do not overlap")
        XCTAssertFalse(TLGeo.sunGlyphFits(264.9, hours: hours, width: 380), "a hair closer is not")
        XCTAssertFalse(TLGeo.sunGlyphFits(2, hours: hours, width: 380), "next to the first label, and at the bar's start")
        XCTAssertFalse(TLGeo.sunGlyphFits(376, hours: hours, width: 380), "at the bar's end the glyph would hang off it")
        XCTAssertTrue(TLGeo.sunGlyphFits(100, hours: [], width: 380))
        XCTAssertTrue(TLGeo.sunGlyphFits(6, hours: [], width: 380), "the edge of the bar is the glyph's own edge")
        XCTAssertFalse(TLGeo.sunGlyphFits(5.9, hours: [], width: 380))
        // the fixture's sunset is 11 minutes after 18:00, a few points from that label on its bar: a tick only
        let m = build(TimelineFixture.today(cal))
        let centres = m.ticks.map { TLGeo.x(Double($0 * 60), m.span, 380) - ($0 == 24 ? 6 : 0) }
        XCTAssertFalse(TLGeo.sunGlyphFits(TLGeo.x(1091, m.span, 380), hours: centres, width: 380))
        XCTAssertTrue(TLGeo.sunGlyphFits(TLGeo.x(1000, m.span, 380), hours: centres, width: 380), "16:40 is clear of 15 and 18")
    }

    func testATapOnTheWeatherSaysWhenTheSunRoseAndSet() throws {
        let m = build(TimelineFixture.today(cal))
        XCTAssertEqual(m.sun?.text, "日出 07:34 · 日落 18:11")
        XCTAssertEqual(TLSun(rise: 400, set: nil).text, "日出 06:40", "what of it there is")
        XCTAssertEqual(TLSun(rise: nil, set: 1100).text, "日落 18:20")
        XCTAssertNil(TLSun(rise: nil, set: nil).text)
        // the header asks for it by the day on show
        XCTAssertEqual(DayTimelineModel.sun(of: TimelineFixture.today(cal))?.text, m.sun?.text)
        XCTAssertNil(DayTimelineModel.sun(of: try reply("")), "a brain that serves no weather")
    }
}
