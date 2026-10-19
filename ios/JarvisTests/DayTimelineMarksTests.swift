import XCTest
@testable import Jarvis

/// His star on a row (Sky/DayTimelineMarks.swift): which rows can have one, whose it is, that it is on the row before the brain has said anything, that the brain's list of the day's highlights
/// replaces the phone's own when it comes, and that a refusal or no answer puts the star back and says why under the row. The brain is a stub that records what it was asked.
@MainActor
final class DayTimelineMarksTests: XCTestCase {
    private var cal: Cal { Cal.cur }

    /// the brain, as far as marks go: what it is asked, what it answers, and (with `hold`) when
    @MainActor private final class Stub {
        var asked: [(day: String, id: String, on: Bool)] = []
        var answer: HighlightAnswer = .done(nil)
        var refetches = 0
        var hold = false
        private var waiting: CheckedContinuation<Void, Never>?
        var brain: TLBrain {
            TLBrain(mark: { [self] day, id, on in
                asked.append((day, id, on))
                if hold { await withCheckedContinuation { waiting = $0 } }
                return answer
            }, refetch: { [self] in refetches += 1 })
        }
        func release() { waiting?.resume(); waiting = nil }
    }

    private func state(_ stub: Stub) -> TimelineState {
        let tl = TimelineState()
        tl.day = tl.today
        tl.fixture = .init(replies: [0: TimelineFixture.today(cal)], now: TimelineFixture.now)
        tl.brain = stub.brain
        return tl
    }
    private func model(_ tl: TimelineState) throws -> DayTimelineModel { tl.model(try XCTUnwrap(tl.reply)) }
    private func entry(_ tl: TimelineState, _ id: String) throws -> TLEntry { try XCTUnwrap(try model(tl).entries.first { $0.id == id }, id) }
    private func date(_ tl: TimelineState) throws -> String { try XCTUnwrap(tl.reply).date }
    private func until(_ what: String, _ ok: () -> Bool) async {
        for _ in 0..<300 where !ok() { try? await Task.sleep(nanoseconds: 10_000_000) }
        XCTAssertTrue(ok(), what)
    }
    private func his(_ ids: String...) -> [Highlight] { ids.map { Highlight(id: $0, label: "", by: "him") } }

    // MARK: which rows, and whose star

    func testEveryItemOfTheDayCanBeStarredAndNothingElse() throws {
        let tl = state(Stub())
        let m = try model(tl)
        for e in m.entries { XCTAssertEqual(e.starrable, e.kind != .now && e.kind != .pause && !e.id.hasPrefix("code-group-"), e.id) }
        XCTAssertTrue(try entry(tl, "workout-demo-1").starrable, "though the host does not take a run as what a turn is about")
        XCTAssertFalse(try entry(tl, "now").starrable)
        XCTAssertFalse(try entry(tl, "code-group-stay-1").starrable, "one row for several sessions: the brain has no item of that id")
        // a stop on the way is a stay the brain has, but the row says nothing of it that could matter
        let pause = DayTimelineModel.build(TimelineFixture.fragmented(cal), now: nil).entries.first { $0.kind == .pause }
        XCTAssertFalse(try XCTUnwrap(pause).starrable)
        // an id the brain would refuse is not starred, though the row is there
        XCTAssertFalse(TLEntry(id: "a b", kind: .talk, t0: 600, title: "x").starrable)
        XCTAssertFalse(TLEntry(id: String(repeating: "a", count: 257), kind: .talk, t0: 600, title: "x").starrable)
        XCTAssertTrue(TLEntry(id: "commitment-ab12@2026-10-08", kind: .event, t0: 600, title: "x").starrable)
    }

    func testHisMarkIsAFilledStarAndHerPickIsAnOutlinedOneAndAMarkOfHisOnItsOwnWinsOverHerPick() throws {
        let tl = state(Stub())
        XCTAssertEqual(try entry(tl, "call-demo-1").lit, true)
        XCTAssertEqual(try entry(tl, "call-demo-1").hers, true, "she picked it")
        XCTAssertEqual(try model(tl).stars, [TLStar(id: "call-demo-1", t: 1000, hers: true)])
        let r = try XCTUnwrap(tl.reply)
        let mine = DayTimelineModel.build(r, now: TimelineFixture.now, done: TLDone(highlights: [Highlight(id: "call-demo-1", label: "和 Mia 聊了很久", by: "him"), Highlight(id: "talk-1", label: "", by: "her")]))
        let call = try XCTUnwrap(mine.entries.first { $0.id == "call-demo-1" }), talk = try XCTUnwrap(mine.entries.first { $0.id == "talk-1" })
        XCTAssertEqual([call.lit, call.hers, talk.lit, talk.hers], [true, false, true, true])
        XCTAssertEqual(mine.stars, [TLStar(id: "talk-1", t: 612, hers: true), TLStar(id: "call-demo-1", t: 1000, hers: false)], "in the order of the day")
        XCTAssertEqual(call.title, "和 Mia 聊了很久", "the name on a mark is the row's name")
        XCTAssertEqual(talk.title, "和她说话", "a mark with no name of its own leaves the row's")
    }

    func testWhatOffersWhatByWhoseStarItWears() throws {
        let tl = state(Stub())
        XCTAssertEqual(try entry(tl, "talk-1").actions.filter { $0 == .mark || $0 == .unmark }, [.mark], "no star: put one on")
        XCTAssertEqual(try entry(tl, "call-demo-1").actions.filter { $0 == .mark || $0 == .unmark }, [.mark, .unmark], "hers: he can make it his, or put it away")
        tl.highlights[try date(tl)] = TLHeld(value: his("talk-1"), until: Date().addingTimeInterval(60))
        XCTAssertEqual(try entry(tl, "talk-1").actions.filter { $0 == .mark || $0 == .unmark }, [.unmark], "his: only take it off")
        XCTAssertEqual(try entry(tl, "call-demo-1").lit, false, "the list the phone holds is the day's, hers too")
    }

    func testWhatTheBrainSaysTakesOverWhenTheHoldIsUp() throws {
        let tl = state(Stub())
        let day = try date(tl)
        tl.highlights[day] = TLHeld(value: his("talk-1"), until: Date().addingTimeInterval(-1))
        XCTAssertEqual(try model(tl).stars.map(\.id), ["call-demo-1"], "the hold is over: the brain's own list")
        tl.highlights[day] = TLHeld(value: his("talk-1"), until: Date().addingTimeInterval(60))
        XCTAssertEqual(try model(tl).stars.map(\.id), ["talk-1"])
    }

    // MARK: the day's list after a word

    func testTheListAfterHisWordIsWhatTheBrainKeeps() {
        let hers = Highlight(id: "h", label: "她的名字", by: "her")
        XCTAssertEqual(TLMarks.toggled([], id: "a", on: true), [Highlight(id: "a", label: "", by: "him")])
        XCTAssertEqual(TLMarks.toggled([hers], id: "h", on: true), [Highlight(id: "h", label: "她的名字", by: "him")], "hers made his keeps her name for it")
        XCTAssertEqual(TLMarks.toggled([hers], id: "h", on: false), [], "put away, though she picked it")
        XCTAssertEqual(TLMarks.toggled([Highlight(id: "a", label: "", by: "him")], id: "a", on: false), [])
        XCTAssertEqual(TLMarks.toggled([], id: "a", on: false), [], "taking off what is not there changes nothing")
        let again = [Highlight(id: "a", label: "x", by: "him")]
        XCTAssertEqual(TLMarks.toggled(again, id: "a", on: true), again, "marking what is marked changes nothing")
        // five stand and hers are in the slots he leaves: his sixth is the brain's to refuse, and hers give way to his fifth
        let four = ["a", "b", "c", "d"].map { Highlight(id: $0, label: "", by: "him") }
        let list = TLMarks.toggled(four + [Highlight(id: "x", label: "", by: "her")], id: "e", on: true)
        XCTAssertEqual(list.map(\.id), ["a", "b", "c", "d", "e"], "her pick gave way")
        XCTAssertEqual(TLMarks.toggled(four + [Highlight(id: "x", label: "", by: "her"), Highlight(id: "y", label: "", by: "her")], id: "e", on: true).map(\.id), ["a", "b", "c", "d", "e"])
        let three = ["a", "b", "c"].map { Highlight(id: $0, label: "", by: "him") }
        XCTAssertEqual(TLMarks.toggled(three + [Highlight(id: "x", label: "", by: "her"), Highlight(id: "y", label: "", by: "her")], id: "e", on: true).map(\.id), ["a", "b", "c", "x", "e"], "one slot left for hers: the first of them")
    }

    // MARK: his word, at once, and the brain's answer

    func testStarringIsOnTheRowAtOnceAndTheBrainIsToldTheDayTheItemAndTheSwitchOnce() async throws {
        let stub = Stub(); stub.hold = true
        let tl = state(stub)
        let talk = try entry(tl, "talk-1")
        tl.toggleStar(talk)
        let now = try entry(tl, "talk-1")
        XCTAssertTrue(now.lit && !now.hers, "his, before the brain has said a word")
        XCTAssertEqual(try model(tl).stars, [TLStar(id: "talk-1", t: 612), TLStar(id: "call-demo-1", t: 1000, hers: true)], "and on the bar beside hers")
        XCTAssertEqual(tl.pop["talk-1"], 1, "the star in the open row bursts")
        XCTAssertTrue(tl.markBusy)
        await until("the brain was asked") { stub.asked.count == 1 }
        XCTAssertEqual(stub.asked.first?.day, try date(tl))
        XCTAssertEqual(stub.asked.first?.id, "talk-1")
        XCTAssertEqual(stub.asked.first?.on, true)
        stub.release()
        await until("the answer was taken") { !tl.markBusy }
        XCTAssertEqual(stub.asked.count, 1)
        XCTAssertTrue(try entry(tl, "talk-1").lit)
        XCTAssertNil(tl.notes["talk-1"])
    }

    func testTheListTheBrainAnswersWithReplacesTheOneThePhoneMadeUp() async throws {
        let stub = Stub()
        stub.answer = .done([Highlight(id: "talk-1", label: "", by: "him"), Highlight(id: "stay-1", label: "家", by: "her")])
        let tl = state(stub)
        tl.setMark(try entry(tl, "talk-1"), on: true)
        await until("the answer was taken") { !tl.markBusy }
        XCTAssertEqual(try model(tl).stars.map(\.id), ["stay-1", "talk-1"], "the brain's list, in the order of the day: call-demo-1 is not among them now")
        XCTAssertEqual(try entry(tl, "stay-1").hers, true)
        XCTAssertFalse(try entry(tl, "call-demo-1").lit)
        let held = try XCTUnwrap(tl.highlights[try date(tl)])
        XCTAssertGreaterThan(held.until.timeIntervalSinceNow, TimelineState.markHolds - 5, "held again from the answer")
    }

    func testAnAnswerThatCannotBeReadLeavesTheStarWhereItIs() async throws {
        let stub = Stub()
        stub.answer = .done(nil)
        let tl = state(stub)
        tl.setMark(try entry(tl, "talk-1"), on: true)
        await until("the answer was taken") { !tl.markBusy }
        XCTAssertTrue(try entry(tl, "talk-1").lit, "taken is taken")
        XCTAssertNil(tl.notes["talk-1"])
    }

    func testAMarkTheBrainRefusesBecauseFiveStandGoesBackWithAWarningAndTheRowSaysWhy() async throws {
        let stub = Stub(); stub.answer = .full
        let tl = state(stub)
        tl.openRow = "talk-1"
        let before = try model(tl)
        tl.setMark(try entry(tl, "talk-1"), on: true)
        XCTAssertTrue(try entry(tl, "talk-1").lit, "first it is there")
        await until("the answer was taken") { !tl.markBusy }
        XCTAssertFalse(try entry(tl, "talk-1").lit, "and then it is not")
        XCTAssertEqual(try model(tl), before, "the day is as it was")
        XCTAssertEqual(tl.notes["talk-1"], "最多标 5 个，先取消一个")
        XCTAssertNil(tl.highlights[try date(tl)], "no hold of the phone's own is left")
        XCTAssertEqual(stub.refetches, 0)
    }

    func testAMarkThatDidNotGoGoesBackAndSaysSoAndOneThatHasNoItemAsksForTheDayAgain() async throws {
        for (answer, refetches) in [(HighlightAnswer.failed, 0), (.gone, 1)] {
            let stub = Stub(); stub.answer = answer
            let tl = state(stub)
            tl.openRow = "talk-1"
            tl.setMark(try entry(tl, "talk-1"), on: true)
            await until("the answer was taken") { !tl.markBusy }
            XCTAssertFalse(try entry(tl, "talk-1").lit, "\(answer)")
            XCTAssertEqual(tl.notes["talk-1"], "没标上，稍后再试", "\(answer)")
            XCTAssertEqual(stub.refetches, refetches, "\(answer)")
        }
    }

    func testAStarTakenOffHerPickGoesBackToHerPickWhenTheBrainWillNot() async throws {
        let stub = Stub(); stub.answer = .failed
        let tl = state(stub)
        tl.setMark(try entry(tl, "call-demo-1"), on: false)
        XCTAssertFalse(try entry(tl, "call-demo-1").lit, "put away at once")
        XCTAssertEqual(stub.asked.count, 0, "the brain is told from a task")
        await until("the answer was taken") { !tl.markBusy }
        XCTAssertEqual(stub.asked.map(\.on), [false])
        XCTAssertTrue(try entry(tl, "call-demo-1").lit && (try entry(tl, "call-demo-1")).hers, "hers again")
    }

    func testTheStarButtonTurnsHisMarkOnAndOffAndHerPickIntoHis() async throws {
        let stub = Stub()
        let tl = state(stub)
        tl.toggleStar(try entry(tl, "call-demo-1"))
        XCTAssertTrue(try entry(tl, "call-demo-1").lit && !(try entry(tl, "call-demo-1")).hers, "hers made his")
        await until("the answer was taken") { !tl.markBusy }
        tl.toggleStar(try entry(tl, "call-demo-1"))
        XCTAssertFalse(try entry(tl, "call-demo-1").lit, "his taken off")
        await until("the answer was taken") { !tl.markBusy }
        XCTAssertEqual(stub.asked.map(\.on), [true, false])
        XCTAssertEqual(tl.pop["call-demo-1"], 1, "only turning it on bursts")
    }

    func testWhileOneWordIsOnItsWayAnotherWaitsForIt() async throws {
        let stub = Stub(); stub.hold = true
        let tl = state(stub)
        tl.setMark(try entry(tl, "talk-1"), on: true)
        tl.setMark(try entry(tl, "stay-1"), on: true)
        XCTAssertFalse(try entry(tl, "stay-1").lit, "ignored, not queued")
        await until("the brain was asked") { stub.asked.count == 1 }
        stub.release()
        await until("the answer was taken") { !tl.markBusy }
        tl.setMark(try entry(tl, "stay-1"), on: true)
        await until("the second was asked") { stub.asked.count == 2 }
        XCTAssertEqual(stub.asked.map(\.id), ["talk-1", "stay-1"])
        stub.release()
        await until("the second was taken") { !tl.markBusy }
    }

    func testAWordFromTheMenuOpensTheRowToSayWhyItDidNotGoAndANoteGoesAfterAWhile() async throws {
        let stub = Stub(); stub.answer = .full
        let tl = state(stub), engine = StarEngine()
        XCTAssertNil(tl.openRow)
        tl.perform(.mark, on: try entry(tl, "talk-1"), engine: engine)
        await until("the answer was taken") { !tl.markBusy }
        XCTAssertEqual(tl.openRow, "talk-1", "he did not have it open: the line is under it")
        XCTAssertEqual(tl.notes["talk-1"], TimelineState.capNote)
        tl.say("x", on: "talk-1")
        tl.say("y", on: "talk-1")
        XCTAssertEqual(tl.notes["talk-1"], "y", "one line at a time")
        // what a row does not offer does nothing
        tl.perform(.unmark, on: try entry(tl, "talk-1"), engine: engine)
        XCTAssertEqual(stub.asked.count, 1, "nobody starred it, so there is no star to take off")
    }

    func testWhatIsNotAnItemIsNeverStarred() throws {
        let stub = Stub()
        let tl = state(stub)
        tl.setMark(try entry(tl, "now"), on: true)
        tl.setMark(try entry(tl, "code-group-stay-1"), on: true)
        XCTAssertFalse(tl.markBusy)
        XCTAssertTrue(stub.asked.isEmpty)
        XCTAssertTrue(tl.highlights.isEmpty)
    }
}
