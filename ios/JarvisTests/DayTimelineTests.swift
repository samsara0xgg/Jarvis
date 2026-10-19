import XCTest
@testable import Jarvis

/// The day as one list: what is a group, what is a trip, what is a row, where the line for now stands, and the words on each.
final class DayTimelineTests: XCTestCase {
    private var cal: Cal { Cal.cur }
    private var base: Int64 { Int64(cal.origin.timeIntervalSince1970 * 1000) }
    private func at(_ minute: Double) -> Int64 { base + Int64(minute * 60_000) }
    private let now = TimelineFixture.now

    private func reply(_ items: String, _ extra: String = "") throws -> DayReply {
        let day = #"{"date": "2026-10-08", "tz": "\#(Cal.cur.cal.timeZone.identifier)", "start_ms": \#(base), "end_ms": \#(base + 86_400_000), "now_ms": \#(at(now)), "missing": [], "items": [\#(items)]\#(extra)}"#
        return try JSONDecoder().decode(DayReply.self, from: Data(day.utf8))
    }
    private func build(_ r: DayReply, now: Double? = TimelineFixture.now) -> DayTimelineModel {
        DayTimelineModel.build(r, now: now, topic: { $0 == "talk-1" ? "明天下午有什么安排？" : nil })
    }
    private func entry(_ m: DayTimelineModel, _ id: String) throws -> TLEntry { try XCTUnwrap(m.entries.first { $0.id == id }, id) }

    // MARK: the day in order

    func testTheFixtureDayIsSleepGroupsAndTripsInTheOrderOfTheDayWithNowBetween() {
        let m = build(TimelineFixture.today(cal))
        let kinds = m.entries.map(\.kind)
        XCTAssertEqual(kinds, [.sleep, .stay, .reminder, .code, .talk, .work, .trip, .trip, .stay, .payment, .trip, .stay, .trip, .stay, .event, .picture, .call, .workout, .trip, .stay,
                               .work, .code, .payment, .claude, .talk, .reminder, .now, .event, .reminder, .departure])
        XCTAssertEqual(m.entries.map(\.title).prefix(5), ["睡觉", "家", "带伞", "Claude Code · 3 个会话", "明天下午有什么安排？"])
        let t = m.entries.map(\.t0)
        XCTAssertEqual(t, t.sorted(), "the list runs forward in time")
        XCTAssertEqual(m.nowIndex, 26)
        XCTAssertEqual(m.entries[26].title, "现在 21:50")
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
        XCTAssertEqual([drive.title, walk.title], ["开车", "步行"], "the Mac's word for it")
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
        XCTAssertEqual(ride.base, TLMetrics.rowDetail, "the stops have a line of their own, so they are not cut off")
        XCTAssertEqual(ride.symbol, "bus.fill")
        XCTAssertEqual(ride.right, "29 分")
        XCTAssertEqual(ride.more, ["实时：BC Transit", "下车了"], "that the bus was followed live and how it ended, when it is opened: the stops are in its line")
        XCTAssertEqual(try entry(m, "ride-12").more.last, "过了要下的站，就算到了")
        // a ride whose other stop is not known yet gives the one it knows; one that knows no stop has no second line
        let one = try reply(#"{"id": "r", "lane": "you", "kind": "ride", "state": "now", "start_ms": \#(at(1280)), "end_ms": null, "route": "12", "board_stop": "Stop A"}"#)
        let a = build(one).entries[0]
        XCTAssertEqual([a.title, a.detail], ["12 路", "Stop A"])
        XCTAssertEqual(a.base, TLMetrics.rowDetail)
        XCTAssertEqual(a.more, ["上车 Stop A"])
        let none = try reply(#"{"id": "r", "lane": "you", "kind": "ride", "state": "now", "start_ms": \#(at(1280)), "end_ms": null, "route": "12"}, {"id": "q", "lane": "you", "kind": "ride", "state": "done", "start_ms": \#(at(100)), "end_ms": \#(at(120))}"#)
        let b = build(none).entries.filter { $0.kind == .trip }
        XCTAssertEqual(b.map(\.title), ["公交", "12 路"], "no route is just a bus")
        XCTAssertEqual(b.map(\.detail), [nil, nil])
        XCTAssertEqual(b.map(\.base), [TLMetrics.row, TLMetrics.row], "no second line, no taller row")
        XCTAssertTrue(b.allSatisfy { $0.more.isEmpty && $0.actions == [.ask, .mark] }, "nothing to say, but she can be asked about it and it can be starred")
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
        XCTAssertEqual(p.more, ["第三题的符号有些看不清。"], "its line is the first line of the note, so opened it says the rest")
        XCTAssertEqual(p.attachment, "pic-demo-1")
        // no note yet: a picture and nothing more
        let r = try reply(#"{"id": "picture-x", "lane": "you", "kind": "picture", "state": "done", "start_ms": \#(at(600)), "end_ms": \#(at(600)), "attachment": "x"}"#)
        XCTAssertNil(build(r).entries[0].detail)
    }

    func testAnEventFromMailSaysSoAndWhereAndAPrivateOneIsOnlyPrivate() throws {
        let m = build(TimelineFixture.today(cal))
        let e = try entry(m, "event-demo-1")
        XCTAssertEqual(e.title, "陶艺课")
        XCTAssertEqual(e.detail, "邮件 · 还没确认 · Studio 4", "where it came from, that it is not his yet, and where: the Mac's words")
        XCTAssertEqual(e.right, "还有 10 分", "within two hours it says how long until it")
        XCTAssertTrue(e.later)
        XCTAssertEqual(try entry(m, "event-demo-2").detail, "日历 · ECS 123", "no source is his calendar")
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
        XCTAssertEqual(build(rang, now: nil).entries.map(\.detail), ["你知道了", "响过了"])
        XCTAssertTrue(r.opens, "a reminder opens to its words in full")
    }

    func testABusDepartureNamesTheRouteAndTheStop() throws {
        let r = try reply(#"{"id": "departure-1", "lane": "you", "kind": "departure", "state": "next", "start_ms": \#(at(1400)), "end_ms": \#(at(1400)), "title": "12 · Stop A", "route": "12", "board_stop": "Stop A", "to": "Downtown", "leave_at_ms": \#(at(1390))}"#)
        let d = build(r, now: nil).entries[0]
        XCTAssertEqual(d.title, "12 · Stop A", "the brain's own title for it")
        XCTAssertEqual([d.detail, d.right], ["去 Downtown", "23:10 出门"], "where it goes under its name, and when to leave where a length would be")
        XCTAssertTrue(d.strongRight)
        XCTAssertEqual(d.more, ["路线 12", "在 Stop A 上车"], "where to and when to leave are its line and its value")
        XCTAssertEqual(d.time, "23:20")
    }

    func testMacWorkIsNamedForTheTopProjectAndCountsTheActiveTime() throws {
        let r = try reply(#"{"id": "w1", "lane": "you", "kind": "work", "state": "done", "start_ms": \#(at(665)), "end_ms": \#(at(798)), "active_s": 6780, "projects": [{"name": "Jarvis", "seconds": 6780}], "apps": [{"name": "Xcode", "seconds": 2400}]}, {"id": "w2", "lane": "you", "kind": "work", "state": "done", "start_ms": \#(at(900)), "end_ms": \#(at(930))}"#)
        let m = build(r)
        XCTAssertEqual(m.entries[0].title, "在 Mac 上工作")
        XCTAssertEqual(m.entries[0].detail, "Jarvis · Xcode", "the top project and the top app")
        XCTAssertEqual(m.entries[0].right, "活跃 1 时 53 分", "the time he was at it")
        XCTAssertEqual(m.entries[0].more, ["活跃 1 时 53 分 · 共 2 时 13 分"], "and the stretch it spans, when it is opened")
        XCTAssertEqual(m.entries[0].work?.projects, [TLTop(name: "Jarvis", minutes: 113, share: 1)])
        XCTAssertEqual(m.entries[0].work?.apps, [TLTop(name: "Xcode", minutes: 40, share: 1)])
        XCTAssertEqual(m.entries[1].title, "在 Mac 上工作")
        XCTAssertNil(m.entries[1].detail)
        XCTAssertEqual(m.entries[1].right, "30 分", "no active time: the stretch")
    }

    func testAPaymentIsNamedForWhoGotTheMoneySaysWhatItWasForAndShowsWhatItCameTo() throws {
        let m = build(TimelineFixture.today(cal))
        let p = try entry(m, "payment-demo-1")
        XCTAssertEqual([p.title, p.detail, p.right, p.time], ["A&W", "Teen Burger Combo · 支付宝", "$12.40", "13:20"], "who, what, and through which app: the Mac's line")
        XCTAssertEqual(p.more, ["支出 $12.40 CAD"], "which way and in what money; who, what and through which app are its line")
        XCTAssertEqual(p.symbol, "creditcard.fill")
        XCTAssertTrue(p.strongRight && p.opens)
        XCTAssertEqual(p.about?.kind, "payment", "the host takes a payment as what a turn is about")
        // money in has a plus; no shop leaves what it was for as the title; a source with no word here is said as it came; no amount is no amount
        let r = try reply(#"{"id": "pay-2", "lane": "you", "kind": "payment", "state": "done", "start_ms": \#(at(600)), "end_ms": \#(at(600)), "source": "wechat_pay", "amount": "1234.50", "currency": "CNY", "direction": "in", "text": "转账"}, {"id": "pay-3", "lane": "you", "kind": "payment", "state": "done", "start_ms": \#(at(700)), "end_ms": \#(at(700)), "source": "venmo", "amount": "5.00", "currency": "AUD", "direction": "out", "counterparty": "Cafe"}, {"id": "pay-4", "lane": "you", "kind": "payment", "state": "done", "start_ms": \#(at(800)), "end_ms": \#(at(800))}"#)
        let rows = build(r, now: nil).entries
        XCTAssertEqual(rows.map(\.title), ["转账", "Cafe", "付款"])
        XCTAssertEqual(rows.map(\.right), ["+¥1,234.50", "5.00 AUD", ""])
        XCTAssertEqual(rows.map(\.more), [["收入 ¥1,234.50 CNY"], ["支出 5.00 AUD", "来自venmo"], []], "the source is said opened only where the line has no word for it")
        XCTAssertEqual(rows.map(\.detail), ["微信", nil, nil], "an app the Mac has a word for is said; one it has not is only in the opened row")
    }

    func testMoneyOutIsPlainWhateverSignTheBrainGaveItAndMoneyInHasAPlus() throws {
        func pay(_ id: String, _ amount: String, _ way: String) -> String {
            #"{"id": "\#(id)", "lane": "you", "kind": "payment", "state": "done", "start_ms": \#(at(600)), "end_ms": \#(at(600)), "amount": "\#(amount)", "currency": "USD", "direction": "\#(way)"}"#
        }
        let r = try reply([pay("a", "12.40", "out"), pay("b", "-3.00", "out"), pay("c", "50.00", "in"), pay("d", "-8.00", "in")].joined(separator: ","))
        XCTAssertEqual(build(r, now: nil).entries.map(\.right), ["US$12.40", "US$3.00", "+US$50.00", "+US$8.00"])
    }

    func testMoneyIsWrittenWithTheMarkOfItsCurrency() {
        XCTAssertEqual(tlMoney(1240, "CAD"), "$12.40")
        XCTAssertEqual(tlMoney(1240, "usd"), "US$12.40", "the Mac writes US dollars apart from Canadian ones")
        XCTAssertEqual(tlMoney(1240, "JPY"), "JP¥12.40")
        XCTAssertEqual(tlMoney(123450, "CNY"), "¥1,234.50")
        XCTAssertEqual(tlMoney(5, "EUR"), "€0.05")
        XCTAssertEqual(tlMoney(500, "AUD"), "5.00 AUD", "a currency with no mark of its own is written after the number")
        XCTAssertEqual(tlMoney(500, nil), "5.00")
        XCTAssertEqual(tlMoney(-1240, "CAD"), "-$12.40")
        XCTAssertEqual(tlMoney(100_000_000, "CAD"), "$1,000,000.00")
    }

    func testAClaudeChatIsNamedForTheConversationAndCountsItsMessagesAndADayListsItsChats() throws {
        let m = build(TimelineFixture.today(cal))
        let c = try entry(m, "claude-demo-1")
        XCTAssertEqual([c.title, c.detail, c.right, c.time], ["积分换元的符号问题", "和 Claude", "14 条", "20:10"], "the Mac's: with whom, and how many messages where a length would be")
        XCTAssertEqual(c.symbol, "sparkles")
        XCTAssertEqual(c.about?.kind, "claude_chat")
        let r = try reply(#"{"id": "claude-x-1", "lane": "you", "kind": "claude_chat", "state": "done", "start_ms": \#(at(600)), "end_ms": \#(at(620)), "text": "Claude conversation", "messages": 3}, {"id": "claude-day-2026-10-08", "lane": "you", "kind": "claude_day", "state": "done", "start_ms": \#(at(700)), "end_ms": \#(at(800)), "source": "claude_daily", "text": "微积分; 论文提纲 ;", "chats": 2}"#)
        let rows = build(r, now: nil).entries
        XCTAssertEqual(rows.map(\.title), ["Claude 对话", "Claude 日报"], "a conversation with no name is only one")
        XCTAssertEqual(rows[1].detail, "2 个对话 · 微积分 · 论文提纲")
        XCTAssertEqual(rows[1].more, ["微积分", "论文提纲"], "its titles as a list when it is opened")
        XCTAssertEqual(rows[1].right, "", "a letter about the day has no length")
        XCTAssertTrue(rows[1].opens)
    }

    // MARK: the day's numbers

    private func chips(_ m: DayTimelineModel) -> [String] { m.chips.map { $0.word + " " + $0.value } }

    func testTheDaysNumbersAreChipsInAFixedOrderAndOnlyTheOnesThatAreNotNothing() throws {
        let m = build(TimelineFixture.today(cal))
        XCTAssertEqual(chips(m), ["睡 8:05", "路上 3:04", "运动 35 分", "Mac 2:20", "通话 18 分", "和她 2 次", "花 $12.40", "照片 1"])
        XCTAssertEqual(m.chips.map(\.lens), TLLens.allCases)
        XCTAssertEqual(m.chips[3].value, "2:20", "the Mac is the time he was at it (42 and 98 minutes), not the 2:55 the blocks span, and not the sessions of Claude Code in it")
        // a call of 35 minutes is minutes; a day of places alone has no numbers at all
        let r = try reply(#"{"id": "c1", "lane": "you", "kind": "call", "state": "done", "start_ms": \#(at(600)), "end_ms": \#(at(635)), "app": "FaceTime"}, {"id": "s1", "lane": "you", "kind": "stay", "state": "done", "start_ms": \#(at(700)), "end_ms": \#(at(800))}"#)
        XCTAssertEqual(chips(build(r)), ["通话 35 分"])
        XCTAssertTrue(build(try reply(#"{"id": "s1", "lane": "you", "kind": "stay", "state": "done", "start_ms": \#(at(700)), "end_ms": \#(at(800))}"#)).chips.isEmpty)
        XCTAssertTrue(build(try reply("")).chips.isEmpty)
    }

    func testTheTimeChipsCountWhatIsStillGoingUpToNowAndTheNightAsHeSleptIt() throws {
        // the night is the minutes the brain says he slept, else its span; a ride that has begun and not ended is counted to now (21:50)
        let r = try reply(#"{"id": "r", "lane": "you", "kind": "ride", "state": "now", "start_ms": \#(at(1290)), "end_ms": null, "route": "12"}, {"id": "n", "lane": "you", "kind": "sleep", "state": "done", "start_ms": \#(at(-30)), "end_ms": \#(at(420)), "minutes": 400}, {"id": "n2", "lane": "you", "kind": "sleep", "state": "done", "start_ms": \#(at(900)), "end_ms": \#(at(930))}"#)
        XCTAssertEqual(chips(build(r)), ["睡 7:10", "路上 20 分"])
        XCTAssertEqual(chips(build(r, now: nil)), ["睡 7:10"], "another day: the ride has no end and no length")
    }

    func testSpendingIsMoneyOutInTheCurrencyHePaidInMostAndAnotherCurrencyAddsAnEllipsis() throws {
        func pay(_ id: String, _ amount: String?, _ cur: String, _ way: String = "out") -> String {
            #"{"id": "\#(id)", "lane": "you", "kind": "payment", "state": "done", "start_ms": \#(at(600)), "end_ms": \#(at(600)), "currency": "\#(cur)", "direction": "\#(way)"\#(amount.map { #", "amount": "\#($0)""# } ?? "")}"#
        }
        func spend(_ items: String...) throws -> String? { build(try reply(items.joined(separator: ","))).chips.first { $0.lens == .payment }?.value }
        XCTAssertEqual(try spend(pay("a", "12.40", "CAD"), pay("b", "0.10", "CAD")), "$12.50")
        XCTAssertEqual(try spend(pay("a", "12.40", "CAD"), pay("b", "7.60", "CAD"), pay("c", "68.00", "CNY"), pay("d", "500.00", "CAD", "in")), "$20.00…", "two in CAD against one in CNY; money in is not spending")
        XCTAssertEqual(try spend(pay("a", "5.00", "CAD"), pay("b", "68.00", "CNY")), "¥68.00…", "a tie in how often goes to the larger sum")
        XCTAssertEqual(try spend(pay("a", "1234.50", "CNY")), "¥1,234.50")
        XCTAssertNil(try spend(pay("a", "5.00", "CAD", "in")), "money in only is no spending")
        XCTAssertNil(try spend(pay("a", nil, "CAD")), "a payment with no amount is not added")
        XCTAssertEqual(try spend(pay("a", "3.00", "")), "3.00", "no currency, no mark")
    }

    func testEachLensHoldsTheRowsOfItsKindAndMarksWhereTheyAreOnTheBar() throws {
        let m = build(TimelineFixture.today(cal))
        func ids(_ l: TLLens) -> [String] { m.entries.filter { $0.lens == l }.map(\.id) }
        XCTAssertEqual(ids(.sleep), ["sleep-0"])
        XCTAssertEqual(ids(.trip), ["move-3", "move-4", "move-6", "ride-8", "ride-12"])
        XCTAssertEqual(ids(.work), ["code-group-stay-1", "work-demo-1", "work-demo-2", "code-group-stay-13"], "Claude Code is lit with the Mac's work")
        XCTAssertEqual(ids(.workout), ["workout-demo-1"])
        XCTAssertEqual(ids(.call), ["call-demo-1"])
        XCTAssertEqual(ids(.talk), ["talk-1", "talk-2"])
        XCTAssertEqual(ids(.payment), ["payment-demo-1"])
        XCTAssertEqual(ids(.picture), ["picture-pic-demo-1"])
        XCTAssertTrue(m.entries.filter { [.stay, .pause, .now, .event, .reminder, .departure, .claude].contains($0.kind) }.allSatisfy { $0.lens == nil }, "a place, the line for now, what is planned and a chat with Claude are in no lens")
        XCTAssertEqual(m.marks(of: .work), [TLMark(t0: 570, t1: 605), TLMark(t0: 612, t1: 650), TLMark(t0: 656, t1: 680), TLMark(t0: 630, t1: 680), TLMark(t0: 1175, t1: 1300),
                                           TLMark(t0: 1180, t1: 1218), TLMark(t0: 1235, t1: 1268), TLMark(t0: 1276, t1: 1306)], "a row that stands for several sessions marks each of them")
        XCTAssertEqual(m.marks(of: .workout), [TLMark(t0: 1020, t1: 1055)])
        XCTAssertEqual(m.marks(of: .picture), [TLMark(t0: 962, t1: 962)], "a moment: its end is its start")
        XCTAssertEqual(m.marks(of: .payment), [TLMark(t0: 800, t1: 800)])
        XCTAssertEqual(m.marks(of: .trip).count, 5)
        // money in is not in the lens that counts what he spent
        let r = try reply(#"{"id": "in", "lane": "you", "kind": "payment", "state": "done", "start_ms": \#(at(600)), "end_ms": \#(at(600)), "amount": "5.00", "currency": "CAD", "direction": "in"}"#)
        XCTAssertNil(build(r).entries[0].lens)
        XCTAssertEqual(build(r).entries[0].right, "+$5.00")
    }

    func testWhatMatteredIsNamedByItsLabelAndLit() throws {
        let m = build(TimelineFixture.today(cal))
        let c = try entry(m, "call-demo-1")
        XCTAssertEqual(c.title, "和 Mia 聊了很久")
        XCTAssertTrue(c.lit)
        XCTAssertEqual(c.detail, "通话 · FaceTime", "what the row was called before it was marked, in the muted line")
        XCTAssertFalse(try entry(m, "talk-1").lit)
        // a place stays its own name even when marked, and wears the star
        let r = try reply(#"{"id": "s1", "lane": "you", "kind": "stay", "state": "done", "start_ms": \#(at(600)), "end_ms": \#(at(700)), "place": "Cafe, Main"}"#, #", "highlights": [{"id": "s1", "label": "Coffee", "by": "him"}]"#)
        XCTAssertEqual(build(r).entries[0].title, "Cafe")
        XCTAssertTrue(build(r).entries[0].lit)
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
        XCTAssertEqual(try entry(m, "stay-1").base, TLMetrics.stay)
        XCTAssertEqual(try entry(m, "stay-1").height, TLMetrics.stay + TLMetrics.gap, "a place has the blank above it, between it and the night")
        XCTAssertEqual(try entry(m, "move-3").base, TLMetrics.row)
        XCTAssertEqual(try entry(m, "move-3").height, TLMetrics.row + TLMetrics.gap, "the first leg of a run has the blank above it")
        XCTAssertEqual(try entry(m, "move-4").height, TLMetrics.row, "the next leg of the run does not")
        XCTAssertEqual(try entry(m, "ride-8").base, TLMetrics.rowDetail, "a ride has its stops under it")
        XCTAssertEqual(try entry(m, "talk-1").height, TLMetrics.rowDetail, "a row with a muted line is taller, and one in a group has no blank above it")
        XCTAssertEqual(try entry(m, "call-demo-1").height, TLMetrics.rowDetail)
        XCTAssertEqual(try entry(m, "now").height, TLMetrics.now)
        XCTAssertEqual(m.top(of: "talk-1"), m.offsets[4])
        XCTAssertTrue(m.entries.allSatisfy { $0.base >= 44 || $0.kind == .now }, "nothing he can touch is lower than 44")
        XCTAssertNil(m.top(of: "nothing"))
    }

    // MARK: the overview bar

    func testTheBarStartsAtTheHourBeforeHeWokeAndNeverBeforeSix() throws {
        // the later of 06:00 and the hour before he woke: the waking day gets the width
        XCTAssertEqual(build(TimelineFixture.today(cal)).span, 480...DAY, "the night ended at 09:05")
        XCTAssertEqual(DayTimelineModel.axisStart(wake: 545, early: []), 480, "woke 09:05: the hour before is 08:05, which is 08:00")
        XCTAssertEqual(DayTimelineModel.axisStart(wake: 480, early: []), 420, "08:00: 07:00")
        XCTAssertEqual(DayTimelineModel.axisStart(wake: 421, early: []), 360, "07:01: the hour before is 06:01, which is 06:00")
        XCTAssertEqual(DayTimelineModel.axisStart(wake: 780, early: []), 720, "13:00: 12:00")
        XCTAssertEqual(DayTimelineModel.axisStart(wake: 310, early: []), 360, "woke 05:10: the hour before is 04:00, but the bar never starts before 06:00")
        XCTAssertEqual(DayTimelineModel.axisStart(wake: 30, early: []), 360)
        XCTAssertEqual(DayTimelineModel.axisStart(wake: nil, early: []), 360, "no night that ended on this day: 06:00")
        let r = try reply(#"{"id": "n", "lane": "you", "kind": "sleep", "state": "done", "start_ms": \#(at(-60)), "end_ms": \#(at(545))}, {"id": "s", "lane": "you", "kind": "stay", "state": "done", "start_ms": \#(at(545)), "end_ms": \#(at(700)), "place": "Home"}"#)
        XCTAssertEqual(build(r).span, 480...DAY)
        let early = try reply(#"{"id": "n", "lane": "you", "kind": "sleep", "state": "done", "start_ms": \#(at(-60)), "end_ms": \#(at(320))}"#)
        XCTAssertEqual(build(early).span, 360...DAY, "woke 05:20")
        XCTAssertEqual(build(try reply("")).span, 360...DAY)
        // a night still going, or one that ends tomorrow, tells nothing
        let on = try reply(#"{"id": "n", "lane": "you", "kind": "sleep", "state": "now", "start_ms": \#(at(-60)), "end_ms": null}, {"id": "t", "lane": "you", "kind": "sleep", "state": "next", "start_ms": \#(at(1380)), "end_ms": \#(at(1900))}"#)
        XCTAssertEqual(build(on).span, 360...DAY)
        // the evening before is cut at the day's start
        let before = try reply(#"{"id": "s1", "lane": "you", "kind": "sleep", "state": "done", "start_ms": \#(at(-30)), "end_ms": \#(at(20))}"#)
        XCTAssertEqual(build(before).span, 360...DAY)
        XCTAssertEqual(build(before).segs[0].t0, 0)
    }

    func testTodayTheBarStartsNoLaterThanTheHourNowIsInSoTheLineForNowIsOnIt() throws {
        XCTAssertEqual(DayTimelineModel.axisStart(wake: nil, early: [], now: 150), 120, "02:30: the bar starts at 02:00")
        XCTAssertEqual(DayTimelineModel.axisStart(wake: nil, early: [], now: 30), 0)
        XCTAssertEqual(DayTimelineModel.axisStart(wake: nil, early: [], now: 360), 360, "06:00 on the dot")
        XCTAssertEqual(DayTimelineModel.axisStart(wake: nil, early: [], now: 400), 360, "later than 06:00 changes nothing")
        XCTAssertEqual(DayTimelineModel.axisStart(wake: 545, early: [], now: 1310), 480, "nor does a night that ended earlier")
        XCTAssertEqual(DayTimelineModel.axisStart(wake: 545, early: [130], now: 150), 120, "what began earlier wins when it is earlier than the hour")
        XCTAssertEqual(DayTimelineModel.axisStart(wake: nil, early: []), 360, "another day has no now")
        let night = try reply(#"{"id": "n", "lane": "you", "kind": "sleep", "state": "done", "start_ms": \#(at(-60)), "end_ms": \#(at(100))}"#)
        XCTAssertEqual(build(night, now: 150).span, 120...DAY, "woke 01:40 and it is 02:30: 06:00 is after now, so the hour now is in")
        XCTAssertEqual(build(try reply(""), now: 150).span, 120...DAY)
        XCTAssertEqual(build(try reply(""), now: nil).span, 360...DAY, "not today")
    }

    func testSomethingThatBeganBeforeTheStartBringsItBackToItsHourButAStayAloneDoesNot() throws {
        XCTAssertEqual(DayTimelineModel.axisStart(wake: 480, early: [280]), 240, "04:40 is in the hour of 04:00")
        XCTAssertEqual(DayTimelineModel.axisStart(wake: 480, early: [359]), 300)
        XCTAssertEqual(DayTimelineModel.axisStart(wake: 480, early: [130, 280]), 120, "the earliest of them")
        XCTAssertEqual(DayTimelineModel.axisStart(wake: 480, early: [-30, 420, 500]), 420, "what began the evening before, or at the start, or later, moves nothing")
        XCTAssertEqual(DayTimelineModel.axisStart(wake: 480, early: [-30, 400]), 360, "and 06:40 is in the hour of 06:00")
        func day(_ kind: String, _ extra: String = "") throws -> DayTimelineModel {
            build(try reply(#"{"id": "n", "lane": "you", "kind": "sleep", "state": "done", "start_ms": \#(at(-60)), "end_ms": \#(at(480))}, {"id": "x", "lane": "you", "kind": "\#(kind)", "state": "done", "start_ms": \#(at(280)), "end_ms": \#(at(300))\#(extra)}"#))
        }
        for kind in ["move", "ride", "talk", "picture", "work", "call", "workout", "code"] { XCTAssertEqual(try day(kind).span.lowerBound, 240, kind) }
        XCTAssertEqual(try day("event", #", "title": "x""#).span.lowerBound, 240)
        XCTAssertEqual(try day("payment", #", "amount": "1.00", "direction": "out""#).span.lowerBound, 240)
        for kind in ["stay", "reminder", "departure"] { XCTAssertEqual(try day(kind).span.lowerBound, 420, "\(kind) does not: the hour before he woke at 08:00") }
    }

    func testTheBarIsTickedEveryThreeHoursInsideItsSpan() throws {
        func ticks(_ lo: Double) -> [Int] { DayTimelineModel(date: "", entries: [], segs: [], span: lo...DAY).ticks }
        XCTAssertEqual(ticks(360), [6, 9, 12, 15, 18, 21, 24])
        XCTAssertEqual(ticks(240), [6, 9, 12, 15, 18, 21, 24], "03 is before the start")
        XCTAssertEqual(ticks(0), [0, 3, 6, 9, 12, 15, 18, 21, 24])
        XCTAssertEqual(ticks(181), [6, 9, 12, 15, 18, 21, 24])
        XCTAssertEqual(ticks(180), [3, 6, 9, 12, 15, 18, 21, 24], "a tick on the start is kept")
        XCTAssertEqual(build(TimelineFixture.today(cal)).ticks, [9, 12, 15, 18, 21, 24], "the bar begins at 08:00")
        XCTAssertEqual(build(TimelineFixture.yesterday(cal), now: nil).ticks, [6, 9, 12, 15, 18, 21, 24])
    }

    func testTheBarIsPiecesOfTheDayFromItsStartToMidnightEachWithItsFillAndTheWordItMayWear() throws {
        let sc = build(TimelineFixture.today(cal)).scene
        XCTAssertEqual(sc.pieces.first?.t0, 480)
        XCTAssertEqual(sc.pieces.last?.t1, DAY)
        XCTAssertEqual(zip(sc.pieces, sc.pieces.dropFirst()).filter { $0.t1 != $1.t0 }.count, 0, "one after the other, none missing, none over")
        func piece(_ id: String) throws -> TLPiece { try XCTUnwrap(sc.pieces.first { $0.id == id }, id) }
        let night = try piece("sleep-0")
        XCTAssertEqual([night.t0, night.t1], [480, 545], "the night's tail, from where the bar begins")
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
        XCTAssertEqual(sc.span, 360...DAY, "he woke at 06:40: the hour before is 05:00, but not before 06:00")
        XCTAssertEqual(sc.pieces.map(\.id), ["n", "s0", "blank-900", "s1", "m", "blank-1110"])
        XCTAssertEqual(sc.pieces.map(\.t0), [360, 400, 900, 1000, 1100, 1110], "the night is cut where the bar begins")
        XCTAssertEqual(sc.pieces.map(\.fill), [.sleep, .place(RGB(196, 204, 238), kept: false), .blank, .place(RGB(196, 204, 238), kept: false), .trip, .blank])
        XCTAssertTrue(sc.ahead.isEmpty && sc.pieces.allSatisfy { $0.fill != .ahead }, "another day has no after")
        // one stay that the night cuts in two is two pieces; one the night covers is none of its own
        let cut = try reply(#"{"id": "s0", "lane": "you", "kind": "stay", "state": "done", "start_ms": \#(at(560)), "end_ms": \#(at(900)), "place": "Home"}, {"id": "n", "lane": "you", "kind": "sleep", "state": "done", "start_ms": \#(at(570)), "end_ms": \#(at(600))}, {"id": "x", "lane": "you", "kind": "stay", "state": "done", "start_ms": \#(at(575)), "end_ms": \#(at(595)), "place": "Hidden"}"#)
        XCTAssertEqual(build(cut, now: nil).scene.span, 540...DAY, "woke at 10:00")
        XCTAssertEqual(build(cut, now: nil).scene.pieces.map(\.id), ["blank-540", "s0", "n", "s0", "blank-900"])
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
        XCTAssertEqual(build(TimelineFixture.today(cal)).scene.stars, [TLStar(id: "call-demo-1", t: 1000, hers: true)], "she picked it")
        let r = try reply(#"{"id": "s1", "lane": "you", "kind": "stay", "state": "done", "start_ms": \#(at(700)), "end_ms": \#(at(800)), "place": "Cafe"}, {"id": "t", "lane": "her", "kind": "talk", "state": "done", "start_ms": \#(at(600)), "end_ms": \#(at(610)), "turns": 2}"#,
                          #", "highlights": [{"id": "s1", "label": "Coffee", "by": "him"}, {"id": "t", "label": "x", "by": "her"}, {"id": "s1", "label": "again", "by": "her"}, {"id": "gone", "label": "y", "by": "her"}]"#)
        XCTAssertEqual(build(r, now: nil).scene.stars, [TLStar(id: "t", t: 600, hers: true), TLStar(id: "s1", t: 700)], "in the order of the day, the item that is not there left out; his mark first wins")
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
        let words = Dictionary(uniqueKeysWithValues: TLBarLabel.placed(sc, width: TLLayout.barW, measure: measure).map { ($0.item, $0) })
        XCTAssertEqual(words["sleep-0"]?.fit, .icon, "25 points: room for the moon, not for 8:05 too")
        XCTAssertEqual(words["stay-1"]?.fit, .iconName)
        XCTAssertEqual(words["stay-9"]?.fit, .iconName, "UVic")
        XCTAssertEqual(words["stay-13"]?.fit, .iconName)
        for id in ["move-4", "stay-5", "move-6", "ride-8", "ride-12", "event-demo-1"] { XCTAssertNil(words[id], "\(id) is too narrow to say anything") }
        XCTAssertEqual(words["sleep-0"]?.x, TLBarLabel.pad, "the word begins 6 points inside its piece")
        XCTAssertEqual(words["sleep-0"]?.dark, false, "light text on the night's indigo")
        XCTAssertEqual(words["stay-1"]?.dark, true, "dark ink on the pale hue of home")
        XCTAssertEqual(words["stay-9"]?.dark, true)
    }

    func testAStayTheNightCutsInTwoHasAWordOnEachPieceUnderIdsOfItsOwn() {
        let home = TLPiece.Fill.place(RGB(255, 168, 150), kept: true)
        let pieces = [TLPiece(id: "s0", t0: 360, t1: 700, fill: home, name: "Home", icon: "house.fill"), TLPiece(id: "n", t0: 700, t1: 900, fill: .sleep, name: nil, icon: "moon.fill"),
                      TLPiece(id: "s0", t0: 900, t1: 1440, fill: home, name: "Home", icon: "house.fill")]
        let scene = TLBarScene(span: 360...DAY, now: nil, pieces: pieces, ahead: [], stars: [], ticks: [])
        let placed = TLBarLabel.placed(scene, width: TLLayout.barW, measure: measure)
        XCTAssertEqual(placed.map(\.item), ["s0", "n", "s0"], "a word on each piece of home")
        XCTAssertEqual(Set(placed.map(\.id)).count, 3, "and none under another's id, which the bar's ForEach would take for one view")
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
        XCTAssertEqual(build(TimelineFixture.today(cal)).scene.ticks, [9, 12, 15, 18, 21, 24])
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

    func testTheListRestsWithTheLineForNowAboutSixtyPercentDownAndARowBeginningAtTheTop() throws {
        let m = build(TimelineFixture.today(cal)), room = TLLayout.viewport
        let y = m.restY(room: room)
        XCTAssertTrue(m.offsets.contains { abs($0 - 8 - y) < 0.001 }, "a whole row begins 8 pt down, so the row above leaves nothing in the fade")
        XCTAssertEqual((m.offsets[m.nowIndex!] + TLMetrics.now / 2 - y) / room, 0.6, accuracy: 0.07, "the line for now stays about 60% of the way down")
        XCTAssertEqual(build(TimelineFixture.yesterday(cal), now: nil).restY(room: room), 0, "another day opens at its top")
        let short = try reply(#"{"id": "t", "lane": "her", "kind": "talk", "state": "done", "start_ms": \#(at(600)), "end_ms": \#(at(610)), "turns": 2}"#)
        XCTAssertEqual(build(short).restY(room: room), 0, "a day that fits does not scroll")
    }

    func testTheRowAtAMinuteIsTheLastOneBegunByThenElseTheFirst() throws {
        let m = build(TimelineFixture.today(cal))
        XCTAssertEqual(m.entry(at: 700)?.id, "move-3", "in the drive")
        XCTAssertEqual(m.entry(at: 550)?.id, "stay-1", "at home before anything happened there")
        XCTAssertEqual(m.entry(at: 600)?.id, "code-group-stay-1", "in the first of the sessions")
        XCTAssertEqual(m.entry(at: 620)?.id, "talk-1")
        XCTAssertEqual(m.entry(at: 10)?.id, "sleep-0", "before anything: the first")
        XCTAssertEqual(m.entry(at: 1439)?.id, "departure-demo-1")
    }

    // MARK: a finger on the bar

    func testAFingerOnAStayReadsItsHoursAndLengthAndWhatElseWentOnThen() {
        let r = build(TimelineFixture.today(cal)).readout(at: 615)
        XCTAssertEqual(r.head, "家 · 09:05–11:25 · 2 时 20 分")
        XCTAssertEqual(r.second, "Claude Code · 接上天气和身体数据 · 明天下午有什么安排？", "the session and the talk that cover 10:15, Claude Code first; the Mac work 15 minutes on is too far")
        XCTAssertNotNil(r.hue)
        XCTAssertTrue(r.strong, "a place he keeps")
        XCTAssertFalse(r.ahead)
        let night = build(TimelineFixture.today(cal)).readout(at: 500)
        XCTAssertEqual(night.head, "睡觉 · 00:55–09:05 · 8 时 5 分", "08:20: the night, with the hours it really had, though the bar shows only the end of it")
        XCTAssertEqual(night.second, "深睡 1:55 · REM 2:00", "08:20: the stages of the night he was in")
    }

    func testTheHeadGivesWayAtItsNameAndNeverAtItsHours() {
        let m = build(TimelineFixture.today(cal))
        XCTAssertEqual(m.readout(at: 615).name, "家")
        XCTAssertEqual(m.readout(at: 615).tail, " · 09:05–11:25 · 2 时 20 分")
        XCTAssertEqual(m.readout(at: 1335).name, "还没到")
        XCTAssertEqual(m.readout(at: 1335).tail, "")
    }

    func testYesterdaysAfternoonHasAHoleAndTheFingerReadsHowLongNothingWasRecorded() {
        let m = DayTimelineModel.build(TimelineFixture.yesterday(cal), now: nil)
        XCTAssertNil(m.seg(at: 1000))
        let r = m.readout(at: 1000)
        XCTAssertEqual(r.head, "没有记录 · 16:20–17:30 · 1 时 10 分")
        XCTAssertNil(r.hue)
        XCTAssertEqual(m.readout(at: 900).head, "UVic · 08:55–16:20 · 7 时 25 分")
        XCTAssertNil(m.lit(at: 1000), "nothing is lit in a hole: the stay before it is over")
        XCTAssertEqual(m.entry(at: 1000)?.title, "UVic", "though the list rests on it")
        XCTAssertEqual(m.lit(at: 900)?.title, "UVic")
    }

    func testAFingerOnATripReadsTheTripAndSaysWhenNothingElseWasThere() {
        let r = build(TimelineFixture.today(cal)).readout(at: 720)
        XCTAssertEqual(r.head, "开车 · 11:25–13:05 · 1 时 40 分")
        XCTAssertEqual(r.second, "这段时间没有别的记录")
        XCTAssertEqual(r.hue, TLPaint.trip)
        XCTAssertTrue(r.elsewhere.isEmpty, "a reminder that rang at 09:20 is hours away")
    }

    func testAFingerOnTheMinuteOfAPaymentReadsThePlaceItWasAtAndThePayment() {
        let r = build(TimelineFixture.today(cal)).readout(at: 800)
        XCTAssertEqual(r.head, "A&W · 13:15–13:55 · 40 分")
        XCTAssertEqual(r.second, "A&W · $12.40")
        XCTAssertFalse(r.strong, "a place he does not keep")
    }

    func testAtMostTwoThingsAreToldAndTheMostTellingComeFirst() {
        let r = build(TimelineFixture.today(cal)).readout(at: 1245)
        XCTAssertEqual(r.head, "家 · 18:50 起 · 3 时", "a stay still going has its start and how long so far")
        XCTAssertEqual(r.elsewhere, ["Jarvis · Xcode", "Claude Code · 论文第三章的数据", "和她说话 · 14 句"], "Mac work, Claude Code, then the talk: all cover 20:45")
        XCTAssertEqual(r.second, "Jarvis · Xcode · Claude Code · 论文第三章的数据", "the phone says the first two")
        XCTAssertEqual(r.elsewhere.count, 3)
        let more = build(TimelineFixture.today(cal)).readout(at: 1225)
        XCTAssertEqual(more.elsewhere, ["Jarvis · Xcode", "Claude · 积分换元的符号问题"], "the Mac work and the chat with Claude cover 20:25; the session that ended seven minutes before and the talk 15 minutes on do not")
        XCTAssertEqual(more.second, "Jarvis · Xcode · Claude · 积分换元的符号问题")
    }

    func testWhatMatteredIsToldByItsLabelAndWhatIsNearButNotOverIsTold() {
        let m = build(TimelineFixture.today(cal))
        XCTAssertEqual(m.readout(at: 1005).second, "和 Mia 聊了很久", "the call that covers 16:45, by the label that was put on it")
        XCTAssertEqual(m.readout(at: 1015).second, "和 Mia 聊了很久", "still on the call at 16:55")
        XCTAssertEqual(m.readout(at: 1025).second, "跑步 · 3.2 公里", "the run covers 17:05: what covers the minute is told, and the call that ended seven minutes before is not")
        XCTAssertEqual(m.readout(at: 1065).second, "跑步 · 3.2 公里", "ten minutes after the run")
        XCTAssertEqual(m.readout(at: 1070).second, "这段时间没有别的记录", "fifteen minutes after")
    }

    func testAFingerPastNowReadsThatItHasNotHappenedAndWhatWasPlanned() {
        let m = build(TimelineFixture.today(cal))
        let planned = m.readout(at: 1335)
        XCTAssertEqual(planned.head, "还没到")
        XCTAssertTrue(planned.ahead)
        XCTAssertNil(planned.hue)
        XCTAssertEqual(planned.second, "陶艺课")
        XCTAssertEqual(m.readout(at: 1420).second, "这段时间没有安排", "nothing is planned for then")
    }

    func testWhereNothingWasRecordedTheFingerReadsHowLongNothingWas() throws {
        let m = build(try reply(#"""
            {"id": "a", "lane": "you", "kind": "move", "state": "done", "start_ms": \#(at(600)), "end_ms": \#(at(700)), "activity": "walking"},
            {"id": "b", "lane": "you", "kind": "move", "state": "done", "start_ms": \#(at(760)), "end_ms": \#(at(800)), "activity": "walking"}
            """#))
        let r = m.readout(at: 730)
        XCTAssertEqual(r.head, "没有记录 · 11:40–12:40 · 1 时")
        XCTAssertNil(r.hue)
        XCTAssertEqual(r.second, "这段时间没有别的记录")
        XCTAssertEqual(m.readout(at: 400).head, "没有记录 · 06:00–10:00 · 4 时", "before the first stretch: from the bar's start")
    }

    func testAMinuteOffTheBarIsTheStartOrTheEndOfItAndNoHoleEndsBeforeItBegins() throws {
        let m = build(TimelineFixture.today(cal))
        XCTAssertEqual(m.span.lowerBound, 480, "the made-up day's bar starts at 08:00, though its night began at 00:55")
        XCTAssertEqual([m.onBar(30), m.onBar(480), m.onBar(900), m.onBar(1440), m.onBar(1500), m.onBar(.infinity), m.onBar(-.infinity)], [480, 480, 900, 1440, 1440, 1440, 480])
        XCTAssertEqual(m.readout(at: 30), m.readout(at: 480), "00:30 is not on the bar: a finger there reads where the bar starts")
        XCTAssertEqual(m.readout(at: 30).head, "睡觉 · 00:55–09:05 · 8 时 5 分", "and not a hole from 08:00 back to 00:55")
        XCTAssertEqual(m.readout(at: 1500), m.readout(at: 1440))
        // every minute from before the bar to after it, on every made-up day, with and without a now: a hole's hours run forward
        func clock(_ s: Substring) -> Int { (Int(s.prefix(2)) ?? -1) * 60 + (Int(s.suffix(2)) ?? -1) }
        var holes = 0
        for r in [TimelineFixture.today(cal), TimelineFixture.yesterday(cal), TimelineFixture.fragmented(cal), TimelineFixture.talkOnly(cal)] {
            for now in [nil, TimelineFixture.now] as [Double?] {
                let day = DayTimelineModel.build(r, now: now)
                for t in stride(from: -90.0, through: 1530.0, by: 1.0) + [day.span.upperBound, day.span.lowerBound, now ?? 0] {
                    let head = day.readout(at: t).head, parts = head.components(separatedBy: " · ")
                    guard parts.first == "没有记录" else { continue }
                    let ends = parts[1].split(separator: "–")
                    XCTAssertEqual(ends.count, 2, head)
                    if ends.count == 2 { XCTAssertLessThan(clock(ends[0]), clock(ends[1]), "\(r.date) now \(now.map { "\($0)" } ?? "none") at \(t): \(head)") }
                    holes += 1
                }
            }
        }
        XCTAssertGreaterThan(holes, 100, "the scan did meet holes")
    }

    func testAFingerOnTheBarLightsTheRowThereAndClosesTheOpenOneAndLettingGoClearsBoth() {
        withTimeline { e in
            let m = build(TimelineFixture.today(cal))
            e.ui.tl.openRow = "talk-1"
            let n = e.ui.tl.request.n
            e.ui.tl.follow(700, in: m)
            XCTAssertEqual(e.ui.tl.scrub, 700)
            XCTAssertEqual(e.ui.tl.hot, "move-3", "the drive covers 11:40")
            XCTAssertNil(e.ui.tl.openRow, "a row that was open is closed: the list goes where the finger goes")
            XCTAssertEqual(e.ui.tl.request.y, m.top(of: "move-3"))
            XCTAssertFalse(e.ui.tl.request.animated)
            XCTAssertEqual(e.ui.tl.request.n, n + 1)
            e.ui.tl.follow(705, in: m)
            XCTAssertEqual(e.ui.tl.request.n, n + 1, "still the drive: the list is not asked again")
            e.ui.tl.follow(1000, in: m)
            XCTAssertEqual(e.ui.tl.hot, "call-demo-1")
            XCTAssertEqual(e.ui.tl.request.n, n + 2)
            // past now, before what was planned: the list goes to the last row there is, but nothing is lit as if it were then
            e.ui.tl.follow(1315, in: m)
            XCTAssertNil(e.ui.tl.hot)
            XCTAssertEqual(e.ui.tl.request.y, m.top(of: "reminder-demo-2"))
            e.ui.tl.follow(1335, in: m)
            XCTAssertEqual(e.ui.tl.hot, "event-demo-1", "the class planned for 22:00 covers 22:15")
            e.ui.tl.follow(nil, in: nil)
            XCTAssertNil(e.ui.tl.scrub)
            XCTAssertNil(e.ui.tl.hot)
        }
    }

    func testAFingerOffTheBarIsHeldAtTheStartOrTheEndOfIt() {
        withTimeline { e in
            let m = build(TimelineFixture.today(cal))
            e.ui.tl.follow(30, in: m)
            XCTAssertEqual(e.ui.tl.scrub, 480, "00:30 is before the bar starts (08:00)")
            XCTAssertEqual(e.ui.tl.hot, m.lit(at: 480)?.id)
            e.ui.tl.follow(2000, in: m)
            XCTAssertEqual(e.ui.tl.scrub, 1440)
            e.ui.tl.follow(900, in: nil)
            XCTAssertEqual(e.ui.tl.scrub, 900, "with no day to measure against there is nothing to clamp to")
            e.ui.tl.follow(nil, in: nil)
            XCTAssertNil(e.ui.tl.scrub)
        }
    }

    // MARK: the setting

    func testVoiceOversStepsGoFromStretchToStretchAndStayAtTheEnds() {
        let sc = build(TimelineFixture.today(cal)).scene
        let starts = sc.stretchStarts
        XCTAssertGreaterThan(starts.count, 3)
        XCTAssertEqual(starts, starts.sorted())
        XCTAssertEqual(starts.first, sc.span.lowerBound)
        XCTAssertEqual(sc.pieces.last?.fill, .ahead)
        XCTAssertFalse(starts.contains(sc.pieces.last?.t0 ?? -1), "what has not happened yet is not a stretch")
        XCTAssertEqual(sc.step(from: nil, forward: true), starts.first)
        XCTAssertEqual(sc.step(from: nil, forward: false), starts.last)
        XCTAssertEqual(sc.step(from: starts[0], forward: true), starts[1])
        XCTAssertEqual(sc.step(from: starts[1], forward: false), starts[0])
        XCTAssertEqual(sc.step(from: starts.last, forward: true), starts.last, "past the last it stays")
        XCTAssertEqual(sc.step(from: starts[0], forward: false), starts[0], "and before the first")
        // where a finger left it, in the middle of a stretch: forward is the next one, back the start of this one
        let mid = (starts[1] + starts[2]) / 2
        XCTAssertEqual(sc.step(from: mid, forward: true), starts[2])
        XCTAssertEqual(sc.step(from: mid, forward: false), starts[1])
        XCTAssertNil(TLBarScene.empty.step(from: nil, forward: true))
    }

    func testTheTopOfTheTimelineIsOnShowOnlyWhenSheIsInHerCornerAndNoPanelCoversIt() {
        let ui = StarEngine().ui
        ui.tlHome = true; ui.panelOpen = false
        XCTAssertTrue(ui.tlHeaderShown)
        ui.panelOpen = true
        XCTAssertFalse(ui.tlHeaderShown, "under the panel nothing of it moves")
        ui.panelOpen = false; ui.tlHome = false
        XCTAssertFalse(ui.tlHeaderShown, "with the dial on show, or while she is on her way, nor does it")
    }

    func testASwipeAlongTheDayIsADayBeforeOrAfterAndADragUpAndDownIsTheListsOwn() {
        XCTAssertEqual(TimelineState.swipe(dx: 80, dy: 10), -1, "dragged right: the day before")
        XCTAssertEqual(TimelineState.swipe(dx: -80, dy: -10), 1, "dragged left: the day after")
        XCTAssertNil(TimelineState.swipe(dx: 59, dy: 0), "not far enough")
        XCTAssertNil(TimelineState.swipe(dx: 100, dy: 60), "not sideways enough")
        XCTAssertNil(TimelineState.swipe(dx: 10, dy: 200), "the list's own")
    }

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
        for id in ["code-group-stay-1", "now"] { XCTAssertNil(try entry(m, id).about, id) }
        for id in ["ride-8", "payment-demo-1", "claude-demo-1", "departure-demo-1", "workout-demo-1"] { XCTAssertNotNil(try entry(m, id).about, id) }
        let dep = try reply(#"{"id": "departure-1", "lane": "you", "kind": "departure", "state": "next", "start_ms": \#(at(1400)), "end_ms": \#(at(1400)), "route": "12"}"#)
        XCTAssertEqual(build(dep, now: nil).entries[0].about?.kind, "departure")
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
            // another row opened is another item; a row that is no one item (sessions folded together) leaves the chip as it is
            e.ui.tl.openRow = "talk-1"
            e.captureAbout()
            XCTAssertEqual(e.ui.about?.id, "talk-1")
            e.ui.tl.openRow = "code-group-stay-1"
            e.captureAbout()
            XCTAssertEqual(e.ui.about?.id, "talk-1")
            e.ui.tl.openRow = "ride-8"
            e.captureAbout()
            XCTAssertEqual(e.ui.about?.id, "ride-8", "a bus ride is one the host takes")
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
