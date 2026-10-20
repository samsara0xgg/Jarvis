import XCTest
@testable import Jarvis

/// 理一下 (Sky/DayTimelineTidy.swift, Brain/DayActions.swift): the request the brain gets, what each answer of it comes to, and what the button and the days do with each.
@MainActor
final class DayTimelineTidyTests: XCTestCase {
    private typealias Stub = DayActionsTests.Stub
    private let pi = URL(string: "http://pi.test:8765")!
    private var savedTidy: URLSession!

    override func setUp() {
        let c = URLSessionConfiguration.ephemeral
        c.protocolClasses = [Stub.self]
        savedTidy = BrainClient.tidySession
        BrainClient.tidySession = URLSession(configuration: c)
        Stub.seen = []
    }
    override func tearDown() { BrainClient.tidySession = savedTidy; Stub.handler = nil }

    // MARK: the request, and the answer by its status

    func testTheRequestIsABodylessPostToTheTidyRouteWithTheTokenAndWaitsNinetySeconds() async throws {
        Stub.handler = { _ in (200, #"{"ok": true, "written": true, "outcome": "written"}"#) }
        let answer = try await BrainClient.tidyDay(bases: [pi], token: "tok")
        XCTAssertEqual(answer, .written)
        XCTAssertEqual(Stub.seen.count, 1)
        XCTAssertEqual(Stub.seen.first?.method, "POST")
        XCTAssertEqual(Stub.seen.first?.path, "/inherent/day/tidy")
        XCTAssertEqual(Stub.seen.first?.auth, "Bearer tok")
        XCTAssertEqual(Stub.seen.first?.raw, "", "no body")
        XCTAssertEqual(savedTidy.configuration.timeoutIntervalForRequest, 90, "the brain holds the answer while a model writes: this one request waits for it")
        XCTAssertEqual(BrainClient.session.configuration.timeoutIntervalForRequest, 4, "and no other request does")
    }

    func testTheBrainsAnswerByItsBody() async throws {
        let cases: [(Int, String, TidyAnswer)] = [
            (200, #"{"ok": true, "written": true, "outcome": "written"}"#, .written),
            (200, #"{"ok": true, "written": false, "outcome": "unchanged"}"#, .unchanged),
            (200, #"{"ok": true, "outcome": "unchanged"}"#, .unchanged),
            (200, #"{"ok": false, "written": false, "outcome": "model_failed"}"#, .failed),
            (200, #"{"ok": false, "written": true}"#, .failed),          // not ok is not a run
            (200, #"{"written": true}"#, .failed),
            (200, "not json", .failed),
            (200, "", .failed),
            (422, #"{"ok": true, "written": true}"#, .failed),
            (502, #"{"detail": "model failed"}"#, .failed),
        ]
        for (status, text, want) in cases {
            Stub.handler = { _ in (status, text) }
            let got = try await BrainClient.tidyDay(bases: [pi], token: "tok")
            XCTAssertEqual(got, want, "\(status) \(text)")
        }
    }

    func testABrainThatDoesNotAnswerOrHasNoRouteIsNoRun() async {
        Stub.handler = { _ in (404, #"{"detail": "Not Found"}"#) }
        do { _ = try await BrainClient.tidyDay(bases: [pi], token: "tok"); XCTFail("a brain without the route is no answer") } catch {}
        Stub.handler = { _ in (500, "") }
        do { _ = try await BrainClient.tidyDay(bases: [pi], token: "tok"); XCTFail("a broken brain is no answer") } catch {}
    }

    func testWhatTheButtonComesToAfterEachAnswer() {
        XCTAssertEqual(TLTidy.after(.written), .idle, "a new run is in: the button is as it was")
        XCTAssertEqual(TLTidy.after(.unchanged), .nothing)
        XCTAssertEqual(TLTidy.after(.failed), .failed)
        XCTAssertEqual([TLTidy.idle, .running, .nothing, .failed].map(\.words), ["理一下", "在理…", "没有要改的", "没理成，等会儿再试"])
    }

    func testWhatSheOffersWhenHeldDown() {
        func list(_ tidy: TLTidy, shown: Bool = true) -> [String] { TLHerMenu.items(tidy: tidy, tidyShown: shown).map { "\($0.kind) \($0.title)\($0.enabled ? "" : " (off)")" } }
        XCTAssertEqual(list(.idle), ["tidy 理一下", "history 聊天记录", "today 回到今天"])
        XCTAssertEqual(list(.nothing), list(.idle), "the words of an answer are the capsule's, not the menu's")
        XCTAssertEqual(list(.running), ["tidy 在理… (off)", "history 聊天记录", "today 回到今天"])
        XCTAssertEqual(list(.idle, shown: false), ["history 聊天记录", "today 回到今天"], "not on another day, and not with no brain: there is nothing to tidy")
        XCTAssertEqual(TLHerMenu.items(tidy: .idle, tidyShown: true).map(\.symbol), ["sparkles", "text.bubble", "arrow.uturn.backward"])
    }

    // MARK: the button, and the days

    private var cal: Cal { Cal.cur }
    private func state(day: Int = 0) -> (TimelineState, TLStubBrain) {
        let tl = TimelineState(), stub = TLStubBrain()
        tl.day = tl.today + day
        tl.fixture = .init(replies: [0: TimelineFixture.today(cal)], now: TimelineFixture.now)
        tl.brain = stub.brain
        tl.tidyHolds = (nothing: 0.3, failed: 0.3)
        return (tl, stub)
    }
    private func until(_ what: String, _ ok: () -> Bool) async {
        for _ in 0..<300 where !ok() { try? await Task.sleep(nanoseconds: 10_000_000) }
        XCTAssertTrue(ok(), what)
    }

    func testTheButtonIsOnlyOnTodayAndWorksWhileTheBrainTakesItsTimeAndASecondTapIsNothing() async {
        let (tl, stub) = state()
        stub.hold = true; stub.tidyAnswer = .written
        XCTAssertTrue(tl.tidyShown)
        tl.tidyDay()
        XCTAssertEqual(tl.tidy, .running)
        tl.tidyDay(); tl.tidyDay()
        await until("the brain was asked") { stub.tidies >= 1 }
        XCTAssertEqual(stub.tidies, 1, "a tap while it works asks nothing")
        XCTAssertEqual(tl.tidy, .running)
        stub.release()
        await until("the answer came") { tl.tidy == .idle }
        XCTAssertEqual(stub.tidies, 1)
        let (past, _) = state(day: -1)
        XCTAssertFalse(past.tidyShown, "the day before is not tidied from here")
    }

    func testARunWrittenAsksForTheDaysAgainAndNotesTheReadingTheBarWasDrawnFrom() async throws {
        let (tl, stub) = state()
        stub.tidyAnswer = .written
        let serial = try XCTUnwrap(tl.reply?.serial)
        tl.tidyDay()
        await until("refetched") { stub.refetches == 1 }
        XCTAssertEqual(tl.tidy, .idle)
        XCTAssertEqual(tl.tidied, serial)
        XCTAssertNil(tl.tidyHold, "a written run has no words to hold")
    }

    func testNothingToChangeSaysSoForAWhileAndAsksForNothing() async {
        let (tl, stub) = state()
        stub.tidyAnswer = .unchanged
        tl.tidyDay()
        await until("it says so") { tl.tidy == .nothing }
        XCTAssertEqual(stub.refetches, 0)
        XCTAssertNil(tl.tidied)
        await until("and goes back") { tl.tidy == .idle }
    }

    func testAFailureSaysSoForAWhileAndMayBeTriedAgain() async {
        let (tl, stub) = state()
        stub.tidyAnswer = .failed
        tl.tidyDay()
        await until("it says so") { tl.tidy == .failed }
        XCTAssertEqual(stub.refetches, 0)
        tl.tidyDay()
        await until("asked again") { stub.tidies == 2 }
        await until("and it fails again") { tl.tidy == .failed }
        await until("and goes back") { tl.tidy == .idle }
    }

    func testAnAnswerAfterHeSwipedToAnotherDayStillAsksForTodayAndLeavesTheBarAlone() async {
        let (tl, stub) = state()
        stub.hold = true; stub.tidyAnswer = .written
        tl.tidyDay()
        await until("the brain was asked") { stub.tidies == 1 }
        tl.day = tl.today - 1
        stub.release()
        await until("refetched") { stub.refetches == 1 }
        XCTAssertEqual(tl.tidy, .idle)
        XCTAssertNil(tl.tidied, "the bar on show is not today's: nothing fades on it")
    }
}
