import XCTest
@testable import Jarvis

/// Message reactions (ADR 0256): the rows that carry them, the frames, his taps, the overlay of frames between two reads, and the wait for a reaction-only answer.
final class ReactionsTests: XCTestCase {
    private let heart = Tapback.standard[0], up = Tapback.standard[1]

    // MARK: rows and frames

    func testRowsCarryReactionsAndAnAbsentOrEmptyOneIsNone() throws {
        let json = #"{"rows": [{"seq": 1, "id": "a", "ts": "2026-10-09T23:50:01", "source": "allen", "text": "hi", "reactions": {"jarvis": "👀", "allen": "❤️"}}, {"seq": 2, "id": "b", "ts": "2026-10-09T23:50:04", "source": "jarvis", "text": "yo", "reactions": {}}, {"seq": 3, "id": "c", "ts": "2026-10-09T23:50:05", "source": "jarvis", "text": "yo"}, {"seq": 4, "id": "d", "ts": "2026-10-09T23:50:06", "source": "jarvis", "text": "yo", "reactions": {"allen": ""}}]}"#
        let rows = try XCTUnwrap(ConversationRow.parse(Data(json.utf8)))
        XCTAssertEqual(rows[0].reactions, RowReactions(allen: "❤️", jarvis: "👀"))
        XCTAssertTrue(rows[1].reactions.isEmpty && rows[2].reactions.isEmpty && rows[3].reactions.isEmpty)
    }

    func testReactionAndSaidFramesDecode() {
        let r = HostFrame(Data(#"{"type":"reaction","target":"rec1","by":"jarvis","emoji":"👀"}"#.utf8))
        XCTAssertEqual(r, .reaction(ReactionFrame(target: "rec1", by: "jarvis", emoji: "👀")))
        XCTAssertEqual(HostFrame(Data(#"{"type":"reaction","target":"rec1","by":"allen","emoji":null}"#.utf8)), .reaction(ReactionFrame(target: "rec1", by: "allen", emoji: nil)), "taken back")
        XCTAssertNil(HostFrame(Data(#"{"type":"reaction","by":"allen","emoji":"x"}"#.utf8)), "no target is no frame")
        XCTAssertEqual(HostFrame(Data(#"{"type":"said","utterance_id":"u1","turn_id":"T1","verdict":"turn","record_id":"rec9"}"#.utf8)),
                       .said(utteranceId: "u1", turnId: "T1", verdict: "turn", recordId: "rec9"))
        XCTAssertEqual(HostFrame(Data(#"{"type":"said","utterance_id":"u1","turn_id":"T1","verdict":"turn"}"#.utf8)),
                       .said(utteranceId: "u1", turnId: "T1", verdict: "turn", recordId: nil), "an older host names no record")
    }

    // MARK: his taps

    func testTheSamePickTakesItBackAndAnotherReplacesIt() {
        XCTAssertEqual(Tapback.next(current: nil, pick: heart), heart)
        XCTAssertNil(Tapback.next(current: heart, pick: heart))
        XCTAssertEqual(Tapback.next(current: heart, pick: up), up)
    }

    func testOnlyAnEmojiIsTakenFromTheKeyboardAndAWholeOne() {
        XCTAssertEqual(Tapback.firstEmoji(in: "🙏🏽x"), "🙏🏽", "a skin tone stays with its emoji")
        XCTAssertEqual(Tapback.firstEmoji(in: "a❤️"), "❤️")
        XCTAssertEqual(Tapback.firstEmoji(in: "‼️"), "‼️")
        XCTAssertNil(Tapback.firstEmoji(in: "7"), "a digit has the emoji property but is not one")
        XCTAssertNil(Tapback.firstEmoji(in: "好"))
    }

    @MainActor func testAPickShowsAtOnceAndStaysWhenTheBrainTakesIt() async {
        let r = Reactions()
        var sent: [String?] = []
        r.send = { _, e in sent.append(e); return true }
        let ok = await r.pick(heart, on: "rec1", shown: nil)
        XCTAssertTrue(ok)
        XCTAssertEqual(r.overlay.merged(RowReactions(), target: "rec1").allen, heart)
        let again = await r.pick(heart, on: "rec1", shown: heart)
        XCTAssertTrue(again)
        XCTAssertNil(r.overlay.merged(RowReactions(allen: heart), target: "rec1").allen, "the same one again takes it back")
        XCTAssertEqual(sent, [heart, nil])
    }

    @MainActor func testAPickTheBrainRefusesIsRolledBack() async {
        let r = Reactions()
        r.send = { _, _ in false }
        let ok = await r.pick(up, on: "rec1", shown: heart)
        XCTAssertFalse(ok)
        XCTAssertEqual(r.overlay.merged(RowReactions(allen: heart), target: "rec1").allen, heart, "back to the one he had")
        let none = await r.pick(up, on: "rec2", shown: nil)
        XCTAssertFalse(none)
        XCTAssertNil(r.overlay.merged(RowReactions(), target: "rec2").allen)
    }

    // MARK: the overlay and the refreshed rows

    func testFramesLieOverTheRowsUntilAReadThatStartedAfterThemHasThem() {
        var o = ReactionOverlay()
        o.set("a", by: "jarvis", emoji: "👀")
        o.set("a", by: "allen", emoji: nil)
        o.set("b", by: "allen", emoji: heart)
        let base = RowReactions(allen: up, jarvis: nil)
        XCTAssertEqual(o.merged(base, target: "a"), RowReactions(allen: nil, jarvis: "👀"), "a frame with no emoji takes his back")
        XCTAssertEqual(o.merged(base, target: "zzz"), base, "a message no frame named is as the row has it")
        let readStarted = o.stamp
        o.set("a", by: "jarvis", emoji: heart)                       // arrived while that read was on its way
        o.settle(upTo: readStarted, targets: ["a"])                  // the read brought row a, not row b
        XCTAssertEqual(o.merged(RowReactions(jarvis: "👀"), target: "a").jarvis, heart, "the later frame is not in that read")
        XCTAssertEqual(o.merged(RowReactions(), target: "b").allen, heart, "a row the read did not bring keeps its marks")
        o.settle(upTo: o.stamp, targets: ["a", "b"])
        XCTAssertTrue(o.isEmpty)
    }

    func testTheStreamShowsTheMergedReactionsOnConversationAndLiveRows() throws {
        let cal = Calendar(identifier: .gregorian)
        let rows = [ConversationRow(seq: 1, source: "allen", ts: "2026-10-09T23:50:01", text: "hi", id: "rec1", reactions: RowReactions(jarvis: heart)),
                    ConversationRow(seq: 2, source: "jarvis", ts: "2026-10-09T23:50:04", text: "yo", id: "rec2")]
        let now = TalkStream.time("2026-10-09T23:55:00", cal)!
        let days = TalkStream.convDays(rows, cal: cal, now: now)
        XCTAssertEqual(days[0].rows.map(\.recordId), ["rec1", "rec2"])
        var o = ReactionOverlay()
        o.set("rec2", by: "allen", emoji: up)
        o.set("live1", by: "jarvis", emoji: "👀")
        var live = LiveTurn(id: 1, his: true, text: "再说", at: now)
        live.recordId = "live1"
        let all = TalkStream.compose(days, live: [live, LiveTurn(id: 2, his: false, text: "好", at: now)], overlay: o, cal: cal, now: now)
        let flat = all.flatMap(\.rows)
        XCTAssertEqual(flat.map(\.reactions), [RowReactions(jarvis: heart), RowReactions(allen: up), RowReactions(jarvis: "👀"), RowReactions()])
        XCTAssertEqual(TalkStream.compose(days, live: [], overlay: ReactionOverlay(), cal: cal, now: now), days, "no frames, no change")
        XCTAssertEqual(flat.map(TalkStream.reactable), [true, true, true, false], "a live row without a record id offers none")
    }

    func testCardsStatusLinesFailuresAndTheChipAreNotReactable() {
        func r(_ f: (inout TalkRow) -> Void) -> Bool { var row = TalkRow(id: "c1", his: false, text: "x", recordId: "rec"); f(&row); return TalkStream.reactable(row) }
        XCTAssertTrue(r { _ in })
        XCTAssertFalse(r { $0.failed = true })
        XCTAssertFalse(r { $0.status = StatusLine() })
        XCTAssertFalse(r { $0.recordId = nil })
        XCTAssertFalse(r { $0.text = "" })
    }

    // MARK: the typed wait

    @MainActor func testHerReactionOnHisLineAnswersTheWaitButTheProgramsEyesDoNot() {
        let r = Reactions()
        r.apply(ReactionFrame(target: "mine", by: "jarvis", emoji: Tapback.eyes))
        XCTAssertFalse(r.answered("mine"), "eyes say seen, not answered")
        r.apply(ReactionFrame(target: "other", by: "jarvis", emoji: heart))
        XCTAssertFalse(r.answered("mine"), "a reaction on another message is not this turn's answer")
        r.apply(ReactionFrame(target: "mine", by: "allen", emoji: heart))
        XCTAssertFalse(r.answered("mine"), "his own is not hers")
        r.apply(ReactionFrame(target: "mine", by: "jarvis", emoji: up))
        XCTAssertTrue(r.answered("mine"))
        XCTAssertFalse(r.answered(nil), "a line without a record id waits as before")
    }

    func testAReactionAloneEndsTheWaitQuietlyAndNothingElseDoes() {
        XCTAssertEqual(WaitEnd.of(failed: false, said: false, reacted: true), .quiet, "no \"她还在想\", no failure")
        XCTAssertEqual(WaitEnd.of(failed: false, said: false, reacted: false), .fallback)
        XCTAssertEqual(WaitEnd.of(failed: false, said: true, reacted: true), .spoke, "words win over a reaction")
        XCTAssertEqual(WaitEnd.of(failed: true, said: false, reacted: true), .failed)
    }
}
