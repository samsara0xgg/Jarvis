import XCTest
@testable import Jarvis

/// Which card the watch lays over her (ios/JarvisWatchShared/WatchCard.swift): her answer over all, then what he opened or what came in, then 该出门了 near its time.
final class WatchCardTests: XCTestCase {
    private let now = Date(timeIntervalSince1970: 1_790_000_000)

    private func item(_ id: String) -> WatchDay.Waiting { .init(id: id, kind: .reminder, symbol: "bell", title: id, sub: "", actions: [.init(id: "ack", title: "ok")]) }

    private func day(waiting: [String] = [], leaveIn: TimeInterval? = nil) -> WatchDay {
        var d = WatchDay(date: "2026-10-24", language: "zh", now: 1310, made: now)
        d.waiting = waiting.map(item)
        d.leaveAt = leaveIn.map { now.addingTimeInterval($0) }
        return d
    }

    private func top(_ d: WatchDay?, answer: WatchCard? = nil, queue: [String] = [], answered: Set<String> = [], leaveDismissed: Date? = nil) -> WatchCard? {
        WatchCard.top(answer: answer, day: d, queue: queue, answered: answered, leaveDismissed: leaveDismissed, now: now)
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
}
