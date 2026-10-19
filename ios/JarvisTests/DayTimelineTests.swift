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
        XCTAssertEqual(kinds, [.sleep, .stay, .talk, .work, .trip, .trip, .stay, .payment, .trip, .stay, .trip, .stay, .picture, .call, .trip, .stay, .work, .claude, .talk, .now, .event, .reminder, .departure])
        XCTAssertEqual(m.entries.map(\.title).prefix(4), ["睡觉", "家", "明天下午有什么安排？", "Jarvis"])
        let t = m.entries.map(\.t0)
        XCTAssertEqual(t, t.sorted(), "the list runs forward in time")
        XCTAssertEqual(m.nowIndex, 19)
        XCTAssertEqual(m.entries[19].title, "现在 21:50")
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

    func testAPaymentIsNamedForWhoGotTheMoneySaysWhatItWasForAndShowsWhatItCameTo() throws {
        let m = build(TimelineFixture.today(cal))
        let p = try entry(m, "payment-demo-1")
        XCTAssertEqual([p.title, p.detail, p.right, p.time], ["A&W", "Teen Burger Combo", "$12.40", "13:20"])
        XCTAssertEqual(p.more, ["支出 $12.40 CAD", "来自支付宝"])
        XCTAssertEqual(p.symbol, "creditcard.fill")
        XCTAssertTrue(p.strongRight && p.opens)
        XCTAssertNil(p.about, "the host does not take a payment yet")
        // money in has a plus; no shop leaves what it was for as the title; a source with no word here is said as it came; no amount is no amount
        let r = try reply(#"{"id": "pay-2", "lane": "you", "kind": "payment", "state": "done", "start_ms": \#(at(600)), "end_ms": \#(at(600)), "source": "wechat_pay", "amount": "1234.50", "currency": "CNY", "direction": "in", "text": "转账"}, {"id": "pay-3", "lane": "you", "kind": "payment", "state": "done", "start_ms": \#(at(700)), "end_ms": \#(at(700)), "source": "venmo", "amount": "5.00", "currency": "AUD", "direction": "out", "counterparty": "Cafe"}, {"id": "pay-4", "lane": "you", "kind": "payment", "state": "done", "start_ms": \#(at(800)), "end_ms": \#(at(800))}"#)
        let rows = build(r, now: nil).entries
        XCTAssertEqual(rows.map(\.title), ["转账", "Cafe", "付款"])
        XCTAssertEqual(rows.map(\.right), ["+¥1,234.50", "AUD 5.00", ""])
        XCTAssertEqual(rows.map(\.more), [["收入 ¥1,234.50 CNY", "来自微信"], ["支出 AUD 5.00", "来自venmo"], []])
        XCTAssertEqual(rows.map(\.detail), [nil, nil, nil])
    }

    func testMoneyIsWrittenWithTheMarkOfItsCurrency() {
        XCTAssertEqual(tlMoney(1240, "CAD"), "$12.40")
        XCTAssertEqual(tlMoney(1240, "usd"), "$12.40")
        XCTAssertEqual(tlMoney(123450, "CNY"), "¥1,234.50")
        XCTAssertEqual(tlMoney(5, "EUR"), "€0.05")
        XCTAssertEqual(tlMoney(500, "AUD"), "AUD 5.00")
        XCTAssertEqual(tlMoney(500, nil), "5.00")
        XCTAssertEqual(tlMoney(-1240, "CAD"), "-$12.40")
        XCTAssertEqual(tlMoney(100_000_000, "CAD"), "$1,000,000.00")
    }

    func testAClaudeChatIsNamedForTheConversationAndCountsItsMessagesAndADayListsItsChats() throws {
        let m = build(TimelineFixture.today(cal))
        let c = try entry(m, "claude-demo-1")
        XCTAssertEqual([c.title, c.detail, c.right, c.time], ["积分换元的符号问题", "14 条消息", "25 分", "20:10"])
        XCTAssertEqual(c.symbol, "sparkles")
        XCTAssertNil(c.about)
        let r = try reply(#"{"id": "claude-x-1", "lane": "you", "kind": "claude_chat", "state": "done", "start_ms": \#(at(600)), "end_ms": \#(at(620)), "text": "Claude conversation", "messages": 3}, {"id": "claude-day-2026-10-08", "lane": "you", "kind": "claude_day", "state": "done", "start_ms": \#(at(700)), "end_ms": \#(at(800)), "source": "claude_daily", "text": "微积分; 论文提纲 ;", "chats": 2}"#)
        let rows = build(r, now: nil).entries
        XCTAssertEqual(rows.map(\.title), ["Claude 对话", "Claude 对话小结"], "a conversation with no name is only one")
        XCTAssertEqual(rows[1].detail, "微积分、论文提纲")
        XCTAssertEqual(rows[1].more, ["微积分", "论文提纲"], "its titles as a list when it is opened")
        XCTAssertEqual(rows[1].right, "2 段")
        XCTAssertTrue(rows[1].opens)
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
        let r = try reply(#"{"id": "p", "lane": "you", "kind": "flight", "state": "done", "start_ms": \#(at(600)), "end_ms": \#(at(600))}"#)
        XCTAssertTrue(build(r).entries.filter { $0.kind != .now }.isEmpty)
        XCTAssertTrue(build(r).isEmpty)
    }

    // MARK: what the list says besides

    func testHerSentenceAndTheSourceThatWasNotReadAreCarried() {
        let m = build(TimelineFixture.today(cal))
        XCTAssertEqual(m.line, "今天去了学校，下午在 UVic 待得最久，晚上回家早点休息。")
        XCTAssertEqual(m.missing, "还没读到：日历")
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

    func testTheBarStartsAtSixOrAtTheHourBeforeHeWokeWhicheverIsEarlier() throws {
        // the night that ended at 09:05 would start it at 08:00, but it is never later than 06:00
        XCTAssertEqual(build(TimelineFixture.today(cal)).span, 360...DAY)
        XCTAssertEqual(DayTimelineModel.axisStart(wake: 545, early: []), 360)
        XCTAssertEqual(DayTimelineModel.axisStart(wake: 320, early: []), 240, "05:20: the hour before is 04:20, which is 04:00")
        XCTAssertEqual(DayTimelineModel.axisStart(wake: 360, early: []), 300, "06:00: 05:00")
        XCTAssertEqual(DayTimelineModel.axisStart(wake: 30, early: []), 0, "never before midnight")
        XCTAssertEqual(DayTimelineModel.axisStart(wake: nil, early: []), 360, "no night that ended on this day: 06:00")
        let r = try reply(#"{"id": "n", "lane": "you", "kind": "sleep", "state": "done", "start_ms": \#(at(-60)), "end_ms": \#(at(320))}, {"id": "s", "lane": "you", "kind": "stay", "state": "done", "start_ms": \#(at(320)), "end_ms": \#(at(700)), "place": "Home"}"#)
        XCTAssertEqual(build(r).span, 240...DAY)
        XCTAssertEqual(build(try reply("")).span, 360...DAY)
        // a night still going, or one that ends tomorrow, tells nothing
        let on = try reply(#"{"id": "n", "lane": "you", "kind": "sleep", "state": "now", "start_ms": \#(at(-60)), "end_ms": null}, {"id": "t", "lane": "you", "kind": "sleep", "state": "next", "start_ms": \#(at(1380)), "end_ms": \#(at(1900))}"#)
        XCTAssertEqual(build(on).span, 360...DAY)
        // the evening before is cut at the day's start
        let before = try reply(#"{"id": "s1", "lane": "you", "kind": "sleep", "state": "done", "start_ms": \#(at(-30)), "end_ms": \#(at(20))}"#)
        XCTAssertEqual(build(before).span, 0...DAY)
        XCTAssertEqual(build(before).segs[0].t0, 0)
    }

    func testSomethingThatBeganBeforeTheStartBringsItBackToItsHourButAStayAloneDoesNot() throws {
        XCTAssertEqual(DayTimelineModel.axisStart(wake: 480, early: [280]), 240, "04:40 is in the hour of 04:00")
        XCTAssertEqual(DayTimelineModel.axisStart(wake: 480, early: [359]), 300)
        XCTAssertEqual(DayTimelineModel.axisStart(wake: 480, early: [130, 280]), 120, "the earliest of them")
        XCTAssertEqual(DayTimelineModel.axisStart(wake: 480, early: [-30, 360, 400]), 360, "what began the evening before, or at the start, or later, moves nothing")
        func day(_ kind: String, _ extra: String = "") throws -> DayTimelineModel {
            build(try reply(#"{"id": "n", "lane": "you", "kind": "sleep", "state": "done", "start_ms": \#(at(-60)), "end_ms": \#(at(480))}, {"id": "x", "lane": "you", "kind": "\#(kind)", "state": "done", "start_ms": \#(at(280)), "end_ms": \#(at(300))\#(extra)}"#))
        }
        for kind in ["move", "ride", "talk", "picture", "work", "call"] { XCTAssertEqual(try day(kind).span.lowerBound, 240, kind) }
        XCTAssertEqual(try day("event", #", "title": "x""#).span.lowerBound, 240)
        XCTAssertEqual(try day("payment", #", "amount": "1.00", "direction": "out""#).span.lowerBound, 240)
        for kind in ["stay", "reminder", "departure"] { XCTAssertEqual(try day(kind).span.lowerBound, 360, "\(kind) does not") }
    }

    func testTheBarIsTickedEveryThreeHoursInsideItsSpan() throws {
        func ticks(_ lo: Double) -> [Int] { DayTimelineModel(date: "", entries: [], segs: [], span: lo...DAY).ticks }
        XCTAssertEqual(ticks(360), [6, 9, 12, 15, 18, 21, 24])
        XCTAssertEqual(ticks(240), [6, 9, 12, 15, 18, 21, 24], "03 is before the start")
        XCTAssertEqual(ticks(0), [0, 3, 6, 9, 12, 15, 18, 21, 24])
        XCTAssertEqual(ticks(181), [6, 9, 12, 15, 18, 21, 24])
        XCTAssertEqual(ticks(180), [3, 6, 9, 12, 15, 18, 21, 24], "a tick on the start is kept")
        XCTAssertEqual(build(TimelineFixture.today(cal)).ticks, [6, 9, 12, 15, 18, 21, 24])
    }

    func testTheBarIsPiecesOfTheDayFromItsStartToMidnightEachWithItsFillAndTheWordItMayWear() throws {
        let sc = build(TimelineFixture.today(cal)).scene
        XCTAssertEqual(sc.pieces.first?.t0, 360)
        XCTAssertEqual(sc.pieces.last?.t1, DAY)
        XCTAssertEqual(zip(sc.pieces, sc.pieces.dropFirst()).filter { $0.t1 != $1.t0 }.count, 0, "one after the other, none missing, none over")
        func piece(_ id: String) throws -> TLPiece { try XCTUnwrap(sc.pieces.first { $0.id == id }, id) }
        let night = try piece("sleep-0")
        XCTAssertEqual([night.t0, night.t1], [360, 545], "the night's tail, from where the bar begins")
        XCTAssertEqual(night.fill, .sleep)
        XCTAssertEqual([night.name, night.icon], ["8:05", "moon.fill"], "its whole length, not the part the bar shows")
        let home = try piece("stay-1")
        XCTAssertEqual(home.fill, .place(RGB(255, 168, 150), kept: true))
        XCTAssertEqual([home.name, home.icon], ["家", "house.fill"])
        XCTAssertEqual([try piece("stay-9").name, try piece("stay-9").icon], ["UVic", "graduationcap.fill"])
        XCTAssertEqual(try piece("stay-5").fill, .place(RGB(196, 204, 238), kept: false), "a place he does not keep")
        XCTAssertEqual([try piece("stay-5").name, try piece("stay-5").icon], ["A&W", "mappin"])
        XCTAssertEqual([try piece("move-3").fill, try piece("move-4").fill], [.trip, .trip])
        XCTAssertEqual([try piece("move-3").name, try piece("move-3").icon], ["开车", "car.fill"])
        XCTAssertEqual([try piece("ride-8").name, try piece("ride-8").icon], ["27", "bus.fill"], "a ride is its route")
        let ahead = try XCTUnwrap(sc.pieces.last)
        XCTAssertEqual(ahead.t0, TimelineFixture.now, "after now: what has not happened")
        XCTAssertEqual(ahead.fill, .ahead)
        XCTAssertEqual(sc.pieces.filter { $0.fill == .blank }.count, 0, "this day has no hole in it")
    }

    func testWhereNothingWasRecordedThereIsABlankPieceAndTheNightIsOnTopAndCutAtTheStart() throws {
        let r = try reply(#"{"id": "s0", "lane": "you", "kind": "stay", "state": "done", "start_ms": \#(at(0)), "end_ms": \#(at(900)), "place": "Home"}, {"id": "n", "lane": "you", "kind": "sleep", "state": "done", "start_ms": \#(at(60)), "end_ms": \#(at(400))}, {"id": "s1", "lane": "you", "kind": "stay", "state": "done", "start_ms": \#(at(1000)), "end_ms": \#(at(1100)), "place": "Cafe"}, {"id": "m", "lane": "you", "kind": "move", "state": "done", "start_ms": \#(at(1100)), "end_ms": \#(at(1110)), "activity": "walking"}"#)
        let sc = build(r, now: nil).scene
        XCTAssertEqual(sc.span, 300...DAY)
        XCTAssertEqual(sc.pieces.map(\.id), ["n", "s0", "blank-900", "s1", "m", "blank-1110"])
        XCTAssertEqual(sc.pieces.map(\.t0), [300, 400, 900, 1000, 1100, 1110])
        XCTAssertEqual(sc.pieces.map(\.fill), [.sleep, .place(RGB(196, 204, 238), kept: false), .blank, .place(RGB(196, 204, 238), kept: false), .trip, .blank])
        XCTAssertTrue(sc.ahead.isEmpty && sc.pieces.allSatisfy { $0.fill != .ahead }, "another day has no after")
        // one stay that the night cuts in two is two pieces; one the night covers is none of its own
        let cut = try reply(#"{"id": "s0", "lane": "you", "kind": "stay", "state": "done", "start_ms": \#(at(400)), "end_ms": \#(at(900)), "place": "Home"}, {"id": "n", "lane": "you", "kind": "sleep", "state": "done", "start_ms": \#(at(500)), "end_ms": \#(at(600))}, {"id": "x", "lane": "you", "kind": "stay", "state": "done", "start_ms": \#(at(520)), "end_ms": \#(at(580)), "place": "Hidden"}"#)
        XCTAssertEqual(build(cut, now: nil).scene.pieces.map(\.id), ["blank-360", "s0", "n", "s0", "blank-900"])
    }

    func testWhatIsPlannedIsDrawnOnTheBarAfterNowAndNothingThatIsOver() throws {
        let sc = build(TimelineFixture.today(cal)).scene
        XCTAssertEqual(sc.ahead.map(\.id), ["event-demo-1", "reminder-demo-1", "departure-demo-1"])
        XCTAssertEqual(sc.ahead.map(\.shape), [.box, .tick, .tick])
        XCTAssertEqual([sc.ahead[0].t0, sc.ahead[0].t1, sc.ahead[1].t0], [1320, 1350, 1380])
        XCTAssertEqual([sc.ahead[0].name, sc.ahead[0].icon], ["陶艺课", "calendar"], "an event wears its title in its box")
        let r = try reply(#"{"id": "over", "lane": "you", "kind": "event", "state": "done", "start_ms": \#(at(1200)), "end_ms": \#(at(1250)), "title": "A"}, {"id": "on", "lane": "you", "kind": "event", "state": "now", "start_ms": \#(at(1290)), "end_ms": \#(at(1330)), "title": "B"}, {"id": "open", "lane": "you", "kind": "event", "state": "next", "start_ms": \#(at(1400)), "end_ms": null, "title": "C"}, {"id": "rang", "lane": "her", "kind": "reminder", "state": "done", "start_ms": \#(at(1300)), "fired": true}, {"id": "next", "lane": "her", "kind": "reminder", "state": "next", "start_ms": \#(at(1320))}"#)
        let a = build(r).scene.ahead
        XCTAssertEqual(a.map(\.id), ["on", "next", "open"], "an event that is on is still ahead of its end; one that is over, and a reminder that has rung, are not")
        XCTAssertEqual(a.map(\.shape), [.box, .tick, .tick], "an event with no end is a moment")
        XCTAssertTrue(build(r, now: nil).scene.ahead.isEmpty)
    }

    func testWhatMatteredIsAStarAtItsTimeOnTheBarEvenForAStayAndOnlyOnce() throws {
        XCTAssertEqual(build(TimelineFixture.today(cal)).scene.stars, [TLStar(id: "call-demo-1", t: 1000)])
        let r = try reply(#"{"id": "s1", "lane": "you", "kind": "stay", "state": "done", "start_ms": \#(at(700)), "end_ms": \#(at(800)), "place": "Cafe"}, {"id": "t", "lane": "her", "kind": "talk", "state": "done", "start_ms": \#(at(600)), "end_ms": \#(at(610)), "turns": 2}"#,
                          #", "highlights": [{"id": "s1", "label": "Coffee", "by": "him"}, {"id": "t", "label": "x", "by": "her"}, {"id": "s1", "label": "again", "by": "her"}, {"id": "gone", "label": "y", "by": "her"}]"#)
        XCTAssertEqual(build(r, now: nil).scene.stars, [TLStar(id: "t", t: 600), TLStar(id: "s1", t: 700)], "in the order of the day, the item that is not there left out")
    }

    func testTheGapBetweenPiecesIsAHairAndAPieceIsNeverNarrowerThanTwoPoints() {
        let pieces = [TLPiece(id: "a", t0: 360, t1: 720, fill: .sleep), TLPiece(id: "b", t0: 720, t1: 721, fill: .trip), TLPiece(id: "c", t0: 721, t1: DAY, fill: .blank)]
        let r = TLGeo.rects(pieces, span: 360...DAY, width: 1080, height: 22)
        XCTAssertEqual([r[0].minX, r[0].maxX], [0, 359.5], "the first begins where the bar does and leaves half a point to the next")
        XCTAssertEqual(r[1].width, 2, "a minute of a 1080-point bar is a point; it is given two")
        XCTAssertEqual(r[2].maxX, 1080, "the last ends where the bar ends")
        XCTAssertEqual(r.map(\.height), [22, 22, 22])
        XCTAssertEqual(TLGeo.x(DAY + 100, 360...DAY, 380), 380, "nothing is drawn past the bar")
    }

    // MARK: what a piece says

    /// six points a Latin letter and eleven a Chinese character: nothing here depends on the font of the machine that runs the test
    private func measure(_ s: String) -> Double { s.unicodeScalars.reduce(0) { $0 + ($1.value > 0x2E80 ? 11.0 : 6.0) } }

    func testAPieceWearsTheRichestOfIconAndNameNameIconNothingThatFits() {
        func fit(_ name: String?, _ icon: Bool, _ w: Double) -> TLBarLabel.Fit { TLBarLabel.fit(name: name, icon: icon, width: w, measure: measure) }
        // UVic is 24 wide: icon (12) + 4 + 24 + 6 each side is 52, the name alone 36, the icon alone 24
        XCTAssertEqual(fit("UVic", true, 60), .iconName)
        XCTAssertEqual(fit("UVic", true, 52), .iconName)
        XCTAssertEqual(fit("UVic", true, 51.9), .name)
        XCTAssertEqual(fit("UVic", true, 36), .name)
        XCTAssertEqual(fit("UVic", true, 35.9), .icon)
        XCTAssertEqual(fit("UVic", true, 24), .icon)
        XCTAssertEqual(fit("UVic", true, 23.9), .none)
        XCTAssertEqual(fit("UVic", false, 60), .name, "no symbol: the word")
        XCTAssertEqual(fit("UVic", false, 30), .none)
        XCTAssertEqual(fit(nil, true, 30), .icon, "no word: the symbol")
        XCTAssertEqual(fit(nil, true, 20), .none)
        XCTAssertEqual(fit(nil, false, 400), .none)
        XCTAssertEqual(fit("开车", true, 50), .iconName)
        XCTAssertEqual(fit("开车", true, 49), .name)
    }

    func testTheMadeUpDaysPiecesWearWhatFitsThemAtTheBarsWidth() throws {
        let sc = build(TimelineFixture.today(cal)).scene
        let words = Dictionary(uniqueKeysWithValues: TLBarLabel.placed(sc, width: TLLayout.barW, measure: measure).map { ($0.id, $0) })
        XCTAssertEqual(words["sleep-0"]?.fit, .iconName, "65 points for a moon and 8:05")
        XCTAssertEqual(words["stay-1"]?.fit, .iconName)
        XCTAssertEqual(words["stay-9"]?.fit, .iconName, "UVic")
        XCTAssertEqual(words["stay-13"]?.fit, .iconName)
        for id in ["move-4", "stay-5", "move-6", "ride-8", "ride-12", "event-demo-1"] { XCTAssertNil(words[id], "\(id) is too narrow to say anything") }
        XCTAssertEqual(words["sleep-0"]?.x, TLBarLabel.pad, "the word begins 6 points inside its piece")
        XCTAssertEqual(words["sleep-0"]?.dark, false, "light text on the night's indigo")
        XCTAssertEqual(words["stay-1"]?.dark, true, "dark ink on the pale hue of home")
        XCTAssertEqual(words["stay-9"]?.dark, true)
    }

    func testTextIsDarkOnALightFillAndLightOnADarkOne() {
        for hue in [RGB(255, 168, 150), RGB(100, 200, 255), RGB(240, 132, 218), RGB(132, 152, 255), RGB(172, 124, 255), RGB(111, 224, 180)] {
            XCTAssertTrue(TLPaint.textIsDark(.place(hue, kept: true)), "\(hue)")
        }
        XCTAssertFalse(TLPaint.textIsDark(.place(RGB(196, 204, 238), kept: false)), "a place he does not keep is a faint haze")
        XCTAssertFalse(TLPaint.textIsDark(.place(RGB(255, 168, 150), kept: false)), "a faint fill is dark, whatever its hue")
        XCTAssertFalse(TLPaint.textIsDark(.sleep))
        XCTAssertFalse(TLPaint.textIsDark(.trip))
        XCTAssertFalse(TLPaint.textIsDark(.blank))
        XCTAssertGreaterThan(TLPaint.luminance(RGB(255, 168, 150), alpha: 0.9), TLPaint.luminance(RGB(112, 120, 205), alpha: 0.9))
    }

    func testTheHoursAreTickedUnderTheScene() {
        XCTAssertEqual(build(TimelineFixture.today(cal)).scene.ticks, [6, 9, 12, 15, 18, 21, 24])
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
        XCTAssertEqual(m.entry(at: 1439)?.id, "departure-demo-1")
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

    func testPaymentsAndClaudeChatsAreReadAndOneThatCannotBeReadCostsOnlyItself() throws {
        let r = try reply(#"{"id": "payment-1", "lane": "you", "kind": "payment", "state": "done", "start_ms": 1, "end_ms": 1, "source": "alipay", "amount": "12.40", "currency": "CAD", "direction": "out", "counterparty": "A&W", "text": "Burger"}, {"id": "payment-2", "lane": "you", "kind": "payment", "state": "done", "start_ms": 1, "end_ms": 1, "amount": 7.5, "currency": 5, "direction": ["x"]}, {"id": "payment-3", "lane": "you", "kind": "payment", "state": "done", "start_ms": 1, "end_ms": 1, "amount": "twelve"}, {"id": "claude-1", "lane": "you", "kind": "claude_chat", "state": "done", "start_ms": 1, "end_ms": 2, "source": "claude", "text": "Limits", "messages": 14}, {"id": "claude-day-1", "lane": "you", "kind": "claude_day", "state": "done", "start_ms": 1, "end_ms": 2, "source": "claude_daily", "text": "a; b", "chats": "three"}"#)
        XCTAssertEqual(r.items.count, 5, "no item is lost")
        let p = r.items[0]
        XCTAssertEqual(p.amount, Amount(cents: 1240))
        XCTAssertEqual([p.currency, p.direction, p.counterparty, p.text, p.source], ["CAD", "out", "A&W", "Burger", "alipay"])
        XCTAssertEqual(r.items[1].amount, Amount(cents: 750), "a number is read too")
        XCTAssertNil(r.items[1].currency)
        XCTAssertNil(r.items[1].direction)
        XCTAssertNil(r.items[2].amount, "an amount that is no number is no amount")
        XCTAssertEqual(r.items[3].messages, 14)
        XCTAssertNil(r.items[4].chats)
        XCTAssertEqual(r.items[4].text, "a; b")
        XCTAssertNil(try reply(#"{"id": "d", "lane": "you", "kind": "stay", "state": "done", "start_ms": 1}"#).items[0].amount, "an old brain has none of them")
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
        XCTAssertEqual(try entry(m, "picture-pic-demo-1").about, About(kind: "picture", id: "picture-pic-demo-1", title: "照片", startMs: at(962), mark: .lit), "the host takes a picture too")
    }

    func testAPrivateEventGivesTheChipNoMoreThanTheRowSays() throws {
        let r = try reply(#"{"id": "event-p", "lane": "you", "kind": "event", "state": "next", "start_ms": \#(at(1400)), "end_ms": \#(at(1430)), "title": "Counselling", "private": true, "place": "Clinic"}"#)
        XCTAssertEqual(build(r).entries.first { $0.id == "event-p" }?.about?.title, "私事")
    }

    func testARowOfAKindTheHostDoesNotKnowOrWithAnIdItWouldRefuseGivesNoChip() throws {
        let m = build(TimelineFixture.today(cal))
        for id in ["ride-8", "now"] { XCTAssertNil(try entry(m, id).about, id) }
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
