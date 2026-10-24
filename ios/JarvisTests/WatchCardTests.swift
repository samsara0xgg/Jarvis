import XCTest
@testable import Jarvis

/// Which card the watch lays over her (ios/JarvisWatchShared/WatchCard.swift): her answer over all, then what he opened or what came in, then 该出门了 near its time,
/// then 早 in the morning, then the stretch she is not sure of.
final class WatchCardTests: XCTestCase {
    private let now = Date(timeIntervalSince1970: 1_790_000_000)

    private func item(_ id: String) -> WatchDay.Waiting { .init(id: id, kind: .reminder, symbol: "bell", title: id, sub: "", actions: [.init(id: "ack", title: "ok")]) }

    private func day(waiting: [String] = [], leaveIn: TimeInterval? = nil) -> WatchDay {
        var d = WatchDay(date: "2026-10-24", language: "zh", now: 1310, made: now)
        d.waiting = waiting.map(item)
        d.leaveAt = leaveIn.map { now.addingTimeInterval($0) }
        return d
    }

    private func top(_ d: WatchDay?, answer: WatchCard? = nil, queue: [String] = [], answered: Set<String> = [], leaveDismissed: Date? = nil,
                     at: Date? = nil, morningSeen: String? = nil, putAway: Set<String> = []) -> WatchCard? {
        WatchCard.top(answer: answer, day: d, queue: queue, answered: answered, leaveDismissed: leaveDismissed, now: at ?? now, morningSeen: morningSeen, putAway: putAway)
    }

    private func clock(_ h: Int, _ m: Int = 0) -> Date { Calendar.current.date(bySettingHour: h, minute: m, second: 0, of: now)! }
    private let unsure = WatchDay.Unsure(id: "span-920", symbol: "book", rgb: nil, title: "在图书馆学习", span: "15:20–16:30", line: "", yes: "在图书馆学习")
    /// a morning: slept 400 minutes, two things to come, the first the headline; and a stretch she is not sure of
    private var morning: WatchDay {
        var d = day()
        d.slept = 400
        d.headline = .init(title: "10:00 组会", note: "", waits: false)
        d.next = ["meet", "lunch"].map { .init(id: $0, symbol: "calendar", title: $0, sub: "", right: "") }
        d.unsure = unsure
        return d
    }

    func testHerAnswerIsOverEverything() {
        let answer = WatchCard.answer(spoken: "好", written: "", action: item("reply"))
        XCTAssertEqual(top(day(waiting: ["med"], leaveIn: 60), answer: answer, queue: ["med"]), answer)
        XCTAssertEqual(top(nil, answer: answer), answer)
    }

    func testTheQueuesFirstItemThatStillWaits() {
        let d = day(waiting: ["med", "pottery"], leaveIn: 60)
        XCTAssertEqual(top(d, queue: ["pottery", "med"]), .waiting(item("pottery")))
        XCTAssertEqual(top(d, queue: ["gone", "pottery"]), .waiting(item("pottery")))                      // ended on the phone: skipped
        XCTAssertEqual(top(d, queue: ["pottery", "med"], answered: ["pottery"]), .waiting(item("med")))      // answered on the wrist: skipped
    }

    func testWhatWaitsButWasNotQueuedIsNoCard() {
        XCTAssertNil(top(day(waiting: ["med"])))          // at launch: the ask face and the dot, no card
    }

    func testLeavingComesUpTenMinutesBeforeAndStaysFiveAfter() {
        XCTAssertEqual(top(day(leaveIn: 600)), .leave(now.addingTimeInterval(600)))
        XCTAssertNil(top(day(leaveIn: 601)))
        XCTAssertEqual(top(day(leaveIn: -300)), .leave(now.addingTimeInterval(-300)))
        XCTAssertNil(top(day(leaveIn: -301)))
    }

    func testLeavingPutAwayStaysAwayUntilANewTime() {
        let t = now.addingTimeInterval(120)
        XCTAssertNil(top(day(leaveIn: 120), leaveDismissed: t))
        XCTAssertEqual(top(day(leaveIn: 180), leaveDismissed: t), .leave(now.addingTimeInterval(180)))
    }

    func testAQueuedItemComesBeforeLeaving() {
        XCTAssertEqual(top(day(waiting: ["med"], leaveIn: 60), queue: ["med"]), .waiting(item("med")))
    }

    func testMorningComesUpBeforeElevenUntilSeenThenTheStretchSheIsNotSureOf() {
        XCTAssertEqual(top(morning, at: clock(7, 42)), .morning(slept: 400, things: 2, first: "10:00 组会"))
        XCTAssertEqual(top(morning, at: clock(10, 59)), .morning(slept: 400, things: 2, first: "10:00 组会"))
        XCTAssertEqual(top(morning, at: clock(11)), .unsure(unsure))
        XCTAssertEqual(top(morning, at: clock(7, 42), morningSeen: "2026-10-24"), .unsure(unsure))
        XCTAssertNil(top(morning, at: clock(11), putAway: ["span-920"]))
        var noNight = morning; noNight.slept = nil
        XCTAssertEqual(top(noNight, at: clock(7, 42)), .unsure(unsure))            // no night from the phone: no 早
    }

    func testNothingPlannedHasNoFirstThing() {
        var d = morning; d.next = []
        XCTAssertEqual(top(d, at: clock(8)), .morning(slept: 400, things: 0, first: nil))
    }

    func testWhatWaitsAndLeavingComeBeforeMorning() {
        var d = morning
        d.waiting = [item("med")]
        d.leaveAt = clock(7, 50)
        XCTAssertEqual(top(d, queue: ["med"], at: clock(7, 42)), .waiting(item("med")))
        XCTAssertEqual(top(d, at: clock(7, 42)), .leave(clock(7, 50)))
    }

    func testAMarksAnswerIsUnderHerAnswerAndOverWhatWaits() {
        let mark = WatchCard.marked(id: "stay-13", day: "2026-10-24", when: "21:30-", title: "在 Studio 4")
        let answer = WatchCard.answer(spoken: "好", written: "", action: nil)
        XCTAssertEqual(WatchCard.top(answer: nil, mark: mark, day: day(waiting: ["med"], leaveIn: 60), queue: ["med"], answered: [], leaveDismissed: nil, now: now), mark)
        XCTAssertEqual(WatchCard.top(answer: answer, mark: mark, day: nil, queue: [], answered: [], leaveDismissed: nil, now: now), answer)
        XCTAssertEqual(WatchCard.top(answer: nil, mark: .markMissed("这会儿没有可标的"), day: nil, queue: [], answered: [], leaveDismissed: nil, now: now), .markMissed("这会儿没有可标的"))
    }
}
