import XCTest
import SwiftUI
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

    // MARK: how long it stays

    @MainActor private func withTimeline(_ body: (StarEngine) -> Void) {
        let kept = HomeModule.shared.kind
        HomeModule.shared.kind = .timeline
        defer { HomeModule.shared.kind = kept }
        body(StarEngine())
    }

    /// a typed round that is all there: his message, and her answer of `chars` characters
    @MainActor private func typedRound(_ e: StarEngine, chars: Int) {
        e.beginTalk()
        e.addTurn(his: true, "这周还有什么安排")
        e.addTurn(his: false, String(repeating: "安", count: chars))
    }

    @MainActor func testATypedRoundStaysUntilHeDoesSomethingElseAndAtMostTenMinutes() {
        withTimeline { e in
            typedRound(e, chars: 100)
            e.stepTalk(nowMs())
            XCTAssertTrue(e.ui.tlTalk, "on while the round is on")
            e.stepTalk(nowMs() + 3_600_000)
            XCTAssertTrue(e.ui.tlTalk, "and a round that is not over does not time out")
            e.endTalk()
            let t0 = nowMs()
            XCTAssertEqual(e.tlUntil - t0, TLTalk.stay, accuracy: 500)
            XCTAssertEqual(TLTalk.stay, 600_000, "ten minutes when he does nothing")
            e.stepTalk(t0 + 300_000)
            XCTAssertTrue(e.ui.tlTalk, "a long read is still there")
            e.stepTalk(t0 + TLTalk.stay + 1000)
            XCTAssertFalse(e.ui.tlTalk)
            XCTAssertFalse(e.ui.talking, "the talk goes with its place")
        }
    }

    @MainActor func testOnTheDialALongAnswerStillLingersEightSeconds() {
        let kept = HomeModule.shared.kind
        HomeModule.shared.kind = .stars
        defer { HomeModule.shared.kind = kept }
        let e = StarEngine()
        typedRound(e, chars: 300)
        e.endTalk()
        XCTAssertEqual(e.lingerUntil - nowMs(), StarEngine.linger, accuracy: 500)
    }

    @MainActor func testASpokenRoundStaysAfterHerVoiceWhileTheMicrophoneIsOpenUntilHeSpeaksAgain() {
        withTimeline { e in
            var v = VoiceShot()
            e.voiceFake = { _ in v }
            func at(_ ms: Double) { _ = e.syncVoice(ms); e.stepTalk(ms) }
            v.state = .listening; v.partial = "明天几点出门"; at(1000); at(1500)
            XCTAssertEqual(TLTalk.of(e.ui.liveTurns).heard, "明天几点出门")
            v.state = .thinking; v.partial = ""; v.transcript = "明天几点出门"; at(2000)
            v.state = .speaking; v.reply = "九点二十。"; v.full = "九点二十。"; at(3000)
            XCTAssertTrue(e.ui.tlTalk)
            v.state = .listening; v.transcript = ""; v.reply = "九点二十。"; v.full = ""; at(5000)          // her voice is done, and the microphone stays open for him
            XCTAssertEqual(e.tlUntil, 5000 + TLTalk.stay, accuracy: 1, "her voice done, the place stays as a typed answer does")
            at(65_000)
            XCTAssertTrue(e.ui.tlTalk, "a minute after her voice it is still there to read")
            XCTAssertTrue(e.ui.talking, "the talk goes on while the microphone is open")
            v.partial = "还有呢"; at(65_300)
            XCTAssertTrue(e.ui.tlTalk, "what he says next is a round of its own, and the place is his again")
            XCTAssertEqual(TLTalk.of(e.ui.liveTurns).heard, "还有呢")
            XCTAssertTrue(TLTalk.of(e.ui.liveTurns).hers.isEmpty)
        }
    }

    /// a spoken round: his words heard, then the session idle, with her words or none
    @MainActor private func spoken(_ e: StarEngine, _ words: String, answer: String = "") {
        var v = VoiceShot()
        e.voiceFake = { _ in v }
        let t = nowMs()
        func at(_ ms: Double) { _ = e.syncVoice(t + ms); e.stepTalk(t + ms) }
        v.state = .listening; v.partial = words; at(0); at(500)
        v.state = .thinking; v.partial = ""; v.transcript = words; at(1000)
        if !answer.isEmpty { v.state = .speaking; v.reply = answer; v.full = answer; at(1500) }
        v.state = .idle; at(2000)
    }

    @MainActor func testSendingHerAwayClosesHerCardAtOnce() {
        withTimeline { e in
            spoken(e, "退下，退下。")
            XCTAssertEqual(TLTalk.of(e.ui.liveTurns).heard, "退下，退下。", "his words were on the card")
            e.stepTalk(nowMs() + 50)
            XCTAssertFalse(e.ui.tlTalk, "the card closes")
            XCTAssertFalse(e.ui.talking, "and the talk is over: she is back at rest")
        }
    }

    @MainActor func testARoundThatEndsWithNothingOfHersGoesInAMoment() {
        withTimeline { e in
            spoken(e, "明天呢")
            let t = nowMs()
            XCTAssertEqual(e.tlUntil - t, TLTalk.unanswered, accuracy: 500, "not the ten minutes of an answer")
            e.stepTalk(t + 2000)
            XCTAssertTrue(e.ui.tlTalk, "his words stay a moment")
            e.stepTalk(t + TLTalk.unanswered + 500)
            XCTAssertFalse(e.ui.tlTalk, "then the card fades on its own")
        }
    }

    @MainActor func testAnAnswerAfterWordsLikeADismissalStillStays() {
        withTimeline { e in
            spoken(e, "停", answer: "好的，我停下了。")
            XCTAssertEqual(e.tlUntil - nowMs(), TLTalk.stay, accuracy: 500, "she said something: it stays as any answer does")
        }
    }

    func testWhatSendsHerAway() {
        for w in ["退下，退下。", "退下吧", "退下 退下", "晚安。", "Stop."] { XCTAssertTrue(TLTalk.dismissal(w), w) }
        for w in ["你先退下吧我跟他说", "退下，明天再说", "", "。"] { XCTAssertFalse(TLTalk.dismissal(w), w) }
    }

    // MARK: how tall her card is

    /// how tall the card is with this answer, laid out on the phone's design screen
    @MainActor private func cardHeight(_ answer: String) async throws -> Double {
        let e = StarEngine()
        let kept = HomeModule.shared.kind
        HomeModule.shared.kind = .timeline
        defer { HomeModule.shared.kind = kept }
        e.beginTalk()
        e.addTurn(his: true, "你怎么知道", heard: true)
        e.addTurn(his: false, answer)
        e.stepTalk(nowMs())
        let window = UIWindow(frame: CGRect(x: 0, y: 0, width: W, height: H))
        window.rootViewController = UIHostingController(rootView: HerWordsCard(engine: e, ui: e.ui))
        window.isHidden = false
        for _ in 0..<100 where e.ui.herCardFoot == 0 { try await Task.sleep(nanoseconds: 20_000_000) }
        try await Task.sleep(nanoseconds: 300_000_000)
        window.isHidden = true
        return try XCTUnwrap(Optional(e.ui.herCardFoot).flatMap { $0 > 0 ? $0 - HerWordsCard.top : nil })
    }

    @MainActor func testHerCardGrowsWithHerAnswerUpToNearHalfTheScreen() async throws {
        let one = try await cardHeight("能听见。")
        let three = try await cardHeight("我是从你上一句「上床喽」听出来的，不是看到的。我这边没有任何能看到你在不在床上的东西。你要是又起来了，说一声就行。")
        let long = try await cardHeight(String(repeating: "知道一部分。我运行在家里的那台树莓派上，你现在是用 iPad 跟我说话。", count: 12))
        XCTAssertLessThan(one, 100, "one line: a small card")
        XCTAssertGreaterThan(three, one + 40, "three lines are all there, not cut to one")
        XCTAssertGreaterThan(long, three)
        XCTAssertLessThanOrEqual(long, HerWordsCard.most + 1, "no taller than 45% of the screen: past it her words scroll")
        XCTAssertGreaterThan(long, HerWordsCard.most - 30)
    }

    @MainActor func testAFingerOnThePlaceHoldsItAndLiftingGivesItsTimeAgain() {
        withTimeline { e in
            typedRound(e, chars: 10)
            e.endTalk()
            e.holdLinger(true)
            e.stepTalk(nowMs() + 3_600_000)
            XCTAssertTrue(e.ui.tlTalk, "held")
            XCTAssertTrue(e.ui.talking)
            e.holdLinger(false)
            XCTAssertEqual(e.tlUntil - nowMs(), TLTalk.stay, accuracy: 500)
        }
    }

    @MainActor func testScrollingTheListOrOpeningARowReleasesAnAnswerThatIsAllThereAndNotOneStillComing() {
        withTimeline { e in
            typedRound(e, chars: 100)
            e.releaseTalk()
            e.stepTalk(nowMs() + 10)
            XCTAssertTrue(e.ui.tlTalk, "her answer is still coming")
            e.endTalk()
            e.stepTalk(nowMs() + 10)
            XCTAssertTrue(e.ui.tlTalk)
            e.releaseTalk()
            e.stepTalk(nowMs() + 10)
            XCTAssertFalse(e.ui.tlTalk, "he has gone to the day")
            XCTAssertEqual(e.lingerUntil - nowMs(), StarEngine.linger, accuracy: 500, "and the talk's rows go after it as they do under the dial, not ten minutes on")
        }
    }

    @MainActor func testAMessageThatDidNotGoOutKeepsItsPlaceUntilItIsSent() {
        withTimeline { e in
            e.beginTalk()
            let row = e.addTurn(his: true, "订位")
            e.markSend(row, .failed)
            e.unsent[row] = SentTurn(words: "订位", shown: "订位", ids: [], pics: [], about: nil, his: row, utterance: "u", taken: false)
            e.talk = nil
            e.endTalk()
            e.stepTalk(nowMs() + 600_000)
            XCTAssertTrue(e.ui.tlTalk)
            XCTAssertTrue(e.ui.talking)
        }
    }

    // MARK: no chat in the middle

    @MainActor func testOnTheTimelineNothingOpensThePanelByItself() {
        withTimeline { e in
            e.beginTalk()
            e.addTurn(his: false, "## 本周\n- 周五 10:00 周会\n- 周六 09:00 爬山")
            XCTAssertFalse(e.ui.panelOpen, "a list in her answer")
            e.measureLive(10_000)
            XCTAssertFalse(e.ui.panelOpen, "rows taller than the place under the dial")
            e.showAnswer(nil)
            XCTAssertTrue(e.ui.panelOpen, "it opens when he asks")
        }
    }

    @MainActor func testUnderTheDialRowsTallerThanItsPlaceStillOpenThePanel() {
        let kept = HomeModule.shared.kind
        HomeModule.shared.kind = .stars
        defer { HomeModule.shared.kind = kept }
        let e = StarEngine()
        e.beginTalk()
        e.measureLive(10_000)
        XCTAssertTrue(e.ui.panelOpen)
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
