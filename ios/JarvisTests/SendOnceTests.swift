import XCTest
@testable import Jarvis

/// One message is one turn, whatever the network does (ADR 0262): the id is made once per message, the field empties at the tap, a second tap in flight does nothing,
/// and a send that got no answer stays on screen as not sent and goes again under the same id. The brain's end is a stub that says what each try came to.
final class SendOnceTests: XCTestCase {
    /// the words out: records every try, answers from `outcomes` in order, and (with `hold`) waits until `release()` before it answers
    @MainActor private final class Brain {
        var tries: [(utterance: String, text: String)] = []
        var outcomes: [Delivery]
        var hold = false
        private var waiting: CheckedContinuation<Void, Never>?
        init(_ outcomes: [Delivery]) { self.outcomes = outcomes }
        func deliver(_ utterance: String, _ text: String, _ ids: [String], _ about: About?) async -> Delivery {
            tries.append((utterance, text))
            if hold { await withCheckedContinuation { waiting = $0 } }
            return outcomes.isEmpty ? .taken(record: nil) : outcomes.removeFirst()
        }
        func release() { waiting?.resume(); waiting = nil }
    }

    @MainActor private func engine(_ brain: Brain) -> StarEngine {
        let e = StarEngine()
        e.deliver = { u, t, i, a in await brain.deliver(u, t, i, a) }
        return e
    }

    @MainActor private func until(_ what: String, _ ok: () -> Bool) async {
        for _ in 0..<200 where !ok() { try? await Task.sleep(nanoseconds: 10_000_000) }
        XCTAssertTrue(ok(), what)
    }

    @MainActor private func his(_ e: StarEngine) -> LiveTurn? { e.ui.liveTurns.first { $0.his } }

    @MainActor func testTheFieldEmptiesAtTheTapAndHisLineIsOnItsWay() async {
        let brain = Brain([.taken(record: nil)]); brain.hold = true
        let e = engine(brain)
        e.ui.text = "司机不知道咋的下车了逆天 \n"
        e.sendToBrain("司机不知道咋的下车了逆天")
        XCTAssertEqual(e.ui.text, "", "empty the moment he taps, before anything has gone out")
        XCTAssertTrue(e.sendingToBrain)
        await until("his line shows, dimmed") { self.his(e)?.send == .sending }
        XCTAssertEqual(his(e)?.text, "司机不知道咋的下车了逆天")
        brain.release()
        await until("the brain has it") { !e.sendingToBrain }
        XCTAssertEqual(his(e)?.send, .sent)
        XCTAssertTrue(e.unsent.isEmpty)
    }

    @MainActor func testASecondTapWhileTheFirstIsInFlightMakesNoSecondTurn() async {
        let brain = Brain([.taken(record: nil)]); brain.hold = true
        let e = engine(brain)
        e.ui.text = "你好"
        e.sendToBrain("你好")
        await until("the first try is out") { brain.tries.count == 1 }
        e.ui.text = "你好"                   // the same words typed again, or the field refilled
        e.sendToBrain("你好")
        e.sendToBrain("你好")
        try? await Task.sleep(nanoseconds: 100_000_000)
        XCTAssertEqual(brain.tries.count, 1, "one message in flight is one try")
        XCTAssertEqual(e.ui.liveTurns.filter(\.his).count, 1)
        XCTAssertEqual(e.ui.text, "你好", "the second tap left the field alone")
        brain.release()
        await until("done") { !e.sendingToBrain }
    }

    @MainActor func testASendThatGotNoAnswerStaysNotSentAndGoesAgainUnderTheSameId() async {
        let brain = Brain([.lost("连不上 Jarvis"), .lost("连不上 Jarvis"), .taken(record: "r1")])
        let e = engine(brain)
        e.ui.text = "司机不知道咋的下车了逆天"
        e.sendToBrain("司机不知道咋的下车了逆天")
        await until("not sent") { self.his(e)?.send == .failed && !e.sendingToBrain }
        XCTAssertEqual(e.ui.text, "", "the words are in his bubble, not back in the field")
        XCTAssertEqual(e.unsent.count, 1)
        let row = "l\(his(e)!.id)"
        e.sendAgain(row: row)
        await until("not sent again") { brain.tries.count == 2 && !e.sendingToBrain }
        XCTAssertEqual(his(e)?.send, .failed)
        e.sendAgain(row: row)
        await until("taken") { brain.tries.count == 3 && !e.sendingToBrain }
        XCTAssertEqual(Set(brain.tries.map(\.utterance)).count, 1, "every try of one message carries its one id")
        XCTAssertEqual(brain.tries.map(\.text), Array(repeating: "司机不知道咋的下车了逆天", count: 3))
        XCTAssertEqual(e.ui.liveTurns.filter(\.his).count, 1, "the same bubble, not a new one each try")
        XCTAssertEqual(his(e)?.send, .sent)
        XCTAssertEqual(his(e)?.recordId, "r1")
        XCTAssertTrue(e.unsent.isEmpty)
        XCTAssertEqual(e.ui.text, "")
    }

    @MainActor func testTheTalkStaysWhileAMessageIsNotSent() async {
        let brain = Brain([.lost("连不上 Jarvis")])
        let e = engine(brain)
        e.ui.text = "在吗"
        e.sendToBrain("在吗")
        await until("not sent") { self.his(e)?.send == .failed && !e.sendingToBrain }
        e.stepTalk(nowMs() + 10 * 60_000)
        XCTAssertTrue(e.ui.talking, "the line with 点重试 does not time out")
    }

    @MainActor func testARefusalTakesHisLineBackIntoTheField() async {
        let brain = Brain([.refused("文件过期了，再发一次")])
        let e = engine(brain)
        e.ui.text = "看看这个"
        e.sendToBrain("看看这个")
        await until("refused") { !e.sendingToBrain }
        XCTAssertNil(his(e), "nothing was taken: no line left behind")
        XCTAssertEqual(e.ui.text, "看看这个")
        XCTAssertTrue(e.unsent.isEmpty)
        e.ui.text = "看看这个"
        e.sendToBrain("看看这个")
        await until("second message tried") { brain.tries.count == 2 }
        XCTAssertNotEqual(brain.tries[0].utterance, brain.tries[1].utterance, "a refused message that he sends again is a new message")
    }

    @MainActor func testAnAnswerThatFailedIsAskedAgainAsANewMessage() async {
        let brain = Brain([.taken(record: nil), .taken(record: nil)])
        let e = engine(brain)
        e.ui.text = "订位"
        e.sendToBrain("订位")
        await until("taken") { brain.tries.count == 1 && !e.sendingToBrain }
        XCTAssertEqual(e.sentTurn?.taken, true)
        e.sendAgain()
        await until("asked again") { brain.tries.count == 2 }
        XCTAssertNotEqual(brain.tries[0].utterance, brain.tries[1].utterance, "the brain already has the first, and would answer it with the failed turn")
        await until("done") { !e.sendingToBrain }
    }

    func testTheHttpRouteCarriesTheSameId() throws {
        let body = try BrainClient.submitBody("嗨", attachments: [], about: nil, utterance: "u-1")
        XCTAssertEqual((try JSONSerialization.jsonObject(with: body) as? [String: Any])?["utterance_id"] as? String, "u-1")
        let old = try BrainClient.submitBody("嗨", attachments: [], about: nil)
        XCTAssertNil((try JSONSerialization.jsonObject(with: old) as? [String: Any])?["utterance_id"], "no id, no key")
    }
}
