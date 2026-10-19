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

    func testTheStayIsFourSecondsForARoundSaidWholeAndTimeToReadForTheRest() {
        XCTAssertEqual(TLTalk.stay(chars: 500, said: true), 4000)
        XCTAssertEqual(TLTalk.stay(chars: 0, said: false), 8000)
        XCTAssertEqual(TLTalk.stay(chars: 100, said: false), 20_000)
        XCTAssertEqual(TLTalk.stay(chars: 1000, said: false), 45_000, "at most 45 s")
        XCTAssertEqual(TLTalk.of([his("a"), hers("**一二**三"), hers("", status: StatusLine()), try! card()]).chars, 3, "her words as they show: the marks are not counted, a status and a card are no words")
    }

    @MainActor func testATypedRoundStaysAsLongAsItTakesToReadAndThenHerSentenceComesBack() {
        withTimeline { e in
            typedRound(e, chars: 100)
            e.stepTalk(nowMs())
            XCTAssertTrue(e.ui.tlTalk, "on while the round is on")
            e.stepTalk(nowMs() + 600_000)
            XCTAssertTrue(e.ui.tlTalk, "and a round that is not over does not time out")
            e.endTalk()
            let t0 = nowMs()
            XCTAssertEqual(e.tlUntil - t0, 20_000, accuracy: 500, "8 s and 0.12 s a character")
            e.stepTalk(t0 + 19_000)
            XCTAssertTrue(e.ui.tlTalk)
            e.stepTalk(t0 + 21_000)
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

    @MainActor func testASpokenRoundSaidWholeGoesFourSecondsAfterHerVoiceAndNotWithTheOpenMicrophone() {
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
            at(8900)
            XCTAssertTrue(e.ui.tlTalk, "under four seconds after her voice")
            at(9100)
            XCTAssertFalse(e.ui.tlTalk, "four seconds after")
            XCTAssertTrue(e.ui.talking, "the talk goes on while the microphone is open")
            v.partial = "还有呢"; at(9300)
            XCTAssertTrue(e.ui.tlTalk, "what he says next is a round of its own, and the place is his again")
            XCTAssertEqual(TLTalk.of(e.ui.liveTurns).heard, "还有呢")
            XCTAssertTrue(TLTalk.of(e.ui.liveTurns).hers.isEmpty)
        }
    }

    @MainActor func testASpokenRoundWithADocumentThatSaysMoreIsReadLikeATypedOne() {
        withTimeline { e in
            let doc = "## 明天\n- 15:00 二面，线上，约四十五分钟\n- 20:30 给 Mia 回电话，我到时候提醒你\n\n二面之前我会把上次面试的笔记整理出来。"
            var v = VoiceShot()
            e.voiceFake = { _ in v }
            func at(_ ms: Double) { _ = e.syncVoice(ms); e.stepTalk(ms) }
            v.state = .listening; v.partial = "明天有什么安排"; at(1000); at(1500)
            v.state = .speaking; v.partial = ""; v.transcript = "明天有什么安排"; v.reply = "明天下午三点有一个二面。"; v.full = doc; v.more = true; at(3000)
            v.reply = doc; at(4500)             // her voice ends with the last of it, as the session paces it
            v.state = .listening; v.transcript = ""; v.more = false; v.full = ""; v.reply = doc; at(5000)
            let stay = TLTalk.stay(chars: TLTalk.of(e.ui.liveTurns).chars, said: false)
            XCTAssertGreaterThan(stay, 8000)
            at(5000 + stay - 100)
            XCTAssertTrue(e.ui.tlTalk)
            at(5000 + stay + 100)
            XCTAssertFalse(e.ui.tlTalk)
        }
    }

    @MainActor func testAFingerOnThePlaceHoldsItAndLiftingGivesItsTimeAgain() {
        withTimeline { e in
            typedRound(e, chars: 10)
            e.endTalk()
            e.holdLinger(true)
            e.stepTalk(nowMs() + 600_000)
            XCTAssertTrue(e.ui.tlTalk, "held")
            XCTAssertTrue(e.ui.talking)
            e.holdLinger(false)
            XCTAssertEqual(e.tlUntil - nowMs(), TLTalk.stay(chars: 10, said: false), accuracy: 500)
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
