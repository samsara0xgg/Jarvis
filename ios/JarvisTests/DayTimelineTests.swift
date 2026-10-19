import XCTest
@testable import Jarvis

/// The day as one list: what is a group, what is a trip, what is a row, where the line for now stands, and the words on each.
final class DayTimelineTests: XCTestCase {
    private var cal: Cal { Cal.cur }
    private var base: Int64 { Int64(cal.origin.timeIntervalSince1970 * 1000) }
    private func at(_ minute: Double) -> Int64 { base + Int64(minute * 60_000) }
    private let now = TimelineFixture.now

    private func reply(_ items: String, _ extra: String = "") throws -> DayReply {
        let day = #"{"date": "2026-10-08", "tz": "UTC", "start_ms": \#(base), "end_ms": \#(base + 86_400_000), "now_ms": \#(at(now)), "missing": [], "items": [\#(items)]\#(extra)}"#
        return try JSONDecoder().decode(DayReply.self, from: Data(day.utf8))
    }
    private func build(_ r: DayReply, now: Double? = TimelineFixture.now) -> DayTimelineModel {
        DayTimelineModel.build(r, day: 0, now: now, topic: { $0 == "talk-1" ? "明天下午有什么安排？" : nil })
    }
    private func entry(_ m: DayTimelineModel, _ id: String) throws -> TLEntry { try XCTUnwrap(m.entries.first { $0.id == id }, id) }

    // MARK: the day in order

    func testTheFixtureDayIsSleepGroupsAndTripsInTheOrderOfTheDayWithNowBetween() {
        let m = build(TimelineFixture.today(cal))
        let kinds = m.entries.map(\.kind)
        XCTAssertEqual(kinds, [.sleep, .stay, .talk, .trip, .trip, .stay, .trip, .stay, .trip, .stay, .picture, .call, .trip, .stay, .talk, .now, .event, .reminder])
        XCTAssertEqual(m.entries.map(\.title).prefix(4), ["睡觉", "家", "明天下午有什么安排？", "开车"])
        let t = m.entries.map(\.t0)
        XCTAssertEqual(t, t.sorted(), "the list runs forward in time")
        XCTAssertEqual(m.nowIndex, 15)
        XCTAssertEqual(m.entries[15].title, "现在 21:50")
    }

    func testWhatIsAfterTheLineForNowIsLaterAndWhatIsBeforeIsNot() {
        let m = build(TimelineFixture.today(cal))
        let i = m.nowIndex!
        XCTAssertTrue(m.entries[(i + 1)...].allSatisfy(\.later))
        XCTAssertFalse(m.entries[..<i].contains(where: \.later))
    }

    func testAnotherDayHasNoLineForNowAndNothingLater() {
        let m = build(TimelineFixture.yesterday(cal), now: nil)
        XCTAssertNil(m.nowIndex)
        XCTAssertNil(m.now)
        XCTAssertFalse(m.entries.contains(where: \.later))
    }

    // MARK: the places

    func testAStayIsAGroupNamedForItsPlaceWithTheColourOfThePlace() throws {
        let m = build(TimelineFixture.today(cal))
        let stays = m.entries.filter { $0.kind == .stay }
        XCTAssertEqual(stays.map(\.title), ["家", "A&W", "家", "UVic", "家"], "the name he gave, else the first part of the phone's label")
        XCTAssertEqual(stays[0].color, RGB(255, 168, 150), "home")
        XCTAssertEqual(stays[3].color, RGB(100, 200, 255), "school")
        XCTAssertEqual(stays[1].color, RGB(196, 204, 238), "a place he does not keep is a neutral haze")
        XCTAssertTrue(stays[0].kept && stays[3].kept)
        XCTAssertFalse(stays[1].kept)
    }

    func testAnotherKeptPlaceKeepsTheHueItsIdPicks() throws {
        let r = try reply(#"{"id": "s1", "lane": "you", "kind": "stay", "state": "done", "start_ms": \#(at(600)), "end_ms": \#(at(700)), "place": "Cafe, Main St", "kept_place": {"id": "p-cafe", "name": "Cafe", "role": "other"}}"#)
        let hues = [RGB(240, 132, 218), RGB(132, 152, 255), RGB(172, 124, 255), RGB(111, 224, 180)]
        XCTAssertEqual(build(r).entries[0].color, hues[Int(PlaceTint.fnv("p-cafe") % 4)])
    }

    func testAStayWithNoNameAtAllIsSaidSo() throws {
        let r = try reply(#"{"id": "s1", "lane": "you", "kind": "stay", "state": "done", "start_ms": \#(at(600)), "end_ms": \#(at(700))}"#)
        XCTAssertEqual(build(r).entries[0].title, "没名字的地方")
    }

    func testAStayShowsItsHoursAndHowLongAndOneStillOnShowsWhenItBeganAndHowLongSoFar() throws {
        let m = build(TimelineFixture.today(cal))
        XCTAssertEqual(try entry(m, "stay-1").right, "09:05–11:25 · 2 时 20 分")
        XCTAssertEqual(try entry(m, "stay-13").right, "18:50 起 · 3 时", "21:50 less 18:50")
        // gone by, the same stay has no length to count
        XCTAssertEqual(build(TimelineFixture.today(cal), now: nil).entries.first { $0.id == "stay-13" }?.right, "18:50 起")
    }

    func testAStayThatBeganTheEveningBeforeSaysSo() throws {
        let r = try reply(#"{"id": "s1", "lane": "you", "kind": "stay", "state": "done", "start_ms": \#(at(-120)), "end_ms": \#(at(120)), "place": "Home"}"#)
        let e = build(r).entries[0]
        XCTAssertEqual(e.right, "昨天 22:00–02:00 · 4 时")
        XCTAssertEqual(e.t0, -120)
    }

    // MARK: the trips

    func testTripsAreRowsBetweenTheGroupsInTheWordsOfWhatHeDid() throws {
        let m = build(TimelineFixture.today(cal))
        let drive = try entry(m, "move-3"), walk = try entry(m, "move-4")
        XCTAssertEqual([drive.title, walk.title], ["开车", "走路"])
        XCTAssertEqual([drive.symbol, walk.symbol], ["car.fill", "figure.walk"])
        XCTAssertEqual([drive.right, walk.right], ["1 时 40 分", "10 分"])
        XCTAssertEqual(drive.time, "11:25")
        let r = try reply(#"{"id": "a", "lane": "you", "kind": "move", "state": "done", "start_ms": \#(at(10)), "end_ms": \#(at(40)), "activity": "running"}, {"id": "b", "lane": "you", "kind": "move", "state": "done", "start_ms": \#(at(50)), "end_ms": \#(at(80)), "activity": "cycling"}"#)
        XCTAssertEqual(build(r, now: nil).entries.map(\.title), ["跑步", "骑车"])
        XCTAssertEqual(build(r, now: nil).entries.map(\.symbol), ["figure.run", "bicycle"])
    }

    func testABusRideIsNamedForItsRouteAndItsStopsAreTheMutedLineUnderIt() throws {
        let m = build(TimelineFixture.today(cal))
        let ride = try entry(m, "ride-8")
        XCTAssertEqual(ride.title, "27 路")
        XCTAssertEqual(ride.detail, "Hillside Centre → UVic Exchange")
        XCTAssertEqual(ride.height, TLMetrics.rowDetail, "the stops have a line of their own, so they are not cut off")
        XCTAssertEqual(ride.symbol, "bus.fill")
        XCTAssertEqual(ride.right, "29 分")
        XCTAssertEqual(ride.more, ["上车 Hillside Centre", "下车 UVic Exchange"], "the stops in full when it is opened")
        // a ride whose other stop is not known yet gives the one it knows; one that knows no stop has no second line
        let one = try reply(#"{"id": "r", "lane": "you", "kind": "ride", "state": "now", "start_ms": \#(at(1280)), "end_ms": null, "route": "12", "board_stop": "Stop A"}"#)
        let a = build(one).entries[0]
        XCTAssertEqual([a.title, a.detail], ["12 路", "Stop A"])
        XCTAssertEqual(a.height, TLMetrics.rowDetail)
        XCTAssertEqual(a.more, ["上车 Stop A"])
        let none = try reply(#"{"id": "r", "lane": "you", "kind": "ride", "state": "now", "start_ms": \#(at(1280)), "end_ms": null, "route": "12"}, {"id": "q", "lane": "you", "kind": "ride", "state": "done", "start_ms": \#(at(100)), "end_ms": \#(at(120))}"#)
        let b = build(none).entries.filter { $0.kind == .trip }
        XCTAssertEqual(b.map(\.title), ["公交", "12 路"], "no route is just a bus")
        XCTAssertEqual(b.map(\.detail), [nil, nil])
        XCTAssertEqual(b.map(\.height), [TLMetrics.trip, TLMetrics.trip], "no second line, no taller row")
        XCTAssertTrue(b.allSatisfy { !$0.opens })
    }

    func testTheNightIsARowWithItsHoursAndHowLongHeSlept() throws {
        let sleep = try entry(build(TimelineFixture.today(cal)), "sleep-0")
        XCTAssertEqual(sleep.title, "睡觉")
        XCTAssertEqual(sleep.detail, "00:55–09:05")
        XCTAssertEqual(sleep.right, "8 时 5 分", "the minutes he was asleep")
        XCTAssertEqual(sleep.symbol, "moon.fill")
    }

    // MARK: the rows in a group

    func testATalkIsNamedForItsTopicElseForWhatItIsAndCountsItsTurns() throws {
        let m = build(TimelineFixture.today(cal))
        let a = try entry(m, "talk-1"), b = try entry(m, "talk-2")
        XCTAssertEqual([a.title, b.title], ["明天下午有什么安排？", "和她说话"])
        XCTAssertEqual([a.detail, b.detail], ["6 句", "14 句"])
        XCTAssertEqual(a.talkStart, at(612))
        XCTAssertTrue(a.opens)
    }

    func testAPictureIsOnlyCalledOneAndSaysTheFirstLineOfWhatTheBrainWroteOfIt() throws {
        let p = try entry(build(TimelineFixture.today(cal)), "picture-pic-demo-1")
        XCTAssertEqual(p.title, "照片")
        XCTAssertEqual(p.detail, "黑板上写着积分公式，下面一行是作业要求。")
        XCTAssertEqual(p.more.count, 2, "both lines when it is opened")
        XCTAssertEqual(p.attachment, "pic-demo-1")
        // no note yet: a picture and nothing more
        let r = try reply(#"{"id": "picture-x", "lane": "you", "kind": "picture", "state": "done", "start_ms": \#(at(600)), "end_ms": \#(at(600)), "attachment": "x"}"#)
        XCTAssertNil(build(r).entries[0].detail)
    }

    func testAnEventFromMailSaysSoAndWhereAndAPrivateOneIsOnlyPrivate() throws {
        let m = build(TimelineFixture.today(cal))
        let e = try entry(m, "event-demo-1")
        XCTAssertEqual(e.title, "陶艺课")
        XCTAssertEqual(e.detail, "从邮件里收的，还没确认 · Studio 4")
        XCTAssertEqual(e.right, "30 分")
        XCTAssertTrue(e.later)
        let r = try reply(#"{"id": "event-p", "lane": "you", "kind": "event", "state": "next", "start_ms": \#(at(1400)), "end_ms": \#(at(1430)), "title": "Counselling", "private": true, "place": "Clinic"}"#)
        let p = build(r).entries.first { $0.id == "event-p" }!
        XCTAssertEqual(p.title, "私事")
        XCTAssertNil(p.detail, "a private event names no place either")
    }

    func testAReminderSaysWhetherItRangAndWasAnswered() throws {
        let m = build(TimelineFixture.today(cal))
        let r = try entry(m, "reminder-demo-1")
        XCTAssertEqual(r.title, "给 Mia 回电话")
        XCTAssertEqual(r.detail, "还没到点")
        XCTAssertEqual(r.time, "23:00")
        let rang = try reply(#"{"id": "r1", "lane": "her", "kind": "reminder", "state": "done", "start_ms": \#(at(600)), "text": "A", "fired": true, "acknowledged": true}, {"id": "r2", "lane": "her", "kind": "reminder", "state": "done", "start_ms": \#(at(700)), "text": "B", "fired": true, "acknowledged": false}"#)
        XCTAssertEqual(build(rang, now: nil).entries.map(\.detail), ["已经响过，你确认过了", "已经响过，你还没确认"])
        XCTAssertFalse(r.opens, "its one line is shown in full already")
    }

    func testABusDepartureNamesTheRouteAndTheStop() throws {
        let r = try reply(#"{"id": "departure-1", "lane": "you", "kind": "departure", "state": "next", "start_ms": \#(at(1400)), "end_ms": \#(at(1400)), "title": "12 · Stop A", "route": "12", "board_stop": "Stop A", "to": "Downtown", "leave_at_ms": \#(at(1390))}"#)
        let d = build(r, now: nil).entries[0]
        XCTAssertEqual(d.title, "12 路 发车")
        XCTAssertEqual(d.detail, "在Stop A上车 · 去Downtown · 23:10 出门")
        XCTAssertEqual(d.time, "23:20")
    }

    func testMacWorkIsNamedForTheTopProjectAndCountsTheActiveTime() throws {
        let r = try reply(#"{"id": "w1", "lane": "you", "kind": "work", "state": "done", "start_ms": \#(at(665)), "end_ms": \#(at(798)), "active_s": 6780, "projects": [{"name": "Jarvis", "seconds": 6780}], "apps": [{"name": "Xcode", "seconds": 2400}]}, {"id": "w2", "lane": "you", "kind": "work", "state": "done", "start_ms": \#(at(900)), "end_ms": \#(at(930))}"#)
        let m = build(r)
        XCTAssertEqual(m.entries[0].title, "Jarvis")
        XCTAssertEqual(m.entries[0].right, "1 时 53 分")
        XCTAssertEqual(m.entries[0].detail, "项目 Jarvis 1 时 53 分 · 应用 Xcode 40 分")
        XCTAssertEqual(m.entries[1].title, "在 Mac 上工作")
        XCTAssertEqual(m.entries[1].right, "30 分")
    }

    func testWhatMatteredIsNamedByItsLabelAndLit() throws {
        let m = build(TimelineFixture.today(cal))
        let c = try entry(m, "call-demo-1")
        XCTAssertEqual(c.title, "和 Mia 聊了很久")
        XCTAssertTrue(c.lit)
        XCTAssertEqual(c.detail, "FaceTime")
        XCTAssertFalse(try entry(m, "talk-1").lit)
        // a place stays its own name even when marked
        let r = try reply(#"{"id": "s1", "lane": "you", "kind": "stay", "state": "done", "start_ms": \#(at(600)), "end_ms": \#(at(700)), "place": "Cafe, Main"}"#, #", "highlights": [{"id": "s1", "label": "Coffee", "by": "him"}]"#)
        XCTAssertEqual(build(r).entries[0].title, "Cafe")
        XCTAssertFalse(build(r).entries[0].lit)
    }

    func testAKindThisPhoneDoesNotKnowIsLeftOut() throws {
        let r = try reply(#"{"id": "p", "lane": "you", "kind": "payment", "state": "done", "start_ms": \#(at(600)), "end_ms": \#(at(600))}"#)
        XCTAssertTrue(build(r).entries.filter { $0.kind != .now }.isEmpty)
        XCTAssertTrue(build(r).isEmpty)
    }

    // MARK: what the list says besides

    func testHerSentenceAndTheSourceThatWasNotReadAreCarried() {
        let m = build(TimelineFixture.today(cal))
        XCTAssertEqual(m.line, "今天去了学校，下午在 UVic 待得最久，晚上回家早点休息。")
        XCTAssertEqual(m.missing, "还没读到：Mac 的时间")
        XCTAssertEqual(m.height, m.entries.reduce(0) { $0 + $1.height } + TLMetrics.note, "the footnote has its own height under the last row")
        XCTAssertNil(build(TimelineFixture.yesterday(cal), now: nil).missing)
    }

    func testDurationsAreInMinutesThenHoursAndMinutes() {
        XCTAssertEqual([0, 40, 59, 60, 100, 120, 485].map(tlDur), ["0 分", "40 分", "59 分", "1 时", "1 时 40 分", "2 时", "8 时 5 分"])
    }

    func testRowsHaveTheHeightsTheListIsLaidOutWithAndTheOffsetsAddUp() throws {
        let m = build(TimelineFixture.today(cal))
        let heights = m.entries.map(\.height)
        XCTAssertEqual(m.offsets.first, 0)
        XCTAssertEqual(m.offsets.last! + heights.last!, m.entries.reduce(0) { $0 + $1.height })
        XCTAssertEqual(try entry(m, "stay-1").height, TLMetrics.stay)
        XCTAssertEqual(try entry(m, "move-3").height, TLMetrics.trip)
        XCTAssertEqual(try entry(m, "ride-8").height, TLMetrics.rowDetail, "a ride has its stops under it")
        XCTAssertEqual(try entry(m, "talk-1").height, TLMetrics.rowDetail, "a row with a muted line is taller")
        XCTAssertEqual(try entry(m, "call-demo-1").height, TLMetrics.rowDetail)
        XCTAssertEqual(m.top(of: "talk-1"), m.offsets[2])
        XCTAssertNil(m.top(of: "nothing"))
    }

    // MARK: the overview bar

    func testTheBarRunsFromTheFirstThingOfTheDayOrSixToMidnight() throws {
        XCTAssertEqual(build(TimelineFixture.today(cal)).span, 55...DAY, "the night began at 00:55")
        let r = try reply(#"{"id": "s1", "lane": "you", "kind": "stay", "state": "done", "start_ms": \#(at(500)), "end_ms": \#(at(700))}"#)
        XCTAssertEqual(build(r).span, 360...DAY, "nothing before 06:00: it starts there")
        XCTAssertEqual(build(try reply("")).span, 360...DAY)
        // the evening before is cut at the day's start
        let before = try reply(#"{"id": "s1", "lane": "you", "kind": "sleep", "state": "done", "start_ms": \#(at(-30)), "end_ms": \#(at(400))}"#)
        XCTAssertEqual(build(before).span, 0...DAY)
        XCTAssertEqual(build(before).segs[0].t0, 0)
    }

    func testTheBarColoursThePlaceTheTripTheNightAndLeavesWhatWasNotRecordedAsTrack() throws {
        let r = try reply(#"{"id": "n", "lane": "you", "kind": "sleep", "state": "done", "start_ms": \#(at(0)), "end_ms": \#(at(360))}, {"id": "s", "lane": "you", "kind": "stay", "state": "done", "start_ms": \#(at(400)), "end_ms": \#(at(700)), "place": "Home", "kept_place": {"id": "h", "name": "Home", "role": "home"}}, {"id": "u", "lane": "you", "kind": "stay", "state": "done", "start_ms": \#(at(800)), "end_ms": \#(at(900)), "place": "Some Cafe"}, {"id": "m", "lane": "you", "kind": "move", "state": "done", "start_ms": \#(at(700)), "end_ms": \#(at(740)), "activity": "walking"}"#)
        let m = build(r, now: nil)
        let n = 1440, cells = m.cells(n)
        func cell(_ minute: Double) -> TLCell { cells[Int((minute - m.span.lowerBound) / (m.span.upperBound - m.span.lowerBound) * Double(n))] }
        XCTAssertEqual(cell(120), TLCell(r: 112, g: 120, b: 205, a: 0.9), "the night, indigo")
        XCTAssertEqual(cell(500), TLCell(r: 255, g: 168, b: 150, a: 0.85), "home, in the colour of home")
        XCTAssertEqual(cell(720), TLCell(r: 196, g: 208, b: 255, a: 0.35), "the walk")
        XCTAssertEqual(cell(850), TLCell(r: 196, g: 204, b: 238, a: 0.28), "a place he does not keep, faint")
        XCTAssertEqual(cell(1000), .track, "nothing recorded")
        XCTAssertEqual(m.stretch(at: 500), "s")
        XCTAssertEqual(m.stretch(at: 1000), "")
    }

    func testAStayWithNoEndOnAnotherDayGoesOnToMidnightOnTheBar() {
        let m = build(TimelineFixture.yesterday(cal), now: nil)
        XCTAssertEqual(m.stretch(at: 1300), "stay-5", "the stay that began at 17:45 and has no end")
        XCTAssertEqual(m.stretch(at: 1439), "stay-5")
        // today the same stay goes on to now and no further
        let today = build(TimelineFixture.today(cal))
        XCTAssertEqual(today.stretch(at: 1300), "stay-13")
        XCTAssertEqual(today.stretch(at: 1330), "", "nothing is known after now")
    }

    func testTheNightIsOnTopOfAStayItOverlaps() throws {
        let r = try reply(#"{"id": "s", "lane": "you", "kind": "stay", "state": "done", "start_ms": \#(at(0)), "end_ms": \#(at(900)), "place": "Home"}, {"id": "n", "lane": "you", "kind": "sleep", "state": "done", "start_ms": \#(at(60)), "end_ms": \#(at(400))}"#)
        let m = build(r, now: nil)
        XCTAssertEqual(m.stretch(at: 100), "n")
        XCTAssertEqual(m.stretch(at: 500), "s")
    }

    func testTheRowAtAMinuteIsTheLastOneBegunByThenElseTheFirst() throws {
        let m = build(TimelineFixture.today(cal))
        XCTAssertEqual(m.entry(at: 700)?.id, "move-3", "in the drive")
        XCTAssertEqual(m.entry(at: 600)?.id, "stay-1", "at home before the talk")
        XCTAssertEqual(m.entry(at: 620)?.id, "talk-1")
        XCTAssertEqual(m.entry(at: 10)?.id, "sleep-0", "before anything: the first")
        XCTAssertEqual(m.entry(at: 1439)?.id, "reminder-demo-1")
    }

    // MARK: the setting

    func testTheHomeScreenShowsTheTimelineUntilHeChoosesTheDialAndKeepsWhatHeChose() {
        let name = "test.homeModule.\(UUID().uuidString)"
        let d = UserDefaults(suiteName: name)!
        defer { d.removePersistentDomain(forName: name) }
        XCTAssertEqual(HomeModule(d).kind, .timeline)
        let h = HomeModule(d)
        h.kind = .stars
        XCTAssertEqual(d.string(forKey: HomeModule.key), "stars")
        XCTAssertEqual(HomeModule(d).kind, .stars)
        d.set("something new", forKey: HomeModule.key)
        XCTAssertEqual(HomeModule(d).kind, .timeline, "a word this build does not know is the default")
    }

    // MARK: what arrives

    func testTheNewFieldsAreReadAndOneThatCannotBeReadCostsOnlyItself() throws {
        let r = try reply(#"{"id": "a", "lane": "you", "kind": "ride", "state": "done", "start_ms": 1, "end_ms": 2, "alight_stop": "Stop B", "note": 5}, {"id": "b", "lane": "you", "kind": "picture", "state": "done", "start_ms": 1, "end_ms": 1, "note": "A note.", "alight_stop": ["x"]}, {"id": "c", "lane": "you", "kind": "stay", "state": "done", "start_ms": 1, "end_ms": 2, "note": null}"#)
        XCTAssertEqual(r.items.count, 3, "no item is lost")
        XCTAssertEqual(r.items[0].alight_stop, "Stop B")
        XCTAssertNil(r.items[0].note)
        XCTAssertEqual(r.items[1].note, "A note.")
        XCTAssertNil(r.items[1].alight_stop)
        XCTAssertNil(r.items[2].note)
        XCTAssertNil(try reply(#"{"id": "d", "lane": "you", "kind": "stay", "state": "done", "start_ms": 1}"#).items[0].alight_stop, "an old brain has neither")
    }

    func testTheSourcesSaidMissingAreNamedTheWayTheDialNamesThem() {
        XCTAssertEqual(BrainLink.missingText(["mac", "calendar"]), "Mac、日历 没连上")
        XCTAssertNil(BrainLink.missingText([]))
        XCTAssertNil(BrainLink.missingText(["something new"]))
    }

    func testTheListNamesWhatWasNotReadByWhatItGivesTheDay() {
        XCTAssertEqual(DayTimelineModel.missingText(["mac"]), "还没读到：Mac 的时间")
        XCTAssertEqual(DayTimelineModel.missingText(["phone", "reminders", "mac", "talks", "calendar", "departures"]), "还没读到：手机的记录、提醒、Mac 的时间、对话、日历、公交")
        XCTAssertNil(DayTimelineModel.missingText([]))
        XCTAssertNil(DayTimelineModel.missingText(["something new"]))
    }

    // MARK: what a row he has open tells the brain (ADR 0214)

    func testARowOfAKindTheHostKnowsIsTheChipWithItsTitleAsTheRowShowsItAndItsStart() throws {
        let m = build(TimelineFixture.today(cal))
        XCTAssertEqual(try entry(m, "event-demo-1").about, About(kind: "event", id: "event-demo-1", title: "陶艺课", startMs: at(1320), mark: .next), "not yet")
        XCTAssertEqual(try entry(m, "reminder-demo-1").about, About(kind: "reminder", id: "reminder-demo-1", title: "给 Mia 回电话", startMs: at(1380), mark: .next))
        XCTAssertEqual(try entry(m, "talk-1").about, About(kind: "talk", id: "talk-1", title: "明天下午有什么安排？", startMs: at(612), mark: .done), "named for its topic; hers, and over")
        XCTAssertEqual(try entry(m, "call-demo-1").about, About(kind: "call", id: "call-demo-1", title: "和 Mia 聊了很久", startMs: at(1000), mark: .lit), "named for the label it was given; his, and over")
        XCTAssertEqual(try entry(m, "stay-1").about?.title, "家")
        XCTAssertEqual(try entry(m, "stay-13").about?.mark, .work, "still on")
        XCTAssertEqual(try entry(m, "sleep-0").about?.kind, "sleep")
        XCTAssertEqual(try entry(m, "move-3").about?.kind, "move")
    }

    func testAPrivateEventGivesTheChipNoMoreThanTheRowSays() throws {
        let r = try reply(#"{"id": "event-p", "lane": "you", "kind": "event", "state": "next", "start_ms": \#(at(1400)), "end_ms": \#(at(1430)), "title": "Counselling", "private": true, "place": "Clinic"}"#)
        XCTAssertEqual(build(r).entries.first { $0.id == "event-p" }?.about?.title, "私事")
    }

    func testARowOfAKindTheHostDoesNotKnowOrWithAnIdItWouldRefuseGivesNoChip() throws {
        let m = build(TimelineFixture.today(cal))
        for id in ["ride-8", "picture-pic-demo-1", "now"] { XCTAssertNil(try entry(m, id).about, id) }
        let dep = try reply(#"{"id": "departure-1", "lane": "you", "kind": "departure", "state": "next", "start_ms": \#(at(1400)), "end_ms": \#(at(1400)), "route": "12"}"#)
        XCTAssertNil(build(dep, now: nil).entries[0].about)
        let odd = try reply(#"{"id": "event a", "lane": "you", "kind": "event", "state": "next", "start_ms": \#(at(1400)), "end_ms": \#(at(1430)), "title": "x"}"#)
        XCTAssertEqual(build(odd).entries.first { $0.id == "event a" }?.title, "x", "the row is there")
        XCTAssertNil(build(odd).entries.first { $0.id == "event a" }?.about, "a space is not in the id set")
    }

    /// the engine, as it is when he talks with the timeline on: `HomeModule` timeline, the day on show the made-up one
    private func withTimeline(_ body: (StarEngine) -> Void) {
        let kept = HomeModule.shared.kind
        HomeModule.shared.kind = .timeline
        defer { HomeModule.shared.kind = kept; MainActor.assumeIsolated { AboutNow.current = nil } }
        let e = StarEngine()
        e.ui.tl.fixture = .init(replies: [0: TimelineFixture.today(cal)], now: TimelineFixture.now)
        e.ui.tl.day = e.ui.tl.today
        body(e)
    }

    func testTheRowOpenInTheListIsTheChipWhenATurnGoesOut() {
        withTimeline { e in
            e.captureAbout()
            XCTAssertNil(e.ui.about, "no row open, no chip")
            e.ui.tl.openRow = "event-demo-1"
            e.captureAbout()
            XCTAssertEqual(e.ui.about, About(kind: "event", id: "event-demo-1", title: "陶艺课", startMs: at(1320), mark: .next))
            MainActor.assumeIsolated { XCTAssertEqual(AboutNow.current?.id, "event-demo-1", "the voice says it too") }
            // another row opened is another item; one of a kind the host does not know leaves the chip as it is
            e.ui.tl.openRow = "talk-1"
            e.captureAbout()
            XCTAssertEqual(e.ui.about?.id, "talk-1")
            e.ui.tl.openRow = "ride-8"
            e.captureAbout()
            XCTAssertEqual(e.ui.about?.id, "talk-1")
        }
    }

    func testTheChipHeTookOffStaysOffWhileThatRowIsTheOpenOne() {
        withTimeline { e in
            e.ui.tl.openRow = "event-demo-1"
            e.captureAbout()
            e.dropAbout()
            XCTAssertNil(e.ui.about)
            e.captureAbout()
            XCTAssertNil(e.ui.about)
            e.ui.tl.openRow = "talk-1"
            e.captureAbout()
            XCTAssertEqual(e.ui.about?.id, "talk-1")
        }
    }

    func testAWaitingCardIsNotARowOfTheDayAndGivesNoChip() {
        withTimeline { e in
            e.ui.tl.openCard = "clarification-Qdebug01"
            e.captureAbout()
            XCTAssertNil(e.ui.about)
            e.ui.tl.openRow = "no-such-row"
            e.captureAbout()
            XCTAssertNil(e.ui.about)
        }
    }

    func testTheDialsOwnOpenStarIsTheChipOnlyWhileTheDialIsTheMiddle() {
        withTimeline { e in
            let star = Ev("R-1", 900, .her, .next, "提醒：给 Mia 回电话", "15:00")
            star.live = true; star.kind = "reminder"; star.startMs = at(900)
            e.EV.append(star)
            e.openId = "R-1"
            e.captureAbout()
            XCTAssertNil(e.ui.about, "the dial is out of sight: what it had open is not what he is looking at")
            HomeModule.shared.kind = .stars
            e.captureAbout()
            XCTAssertEqual(e.ui.about?.id, "R-1")
        }
    }

    // MARK: she and her sentence

    func testHerSentenceAndTheBarWaitForHerToGetHomeAndGoWithTheFirstFrameOfHerLeaving() {
        let kept = HomeModule.shared.kind
        HomeModule.shared.kind = .stars
        defer { HomeModule.shared.kind = kept }
        let e = StarEngine()
        XCTAssertFalse(e.ui.tlHome)
        HomeModule.shared.kind = .timeline
        var waited = 0
        for _ in 0..<300 {
            e.syncModule(1.0 / 60)
            let away = (1 - e.tm) * hypot(TLLayout.her.x - CX, TLLayout.her.y - CY)
            XCTAssertEqual(e.ui.tlHome, away < 20, "drawn only once she is within 20 pt of her corner")
            if !e.ui.tlHome { waited += 1 }
        }
        XCTAssertGreaterThan(waited, 10, "she is on her way for a while")
        XCTAssertTrue(e.ui.tlHome)
        HomeModule.shared.kind = .stars
        e.syncModule(1.0 / 60)
        XCTAssertFalse(e.ui.tlHome, "she has hardly moved, and they are gone")
    }
}
