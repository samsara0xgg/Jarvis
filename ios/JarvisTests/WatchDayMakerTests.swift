import XCTest
@testable import Jarvis

/// The phone's day as the watch draws it, from the made-up day at 21:50 (the DEBUG fixture the timeline's scenes use): the ring in the bar's colours, the stretch now, what waits, what comes next.
@MainActor
final class WatchDayMakerTests: XCTestCase {
    private var saved = Words.Lang.zh
    private let now = TimelineFixture.now
    private var made: Date { Cal.cur.date(0).addingTimeInterval(now * 60) }

    override func setUp() async throws {
        saved = Words.shared.language
        Words.shared.set(.zh)
    }
    override func tearDown() async throws { Words.shared.set(saved) }

    private func day(mail: Bool = false, minute: Double? = TimelineFixture.now, cards: [WaitingCard] = [], riding: WatchDayMaker.Riding? = nil, acked: Set<String> = []) -> WatchDay {
        WatchDayMaker.make(TimelineFixture.today(Cal.cur, mail: mail, doing: true), minute: minute, made: made, cards: cards, riding: riding, acked: acked)
    }
    private func ride(_ json: String, board: String? = "光华路") throws -> WatchDayMaker.Riding {
        WatchDayMaker.Riding(route: "32", board: board, state: try JSONDecoder().decode(RideAttributes.ContentState.self, from: Data(json.utf8)))
    }
    private func hex(_ c: RGB) -> UInt32 { UInt32(c.r.rounded()) << 16 | UInt32(c.g.rounded()) << 8 | UInt32(c.b.rounded()) }

    func testTheDayIsTheBrainsAtTheMinuteItWasMade() {
        let d = day()
        XCTAssertEqual(d.date, Cal.cur.iso(0))
        XCTAssertEqual(d.language, "zh")
        XCTAssertEqual(d.now, 1310)
        XCTAssertEqual(d.made, made)
    }

    func testTheRingIsTheBarsPastPiecesInItsColours() {
        let d = day()
        XCTAssertFalse(d.blocks.isEmpty)
        XCTAssertEqual(d.blocks.map(\.t0), d.blocks.map(\.t0).sorted(), "in time order")
        XCTAssertLessThanOrEqual(d.blocks.map(\.t1).max() ?? 0, 1310, "nothing that has not happened")
        let night = d.blocks[0]
        XCTAssertEqual(night.title, "睡觉")
        XCTAssertEqual(night.t0, 55)
        // the night's colour at the bar's strength (0.9) over the ground it is drawn on
        let g = TLPaint.ground, c = TLPaint.sleep
        XCTAssertEqual(night.rgb, hex(RGB(c.r * 0.9 + g.r * 0.1, c.g * 0.9 + g.g * 0.1, c.b * 0.9 + g.b * 0.1)))
        // a stretch of what he was doing wears its category's colour, and says its place, hours and length when tapped
        let work = d.blocks.last
        XCTAssertEqual(work?.title, "做 Jarvis")
        XCTAssertEqual(work?.t1, 1310)
        XCTAssertEqual(work?.rgb, hex(RGB(91 * 0.85 + g.r * 0.15, 140 * 0.85 + g.g * 0.15, 255 * 0.85 + g.b * 0.15)))
        XCTAssertEqual(work?.sub, "家 · 19:35 起 · 2 小时 15 分")
    }

    func testWhatIsPlannedIsTicksAndTheOnesThatWaitAreAmber() {
        XCTAssertEqual(day().ahead, [.init(t: 1320, waits: true), .init(t: 1380, waits: false), .init(t: 1400, waits: false)])
    }

    func testTheStretchNowIsInTheListsWords() {
        let c = day().current
        XCTAssertEqual(c?.title, "做 Jarvis")
        XCTAssertEqual(c?.symbol, "briefcase")
        XCTAssertEqual(c?.rgb, 0x5B8CFF)
        XCTAssertEqual(c?.sub, "家")
        XCTAssertEqual(c?.right, "2 小时 15 分")
        XCTAssertFalse(c?.planned ?? true)
    }

    func testAReminderThatRangWaitsAndIsAcknowledged() {
        let w = day().waiting
        XCTAssertEqual(w.map(\.id), ["reminder-demo-2"], "the event his mail made has no commitment of the brain's here: nothing to say to it")
        XCTAssertEqual(w.first?.kind, .reminder)
        XCTAssertEqual(w.first?.title, "吃药")
        XCTAssertEqual(w.first?.sub, "21:30 响过了")
        XCTAssertEqual(w.first?.actions, [.init(id: "ack", title: "知道了")])
        XCTAssertEqual(day(acked: ["reminder-demo-2"]).waiting, [], "he has said so: it waits no more")
    }

    func testAnEventHisMailMadeIsConfirmedOrNot() {
        let w = day(mail: true).waiting
        XCTAssertEqual(w.map(\.title), ["吃药", "陶艺课"])
        XCTAssertEqual(w[1].actions, [.init(id: "confirm", title: "是我的"), .init(id: "deny", title: "不是")])
        XCTAssertEqual(w[1].id, TimelineFixture.commitmentId(Cal.cur))
    }

    func testWhatComesNextIsInTimeOrderWithoutWhatWaits() {
        let n = day().next
        XCTAssertEqual(n.map(\.id), ["event-demo-1", "reminder-demo-1", "departure-demo-1"])
        XCTAssertEqual(n.map(\.title), ["陶艺课", "给 Mia 回电话", "12 · Hillside Centre"])
        XCTAssertEqual(n.map(\.right), ["", "", "23:10 出门"], "the time left is in the grey line, so a title is never squeezed by it")
        XCTAssertTrue(n.allSatisfy(\.planned))
        XCTAssertEqual(n.map(\.symbol), ["calendar.circle", "bell", "bus"], "not happened yet: the outline")
        XCTAssertEqual(n[0].sub, "22:00 · 还有 10 分钟 · 邮件 · 还没确认 · Studio 4")
        XCTAssertEqual(n[1].sub, "23:00 · 还有 1 小时 10 分 · 还没到点")
        XCTAssertEqual(day(mail: true).next.map(\.id), ["reminder-demo-1", "departure-demo-1"], "an event he can confirm is under 等你, not twice")
    }

    func testLengthsAreInWordsAndEveryRowOfTheSampleToo() {
        // 18 分钟 under an hour, 1 小时 40 分 past it: a bare 分 is only the tail of hours
        let old = try! NSRegularExpression(pattern: #"(?<!小时 )\b\d+ 分(?!钟)"#)
        func words(_ d: WatchDay) -> [String] {
            let rows = d.next + (d.current.map { [$0] } ?? [])
            return d.blocks.map(\.sub) + rows.flatMap { [$0.sub, $0.right] } + d.waiting.map(\.sub) + [d.headline?.note, d.ride?.sub].compactMap { $0 }
        }
        for d in [day(), day(mail: true), WatchDay.sample] {
            for w in words(d) { XCTAssertNil(old.firstMatch(in: w, range: NSRange(w.startIndex..., in: w)), w) }
        }
    }

    func testTheHeadlineIsTheFirstThingToComeAndAmberWhenItWaits() {
        XCTAssertEqual(day().headline, .init(title: "22:00 陶艺课", note: "还有 10 分钟", waits: true))
        // the evening is over: what still waits is the headline
        let late = day(minute: 1439)
        XCTAssertEqual(late.headline, .init(title: "吃药", note: "等你", waits: true))
        Words.shared.set(.en)
        XCTAssertEqual(day(minute: 1439).headline?.note, "Waiting for you")
    }

    func testTheLeaveTimeIsTheDeparturesOwn() {
        XCTAssertEqual(day().leaveAt, Cal.cur.date(0).addingTimeInterval(1390 * 60))
        XCTAssertNil(day(minute: 1439).leaveAt, "past it")
    }

    func testACardIsTheCardWithItsButtonsThatNeedNothingTyped() throws {
        let json = #"{"clarification": {"id": "q1", "question": "送到哪里？", "fields": [{"label": "地址"}]}, "claude_requests": [{"id": "r1", "tool": "Bash", "input": {"command": "npm test"}, "cwd": "/a/jarvis", "always": "x", "session_id": "s", "title": "修测试", "project": "jarvis"}], "notices": {"notices": [{"id": "reminder-demo-2", "kind": "mail", "title": "吃药", "line": "", "mail_kind": "reminder"}, {"id": "n9", "kind": "mail", "title": "面试邀请", "line": "来自 HR"}]}}"#
        let cards = try XCTUnwrap(WaitingCards.parse(Data(json.utf8)))
        let w = day(cards: cards).waiting
        XCTAssertEqual(w.map(\.id), ["reminder-demo-2", "clarification-q1", "claude-r1", "notice-n9"], "a reminder's card is its row")
        XCTAssertEqual(w[1].actions, [.init(id: "a0", title: "取消")], "the send button needs the typing he cannot do here")
        XCTAssertEqual(w[2].actions.map(\.title), ["拒绝", "一直允许", "允许"])
        XCTAssertEqual(w[2].actions.map(\.id), ["a0", "a1", "a2"])
        XCTAssertEqual(w[3].title, "面试邀请")
        XCTAssertEqual(w[3].sub, "来自 HR")
        XCTAssertEqual(w[3].kind, .card)
        let login = WaitingCard(id: "plugin-p", kind: .plugin, brainId: "p", short: "", title: "连接 X", meta: "", body: "", actions: [CardAction(label: "登录", answer: .openLogin)])
        XCTAssertNil(WatchDayMaker.card(login), "opening a page on the phone is not for the wrist")
    }

    func testTheBusHeNeedsToGetOffIsTheHeadline() throws {
        let riding = try ride(#"{"route":"32","next_stop":"光华路","stops_left":4,"minutes_to_alight":6,"alight_stop":"国贸","position_live":true,"arrival_live":true,"phase":"riding"}"#)
        let d = day(riding: riding)
        XCTAssertEqual(d.ride, .init(route: "32 路", title: "还有 4 站", sub: "到 国贸 · 6 分钟", stops: ["光华路", "国贸"], at: 0, note: nil, alightNext: false))
        XCTAssertEqual(d.headline, .init(title: "32 路", note: "还有 4 站", waits: false))
        let arriving = try ride(#"{"route":"32","next_stop":"国贸","stops_left":1,"minutes_to_alight":0,"alight_stop":"国贸","position_live":false,"arrival_live":false,"phase":"arriving"}"#)
        let a = day(riding: arriving)
        XCTAssertEqual(a.ride?.title, "下一站下车")
        XCTAssertEqual(a.ride?.sub, "到 国贸 · 马上到")
        XCTAssertEqual(a.ride?.note, "没有车的实时位置，按时刻表")
        XCTAssertEqual(a.ride?.alightNext, true)
        XCTAssertEqual(a.headline?.waits, true)
        XCTAssertNil(day().ride)
    }

    func testTheActionIdsOfACardAreItsButtonsPlaces() {
        XCTAssertEqual(WatchActionID.card(2), "a2")
        XCTAssertEqual(WatchActionID.cardIndex("a2"), 2)
        XCTAssertNil(WatchActionID.cardIndex("ack"))
        XCTAssertNil(WatchActionID.cardIndex("a"))
    }
}
