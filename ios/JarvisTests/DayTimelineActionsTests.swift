import XCTest
@testable import Jarvis

/// What a row does when it is opened (Sky/DayTimelineActions.swift): each action only where the row offers it, and what he has just done shows before the brain has said it. The made-up
/// day has no brain, so what he does to it stays on the phone; the brain's own routes for a rename are in PlaceRenameTests.
@MainActor
final class DayTimelineActionsTests: XCTestCase {
    private var cal: Cal { Cal.cur }

    /// the timeline on the made-up day
    private func state() -> TimelineState {
        let tl = TimelineState()
        tl.day = tl.today
        tl.fixture = .init(replies: [0: TimelineFixture.today(cal)], now: TimelineFixture.now)
        return tl
    }
    private func model(_ tl: TimelineState) throws -> DayTimelineModel { tl.model(try XCTUnwrap(tl.reply)) }
    private func entry(_ tl: TimelineState, _ id: String) throws -> TLEntry { try XCTUnwrap(try model(tl).entries.first { $0.id == id }, id) }
    private func until(_ what: String, _ ok: () -> Bool) async {
        for _ in 0..<300 where !ok() { try? await Task.sleep(nanoseconds: 10_000_000) }
        XCTAssertTrue(ok(), what)
    }

    /// the words out: records every turn the engine sends, and takes it
    @MainActor private final class Brain {
        var turns: [(text: String, about: About?)] = []
        func deliver(_ utterance: String, _ text: String, _ ids: [String], _ about: About?) async -> Delivery {
            turns.append((text, about))
            return .taken(record: nil)
        }
    }

    // MARK: ask her, open the talk, sum up the work

    func testAskHandsTheRowToHerAsTheChipOfTheNextTurnAndTheComposerTakesTheCursor() throws {
        let tl = state(), e = StarEngine()
        var began = 0
        e.beginEditing = { began += 1 }
        let talk = try entry(tl, "talk-1")
        let about = try XCTUnwrap(talk.about)
        tl.perform(.ask, on: talk, engine: e)
        XCTAssertEqual(e.ui.about, about)
        XCTAssertEqual(began, 1, "the composer opens with the keyboard up")
        // he took that chip off earlier: asking again brings it back
        e.dropAbout()
        XCTAssertNil(e.ui.about)
        tl.perform(.ask, on: talk, engine: e)
        XCTAssertEqual(e.ui.about, about)
        XCTAssertNil(e.aboutDropped)
        XCTAssertEqual(began, 2)
    }

    func testAChipHeNeverSentGoesWhenThePanelShutsButNotWhileATurnIsOnItsWayOrATalkGoes() throws {
        let tl = state(), e = StarEngine()
        let talk = try entry(tl, "talk-1")
        tl.perform(.ask, on: talk, engine: e)
        e.openPanel(quiet: true)
        XCTAssertNotNil(e.ui.about)
        e.closePanel(quiet: true)
        XCTAssertNil(e.ui.about, "nobody sent anything: the next turn is about nothing he did not mean (the open row is the chip again when one goes out)")
        XCTAssertNil(AboutNow.current)
        // the turn is on its way, or the talk has begun: the chip is its own
        for (sending, talking) in [(true, false), (false, true)] {
            tl.perform(.ask, on: talk, engine: e)
            e.openPanel(quiet: true)
            e.sendingToBrain = sending; e.ui.talking = talking
            e.closePanel(quiet: true)
            XCTAssertNotNil(e.ui.about, "sending \(sending), talking \(talking)")
            e.sendingToBrain = false; e.ui.talking = false
            e.setAbout(nil)
        }
    }

    func testAnActionTheRowDoesNotOfferDoesNothing() throws {
        let tl = state(), e = StarEngine()
        let brain = Brain()
        e.deliver = { u, t, i, a in await brain.deliver(u, t, i, a) }
        var began = 0
        e.beginEditing = { began += 1 }
        let home = try entry(tl, "stay-1"), bare = try entry(tl, "stay-5"), talk = try entry(tl, "talk-1"), ride = try entry(tl, "ride-8")
        tl.perform(.acknowledge, on: home, engine: e)
        XCTAssertTrue(tl.acked.isEmpty, "a place is not a reminder")
        tl.perform(.openTalk, on: home, engine: e)
        XCTAssertFalse(e.ui.panelOpen)
        tl.perform(.rename, on: bare, engine: e)
        XCTAssertNil(tl.renaming, "a place he does not keep cannot be renamed")
        tl.perform(.summarize, on: talk, engine: e)
        tl.perform(.ask, on: ride, engine: e)
        XCTAssertNil(e.ui.about, "the host does not take a ride as what a turn is about")
        XCTAssertEqual(began, 0)
        XCTAssertTrue(brain.turns.isEmpty)
    }

    func testOpenTalkBringsUpTheConversationAtThatTalk() throws {
        let tl = state(), e = StarEngine()
        XCTAssertFalse(e.ui.panelOpen)
        tl.perform(.openTalk, on: try entry(tl, "talk-1"), engine: e)
        XCTAssertTrue(e.ui.panelOpen)
    }

    func testSumUpSendsOneTurnWithTheWorkItAsWhatItIsAbout() async throws {
        let tl = state(), e = StarEngine()
        let brain = Brain()
        e.deliver = { u, t, i, a in await brain.deliver(u, t, i, a) }
        let work = try entry(tl, "work-demo-2")
        XCTAssertTrue(work.actions.contains(.summarize))
        e.aboutDropped = work.about?.id            // the chip he took off does not keep this from being what the turn is about
        tl.perform(.summarize, on: work, engine: e)
        await until("one turn went out") { brain.turns.count == 1 }
        XCTAssertEqual(brain.turns.first?.text, TimelineState.summarizeWords)
        XCTAssertEqual(brain.turns.first?.about, work.about)
        try? await Task.sleep(nanoseconds: 100_000_000)
        XCTAssertEqual(brain.turns.count, 1)
    }

    // MARK: a reminder that rang

    func testAReminderHeKnowsStopsWaitingAtOnceAndTheCountGoesDown() throws {
        let tl = state(), e = StarEngine()
        XCTAssertEqual(try model(tl).waiting, ["reminder-demo-2", "event-demo-1"])
        let rang = try entry(tl, "reminder-demo-2")
        XCTAssertTrue(rang.actions.contains(.acknowledge))
        tl.perform(.acknowledge, on: rang, engine: e)
        XCTAssertEqual(tl.acked, ["reminder-demo-2"])
        XCTAssertEqual(try model(tl).waiting, ["event-demo-1"], "one fewer")
        let known = try entry(tl, "reminder-demo-2")
        XCTAssertFalse(known.waiting)
        XCTAssertEqual(known.detail, "你知道了")
        XCTAssertFalse(known.actions.contains(.acknowledge), "and offers nothing more to answer")
        tl.perform(.acknowledge, on: rang, engine: e)       // twice is once
        XCTAssertEqual(tl.acked, ["reminder-demo-2"])
    }

    func testAnAnswerTheBrainDoesNotTakeMakesTheReminderWaitAgain() async throws {
        try XCTSkipIf(BrainLink.shared.hasToken, "a paired phone would send it")
        let tl = TimelineState()                            // no made-up day: it goes to the brain, which this phone has no token for
        var rang = TLEntry(id: "reminder-x", kind: .reminder, t0: 600, title: "吃药")
        rang.waiting = true; rang.actions = [.acknowledge]
        tl.acknowledge(rang)
        XCTAssertTrue(tl.acked.contains("reminder-x"), "the row says so at once")
        await until("the brain did not take it, so it waits again") { !tl.acked.contains("reminder-x") }
    }

    // MARK: a place he keeps

    func testWhatHeTypesAsANameIsCheckedBeforeAnythingIsSent() throws {
        let tl = state()
        let home = try entry(tl, "stay-1")
        tl.beginRename(home)
        XCTAssertEqual(tl.renaming, "stay-1")
        XCTAssertEqual(tl.openRow, "stay-1", "the row is opened with its field")
        tl.rename(home, to: " \n ")
        XCTAssertEqual(tl.renameNote, "名字不能是空的")
        tl.rename(home, to: String(repeating: "长", count: 81))
        XCTAssertEqual(tl.renameNote, "名字最多 80 个字")
        XCTAssertTrue(tl.renamed.isEmpty)
        XCTAssertEqual(tl.renaming, "stay-1", "the field stays for him to put it right")
        tl.rename(home, to: "  家 ")
        XCTAssertNil(tl.renaming, "the name it has: nothing to send, and the field goes")
        XCTAssertNil(tl.renameNote)
        XCTAssertTrue(tl.renamed.isEmpty)
        // a place he does not keep has no field
        tl.beginRename(try entry(tl, "stay-5"))
        XCTAssertNil(tl.renaming)
    }

    func testARenameShowsAtOnceAndCanBeTakenBack() async throws {
        let tl = state()
        let home = try entry(tl, "stay-1")
        tl.beginRename(home)
        tl.rename(home, to: "  新   家 ")
        await until("the place has its new name") { tl.renamed["p-home"] != nil }
        XCTAssertEqual(tl.renamed["p-home"]?.name, "新 家", "blanks collapsed, as the brain does")
        XCTAssertNil(tl.renaming)
        XCTAssertFalse(tl.renameBusy)
        XCTAssertEqual(try entry(tl, "stay-1").title, "新 家")
        XCTAssertEqual(try entry(tl, "stay-1").more.last, "常去的地方 · 新 家")
        // and for a few seconds he can take it back
        let u = try XCTUnwrap(tl.undo)
        XCTAssertEqual([u.place, u.was], ["p-home", "家"])
        XCTAssertNil(u.note)
        tl.undoRename()
        await until("it has its old name again") { tl.undo == nil }
        XCTAssertEqual(try entry(tl, "stay-1").title, "家")
    }

    func testARenameHeldOnThePhoneEndsWhenItsTimeIsUp() throws {
        let tl = state()
        tl.renamed["p-home"] = .init(name: "旧家", until: Date().addingTimeInterval(60))
        XCTAssertEqual(try entry(tl, "stay-1").title, "旧家")
        tl.renamed["p-home"] = .init(name: "旧家", until: Date().addingTimeInterval(-1))
        XCTAssertEqual(try entry(tl, "stay-1").title, "家", "the brain has had time to say it: what the day says is what shows")
    }

    // MARK: what waits

    func testTheWaitingCountGoesToTheFirstRowThatWaitsAndFlashesIt() async throws {
        let tl = state()
        tl.openRow = "stay-1"
        let m = try model(tl)
        let id = try XCTUnwrap(m.waiting.first)
        XCTAssertEqual(id, "reminder-demo-2")
        let before = tl.request.n
        tl.revealWaiting(in: m)
        XCTAssertNil(tl.openRow, "a row he had open is closed first, so the place is where the closed list has it")
        XCTAssertEqual(tl.request.n, before + 1)
        XCTAssertTrue(tl.request.animated)
        let e = try XCTUnwrap(m.entries.first { $0.id == id }), top = try XCTUnwrap(m.top(of: id))
        XCTAssertEqual(tl.request.y, max(0, top + e.gap - TLLayout.fadeTop - 6), accuracy: 0.01)
        var flashed = false
        for _ in 0..<400 { if tl.flash == id { flashed = true; break }; try? await Task.sleep(nanoseconds: 5_000_000) }
        XCTAssertTrue(flashed, "the row flashes where the eye arrives")
        await until("and the flash is over") { tl.flash == nil }
        // nothing waits, nothing to go to
        let calm = DayTimelineModel.build(TimelineFixture.yesterday(cal), day: -1, now: nil)
        XCTAssertTrue(calm.waiting.isEmpty)
        let n = tl.request.n
        tl.revealWaiting(in: calm)
        XCTAssertEqual(tl.request.n, n)
    }

    // MARK: the header

    func testATapOnTheWeatherSaysTheSunForAMomentAndAnotherPutsItAway() async throws {
        let tl = state()
        XCTAssertFalse(tl.sunSaid)
        tl.saySun(for: 0.15)
        XCTAssertTrue(tl.sunSaid)
        await until("it goes by itself") { !tl.sunSaid }
        tl.saySun(for: 30)
        XCTAssertTrue(tl.sunSaid)
        tl.saySun()
        XCTAssertFalse(tl.sunSaid, "a second tap puts it away at once")
        try? await Task.sleep(nanoseconds: 100_000_000)
        XCTAssertFalse(tl.sunSaid, "and the first one's time running out does not bring it back")
        // another day has its own weather: it does not stay
        tl.saySun(for: 30)
        tl.setDay(tl.today - 1)
        XCTAssertFalse(tl.sunSaid)
    }

    func testTheWeatherGivesUpItsWordThenItselfBeforeItCrowdsTheTopRow() {
        let w = TLWeather(word: "多云", symbol: "cloud.fill", high: 14, low: 8)
        XCTAssertEqual(TLDayHead.weatherFit(w, today: true, room: 175), 2, "all of it")
        XCTAssertEqual(TLDayHead.weatherFit(w, today: true, room: 140), 1, "the temperatures")
        XCTAssertEqual(TLDayHead.weatherFit(w, today: false, room: 180), 1, "the way back is wider than the tag, so the word goes first")
        XCTAssertEqual(TLDayHead.weatherFit(w, today: false, room: 130), 0, "and then all of it")
        XCTAssertEqual(TLDayHead.weatherFit(w, today: true, room: 0), 0)
        // no symbol, no word: only what there is
        XCTAssertEqual(TLDayHead.weatherFit(TLWeather(word: nil, symbol: nil, high: 21, low: nil), today: true, room: 120), 2)
    }
}
