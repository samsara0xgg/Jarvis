import XCTest
@testable import Jarvis

/// What he says of an event his mail made (Sky/DayTimelineCommit.swift): which events offer 是我的 and 不是, that what he says is on the row (and the bar) before the brain has answered, what each
/// answer of the brain comes to, and that a refusal or no answer puts the row back and says why under it. The brain is a stub that records what it was asked (TLStubBrain); the routes
/// themselves are in DayActionsTests.
@MainActor
final class DayTimelineCommitTests: XCTestCase {
    private var cal: Cal { Cal.cur }
    private var base: Int64 { Int64(cal.origin.timeIntervalSince1970 * 1000) }
    private func at(_ minute: Double) -> Int64 { base + Int64(minute * 60_000) }
    /// the class his mail made, as the brain would name it
    private var mail: String { TimelineFixture.commitmentId(cal) }
    private let hex = "3f9a1c2e7b4d4a0c9e5f6a7b8c9d0e1f"

    private func state(_ stub: TLStubBrain) -> TimelineState {
        let tl = TimelineState()
        tl.day = tl.today
        tl.fixture = .init(replies: [0: TimelineFixture.today(cal, mail: true)], now: TimelineFixture.now)
        tl.brain = stub.brain
        return tl
    }
    private func model(_ tl: TimelineState) throws -> DayTimelineModel { tl.model(try XCTUnwrap(tl.reply)) }
    private func entry(_ tl: TimelineState, _ id: String) throws -> TLEntry { try XCTUnwrap(try model(tl).entries.first { $0.id == id }, id) }
    private func has(_ tl: TimelineState, _ id: String) throws -> Bool { try model(tl).entries.contains { $0.id == id } }
    private func until(_ what: String, _ ok: () -> Bool) async {
        for _ in 0..<300 where !ok() { try? await Task.sleep(nanoseconds: 10_000_000) }
        XCTAssertTrue(ok(), what)
    }

    /// a day of the items given, as the brain serves them (the fixture's own now: 21:50)
    private func reply(_ items: String) throws -> DayReply {
        let day = #"{"date": "2026-10-08", "tz": "\#(Cal.cur.cal.timeZone.identifier)", "start_ms": \#(base), "end_ms": \#(base + 86_400_000), "now_ms": \#(at(TimelineFixture.now)), "missing": [], "items": [\#(items)]}"#
        return try JSONDecoder().decode(DayReply.self, from: Data(day.utf8))
    }
    private func event(_ id: String, _ from: Double, _ to: Double, _ more: String) -> String {
        #"{"id": "\#(id)", "lane": "you", "kind": "event", "state": "done", "start_ms": \#(at(from)), "end_ms": \#(at(to)), \#(more)}"#
    }

    // MARK: which events offer it

    func testOnlyAnEventHisMailMadeAndHeHasNotConfirmedOffersIsMineAndIsNot() throws {
        let pair: [TLAction] = [.confirm, .deny]
        func offers(_ id: String, _ more: String, from: Double = 1320, to: Double = 1380) throws -> Bool {
            let m = DayTimelineModel.build(try reply(event(id, from, to, more)), now: TimelineFixture.now)
            let e = try XCTUnwrap(m.entries.first { $0.id == id })
            XCTAssertEqual(e.actions.contains(.confirm), e.actions.contains(.deny), "both or neither")
            XCTAssertEqual(e.commitment != nil, e.actions.contains(.confirm), "it is the hex that is asked with")
            return pair.allSatisfy(e.actions.contains)
        }
        let id = "commitment-ab12cd@2026-10-08"
        XCTAssertTrue(try offers(id, #""source": "mail", "confirmed": false"#), "not yet")
        XCTAssertTrue(try offers(id, #""source": "mail", "confirmed": false"#, from: 700, to: 760), "one that has passed and was never confirmed too: it can be cleared")
        XCTAssertTrue(try offers(id, #""source": "mail", "confirmed": false, "private": true"#), "a private one: its words are not on the screen, but it is still his to say")
        XCTAssertFalse(try offers(id, #""source": "mail", "confirmed": true"#), "he said so already")
        XCTAssertFalse(try offers(id, #""source": "mail""#), "the brain does not say")
        XCTAssertFalse(try offers(id, #""source": "said", "confirmed": false"#), "his own words")
        XCTAssertFalse(try offers(id, #""source": "plan", "confirmed": false"#))
        XCTAssertFalse(try offers(id, #""title": "日历""#), "a calendar event is no commitment")
        // an id the brain's route could not take (it must be hex between `commitment-` and `@`) is not asked with
        for odd in ["event-demo-1", "commitment-ab12", "commitment-@2026-10-08", "commitment-xyz@2026-10-08", "commitment-ab/12@2026-10-08", "commitment-" + String(repeating: "a", count: 65) + "@2026-10-08"] {
            XCTAssertFalse(try offers(odd, #""source": "mail", "confirmed": false"#), odd)
        }
        XCTAssertTrue(try offers("commitment-" + String(repeating: "A", count: 64) + "@2026-10-08", #""source": "mail", "confirmed": false"#), "64 hex digits in either case are the longest")
    }

    func testTheHexIsWhatLiesBetweenTheDashAndTheAt() {
        XCTAssertEqual(DayTimelineModel.commitmentHex("commitment-3f9a1c@2026-10-08"), "3f9a1c")
        XCTAssertEqual(DayTimelineModel.commitmentHex("commitment-0@x"), "0")
        XCTAssertEqual(DayTimelineModel.commitmentHex("commitment-ab@cd@ef"), "ab", "up to the first @")
        for bad in ["", "commitment-", "commitment-@x", "commitment-ab", "event-ab@x", "Commitment-ab@x", "commitment-ａｂ@x", "commitment-ab c@x", "commitment-g@x"] {
            XCTAssertNil(DayTimelineModel.commitmentHex(bad), bad)
        }
    }

    func testTheFixtureHoldsOneThatWaitsAndItsOldIdOffersNothing() throws {
        let tl = state(TLStubBrain())
        let e = try entry(tl, mail)
        XCTAssertEqual(e.commitment, hex)
        XCTAssertEqual(e.actions, [.confirm, .deny, .ask, .mark], "is mine, is not, ask her, star it")
        XCTAssertTrue(e.waiting)
        XCTAssertEqual(try model(tl).waiting, ["reminder-demo-2", mail])
        XCTAssertEqual(e.detail, "邮件 · 还没确认 · Studio 4")
        // the same class under a plain id (the older fixture) has nothing to confirm with
        let plain = DayTimelineModel.build(TimelineFixture.today(cal), now: TimelineFixture.now)
        XCTAssertEqual(plain.entries.first { $0.id == "event-demo-1" }?.actions, [.ask, .mark])
        XCTAssertEqual(plain.entries.first { $0.id == "event-demo-1" }?.commitment, nil)
    }

    func testTheMenuOfALongPressOffersBothAndTheButtonsSayWhatTheyDo() throws {
        let tl = state(TLStubBrain())
        let e = try entry(tl, mail)
        XCTAssertEqual(TLAction.menu.filter { e.actions.contains($0) }, [.ask, .confirm, .deny, .mark])
        XCTAssertEqual([TLAction.confirm.title, TLAction.deny.title], ["是我的", "不是"])
        XCTAssertNotEqual(TLAction.confirm.symbol, TLAction.deny.symbol)
    }

    // MARK: what he says is on the row before the brain says it

    func testWhatHeSaysIsOnTheRowAndTheBarBeforeTheBrainHasSaidIt() throws {
        let r = TimelineFixture.today(cal, mail: true)
        let said = DayTimelineModel.build(r, now: TimelineFixture.now, done: TLDone(confirmed: [mail]))
        let e = try XCTUnwrap(said.entries.first { $0.id == mail })
        XCTAssertFalse(e.waiting)
        XCTAssertEqual(said.waiting, ["reminder-demo-2"], "the count of what waits goes down")
        XCTAssertEqual(e.detail, "邮件 · 你确认了 · Studio 4", "the line says he did")
        XCTAssertEqual(e.actions, [.ask, .mark], "nothing left to say of it")
        XCTAssertEqual(said.scene.ahead.map(\.id), [mail, "reminder-demo-1", "departure-demo-1"], "it stays on the bar")
        // not his: no row, no box on the bar, and the count is down by one as well
        let not = DayTimelineModel.build(r, now: TimelineFixture.now, done: TLDone(dropped: [mail]))
        XCTAssertNil(not.entries.first { $0.id == mail })
        XCTAssertEqual(not.waiting, ["reminder-demo-2"])
        XCTAssertEqual(not.scene.ahead.map(\.id), ["reminder-demo-1", "departure-demo-1"])
        XCTAssertEqual(not.entries.count, DayTimelineModel.build(r, now: TimelineFixture.now).entries.count - 1, "and nothing else goes with it")
    }

    func testTheBrainsOwnWordThatHeConfirmedItIsSaidTheSameWay() throws {
        let said = try reply(event("commitment-ab@2026-10-08", 1320, 1380, #""source": "mail", "confirmed": true, "place": "Studio 4""#))
        let e = try XCTUnwrap(DayTimelineModel.build(said, now: TimelineFixture.now).entries.first { $0.id == "commitment-ab@2026-10-08" })
        XCTAssertEqual(e.detail, "邮件 · 你确认了 · Studio 4", "what he said a moment ago and what the brain says are one line: nothing changes when the day comes again")
        XCTAssertEqual(e.more, ["22:00–23:00"])
        XCTAssertFalse(e.waiting)
        // a private one has no line: it says so opened
        let hidden = try reply(event("commitment-ab@2026-10-08", 1320, 1380, #""source": "mail", "confirmed": false, "private": true"#))
        let h = try XCTUnwrap(DayTimelineModel.build(hidden, now: TimelineFixture.now, done: TLDone(confirmed: ["commitment-ab@2026-10-08"])).entries.first { $0.id == "commitment-ab@2026-10-08" })
        XCTAssertNil(h.detail)
        XCTAssertEqual(h.more, ["22:00–23:00", "来自邮件", "你确认了"])
    }

    // MARK: is mine

    func testIsMineIsOnTheRowAtOnceTheBrainIsToldOnceAndTheDayIsAskedForWhenItAnswers() async throws {
        let stub = TLStubBrain(); stub.hold = true
        let tl = state(stub)
        tl.confirm(try entry(tl, mail))
        let e = try entry(tl, mail)
        XCTAssertFalse(e.waiting, "the amber badge goes")
        XCTAssertEqual(try model(tl).waiting, ["reminder-demo-2"], "and the count with it")
        XCTAssertEqual(e.detail, "邮件 · 你确认了 · Studio 4")
        XCTAssertNil(e.commitment)
        XCTAssertNil(tl.notes[mail])
        await until("the brain was asked") { stub.committed.count == 1 }
        XCTAssertEqual(stub.committed.first?.hex, hex)
        XCTAssertEqual(stub.committed.first?.action, .confirm)
        XCTAssertEqual(stub.refetches, 0, "not before it has answered")
        stub.release()
        await until("the days were asked for again") { stub.refetches == 1 }
        XCTAssertEqual(try entry(tl, mail).detail, "邮件 · 你确认了 · Studio 4")
        XCTAssertNil(tl.notes[mail])
        XCTAssertEqual(stub.committed.count, 1)
    }

    func testIsMineAnsweredAsAlreadyConfirmedIsWhatHeSaid() async throws {
        let stub = TLStubBrain(); stub.commitAnswer = .already
        let tl = state(stub)
        tl.confirm(try entry(tl, mail))
        await until("the days were asked for again") { stub.refetches == 1 }
        XCTAssertFalse(try entry(tl, mail).waiting)
        XCTAssertNil(tl.notes[mail], "nothing went wrong: it is as he said")
        XCTAssertNotNil(tl.confirmed[mail], "and it stays his word until the day says it")
    }

    func testWhenTheBrainHasNoSuchCommitmentTheRowGoesWhateverHeSaidOfIt() async throws {
        for action in [CommitAction.confirm, .cancel] {
            let stub = TLStubBrain(); stub.commitAnswer = .gone
            let tl = state(stub)
            let e = try entry(tl, mail)
            if action == .confirm { tl.confirm(e) } else { tl.deny(e) }
            await until("the days were asked for again, \(action)") { stub.refetches == 1 }
            XCTAssertFalse(try has(tl, mail), "dropped on the Mac or by mail: it is not a row, \(action)")
            XCTAssertNil(tl.notes[mail])
            XCTAssertEqual(try model(tl).waiting, ["reminder-demo-2"])
        }
    }

    // MARK: is not

    func testIsNotTakesTheRowOutAndTheDashedBoxOffTheBarAndTheBrainIsToldToCancel() async throws {
        let stub = TLStubBrain(); stub.hold = true
        let tl = state(stub)
        XCTAssertTrue(try model(tl).scene.ahead.contains { $0.id == mail && $0.shape == .box })
        tl.deny(try entry(tl, mail))
        let m = try model(tl)
        XCTAssertFalse(m.entries.contains { $0.id == mail })
        XCTAssertFalse(m.scene.ahead.contains { $0.id == mail })
        XCTAssertEqual(m.waiting, ["reminder-demo-2"])
        await until("the brain was asked") { stub.committed.count == 1 }
        XCTAssertEqual(stub.committed.first?.hex, hex)
        XCTAssertEqual(stub.committed.first?.action, .cancel)
        XCTAssertEqual(stub.refetches, 0)
        stub.release()
        await until("the days were asked for again") { stub.refetches == 1 }
        XCTAssertFalse(try has(tl, mail))
    }

    func testARowThatIsTakenOutWhileOpenIsLetGoOf() throws {
        let tl = state(TLStubBrain())
        tl.openRow = mail
        tl.deny(try entry(tl, mail))
        tl.keep(only: Set(try model(tl).entries.map(\.id)))          // what the list does when its rows change
        XCTAssertNil(tl.openRow)
    }

    // MARK: what the brain will not do

    func testWhenTheBrainWillNotIsMineTheRowIsAsItWasWithAWarningAndWhyUnderIt() async throws {
        let stub = TLStubBrain(); stub.commitAnswer = .failed
        let tl = state(stub)
        tl.confirm(try entry(tl, mail))
        await until("the row says why") { tl.notes[mail] != nil }
        XCTAssertEqual(tl.notes[mail], TimelineState.commitNote)
        XCTAssertEqual(TimelineState.commitNote, "没成，稍后再试")
        let e = try entry(tl, mail)
        XCTAssertTrue(e.waiting, "it waits on him again")
        XCTAssertEqual(e.detail, "邮件 · 还没确认 · Studio 4")
        XCTAssertEqual(e.actions, [.confirm, .deny, .ask, .mark], "and can be said again")
        XCTAssertEqual(tl.openRow, mail, "the row is opened to say why")
        XCTAssertEqual(stub.refetches, 0, "nothing changed on the brain")
    }

    func testWhenTheBrainWillNotCancelTheRowComesBackAndSaysWhy() async throws {
        let stub = TLStubBrain(); stub.commitAnswer = .failed
        let tl = state(stub)
        tl.deny(try entry(tl, mail))
        XCTAssertFalse(try has(tl, mail), "gone at once")
        await until("the row says why") { tl.notes[mail] != nil }
        XCTAssertTrue(try has(tl, mail), "and back when the brain would not")
        XCTAssertEqual(tl.notes[mail], TimelineState.commitNote)
        XCTAssertEqual(tl.openRow, mail)
        XCTAssertNil(tl.dropped[mail])
        XCTAssertEqual(try model(tl).waiting, ["reminder-demo-2", mail])
    }

    // MARK: the rest

    func testWhatHeSaidLastsUntilTheBrainsOwnAnswerIsTheDays() throws {
        let tl = state(TLStubBrain())
        tl.confirmed[mail] = Date().addingTimeInterval(60)
        XCTAssertFalse(try entry(tl, mail).waiting)
        tl.confirmed[mail] = Date().addingTimeInterval(-1)
        XCTAssertTrue(try entry(tl, mail).waiting, "the hold is over: the day is what the brain says")
        tl.dropped[mail] = Date().addingTimeInterval(60)
        XCTAssertFalse(try has(tl, mail))
        tl.dropped[mail] = Date().addingTimeInterval(-1)
        XCTAssertTrue(try has(tl, mail))
        XCTAssertEqual(TimelineState.commitHolds, 120)
    }

    func testSayingItAgainWhileTheFirstIsOnItsWayAsksNothingMore() async throws {
        let stub = TLStubBrain(); stub.hold = true
        let tl = state(stub)
        let e = try entry(tl, mail)
        tl.confirm(e); tl.confirm(e); tl.deny(e)
        await until("the brain was asked") { stub.committed.count == 1 }
        try? await Task.sleep(nanoseconds: 80_000_000)
        XCTAssertEqual(stub.committed.count, 1, "one word at a time, the first")
        XCTAssertEqual(stub.committed.first?.action, .confirm)
        stub.release()
    }

    func testNothingIsAskedOfAnEventThatIsNoCommitmentOrOfARowThatIsNotOne() async throws {
        let stub = TLStubBrain()
        let tl = state(stub)
        let calendar = try entry(tl, "event-demo-2"), talk = try entry(tl, "talk-1")
        tl.confirm(calendar); tl.deny(talk)
        tl.perform(.confirm, on: calendar, engine: StarEngine())
        tl.perform(.deny, on: calendar, engine: StarEngine())
        try? await Task.sleep(nanoseconds: 80_000_000)
        XCTAssertTrue(stub.committed.isEmpty)
        XCTAssertTrue(tl.confirmed.isEmpty && tl.dropped.isEmpty)
    }

    func testTheMenuAndTheButtonsGoThroughPerform() async throws {
        let stub = TLStubBrain()
        let tl = state(stub)
        tl.perform(.confirm, on: try entry(tl, mail), engine: StarEngine())
        await until("the brain was asked") { stub.committed.count == 1 }
        XCTAssertEqual(stub.committed.first?.action, .confirm)
    }
}
