import XCTest
@testable import Jarvis

/// What the timeline's place for a talk holds (Sky/DayTimelineTalk.swift): the latest round and no earlier one, his words only when he spoke them, a card counted and not drawn, a message or a turn
/// that did not go; and that the place is there for a talk on the timeline and not on the dial.
final class DayTimelineTalkTests: XCTestCase {
    private var seq = 0
    private func his(_ text: String, heard: Bool = false, send: SendState = .sent) -> LiveTurn { seq += 1; return LiveTurn(id: seq, his: true, text: text, heard: heard, send: send) }
    private func hers(_ text: String, leadIn: Bool = false, status: StatusLine? = nil, failed: Bool = false) -> LiveTurn {
        seq += 1
        return LiveTurn(id: seq, his: false, text: text, leadIn: leadIn, status: status, failed: failed)
    }
    private func card() throws -> LiveTurn {
        let c = try XCTUnwrap(WaitingCards.parse(Data(#"{"clarification": {"id": "Qdebug01", "question": "哪一天，几位？", "fields": [{"label": "哪一天", "choices": ["周五", "周六"]}]}}"#.utf8))?.first)
        seq += 1
        return LiveTurn(id: seq, his: false, text: "", card: c)
    }

    func testThePlaceHoldsTheLatestRoundAlone() {
        let t = TLTalk.of([his("第一句"), hers("好。"), his("第二句", heard: true), hers("我查查。", leadIn: true), hers("三点有二面。")])
        XCTAssertEqual(t.heard, "第二句")
        XCTAssertEqual(t.hers.map(\.text), ["我查查。", "三点有二面。"], "the round before is the history's")
        XCTAssertEqual(t.answer, t.hers.last?.id, "the answer is her row after the line before it")
    }

    func testAnAnswerThatIsOnlyALeadInIsTheLeadIn() {
        let t = TLTalk.of([his("查一下"), hers("我上网查查。", leadIn: true)])
        XCTAssertEqual(t.answer, t.hers.first?.id)
    }

    func testHisWordsAreShownForARoundHeSpokeAndNotForOneHeTypedOrASendButtonMade() {
        XCTAssertEqual(TLTalk.of([his("  明天几点出门 ", heard: true)]).heard, "明天几点出门", "trimmed, as the phone heard them")
        XCTAssertNil(TLTalk.of([his("明天几点出门"), hers("九点。")]).heard, "typed")
        XCTAssertNil(TLTalk.of([his("  ", heard: true), hers("九点。")]).heard, "a recognizer that has heard nothing yet shows nothing")
    }

    func testTheStatusStandsInHerPlaceUntilHerWordsTakeItsRow() {
        let t = TLTalk.of([his("订位"), hers("", status: StatusLine(label: "查公交", since: 0))])
        XCTAssertEqual(t.hers.count, 1)
        XCTAssertNotNil(t.hers.first?.status)
        XCTAssertNil(t.answer, "she has said nothing to open the history at")
        XCTAssertFalse(t.isEmpty)
    }

    func testACardIsCountedAndNotDrawn() throws {
        let t = TLTalk.of([his("订位"), hers("好，先问你一件事。"), try card(), try card()])
        XCTAssertEqual(t.cards, 2)
        XCTAssertEqual(t.hers.map(\.text), ["好，先问你一件事。"])
        XCTAssertEqual(TLTalk.of([his("订位"), try card()]).hers.count, 0)
    }

    func testAMessageThatDidNotGoOutAndATurnThatFailedAreKept() {
        let a = TLTalk.of([his("订位", send: .failed)])
        XCTAssertEqual(a.unsent?.text, "订位")
        XCTAssertFalse(a.isEmpty, "the place says 没发出去 even when she has said nothing")
        let b = TLTalk.of([his("订位"), hers("没答出来", failed: true)])
        XCTAssertEqual(b.hers.first?.failed, true)
        XCTAssertNil(b.unsent)
    }

    func testNothingInTheRoundIsNothing() {
        XCTAssertTrue(TLTalk.of([]).isEmpty)
        XCTAssertTrue(TLTalk.of([his("订位")]).isEmpty, "a typed message with nothing back yet: her sentence stays until the typing line comes")
        XCTAssertTrue(TLTalk.of([his("订位"), hers("")]).isEmpty, "a row with no words, no status and no failure is no row")
    }

    // MARK: whether the place is there

    @MainActor func testThePlaceIsThereForATalkOnTheTimelineAndNotOnTheDial() {
        let kept = HomeModule.shared.kind
        defer { HomeModule.shared.kind = kept }
        let e = StarEngine()
        e.beginTalk()
        e.ui.liveTurns = [his("明天几点出门", heard: true)]
        HomeModule.shared.kind = .stars
        e.stepTalk(nowMs())
        XCTAssertFalse(e.ui.tlTalk, "the dial reads the talk under itself")
        HomeModule.shared.kind = .timeline
        e.stepTalk(nowMs())
        XCTAssertTrue(e.ui.tlTalk)
        e.ui.liveTurns = []
        e.stepTalk(nowMs())
        XCTAssertFalse(e.ui.tlTalk, "nothing in the round yet: her sentence, the bar and the chips stay")
    }

    // MARK: the history at her answer

    @MainActor func testTheHistoryOpensAtHerAnswer() async {
        let e = StarEngine()
        e.showAnswer(7)
        XCTAssertTrue(e.ui.panelOpen)
        for _ in 0..<100 where e.ui.scrollTo == nil { try? await Task.sleep(nanoseconds: 10_000_000) }
        XCTAssertEqual(e.ui.scrollTo, "l7", "the live rows of the stream are named l<id>")
        let f = StarEngine()
        f.showAnswer(nil)
        XCTAssertTrue(f.ui.panelOpen, "no answer yet: the history from its foot")
    }
}
