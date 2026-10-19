import XCTest
@testable import Jarvis

/// What he was doing in a stretch, said in his own words (Sky/DayTimelineSayWhat.swift, `POST /inherent/day/answer`): the body the brain is sent, which rows offer the entry and which do not,
/// the hours the sheet starts with and the ones it allows, and that a refusal or no answer leaves the sheet open with the failure line while an answer closes it and asks for the days again.
/// The brain is a stub (TLStubBrain), or the client over a stubbed session (DayActionsTests.Stub); nothing here is the network.
@MainActor
final class DayTimelineSayWhatTests: XCTestCase {
    private var cal: Cal { Cal.cur }
    private var base: Int64 { Int64(cal.origin.timeIntervalSince1970 * 1000) }
    private func at(_ minute: Double) -> Int64 { base + Int64(minute * 60_000) }
    private let now = TimelineFixture.now
    private let pi = URL(string: "http://pi.test:8765")!
    private var savedSession: URLSession!, savedSlow: URLSession!

    override func setUp() {
        let c = URLSessionConfiguration.ephemeral
        c.protocolClasses = [DayActionsTests.Stub.self]
        savedSession = BrainClient.session; savedSlow = BrainClient.slowSession
        BrainClient.session = URLSession(configuration: c); BrainClient.slowSession = BrainClient.session
        DayActionsTests.Stub.seen = []
    }
    override func tearDown() { BrainClient.session = savedSession; BrainClient.slowSession = savedSlow; DayActionsTests.Stub.handler = nil }

    /// the timeline on the made-up day
    private func state(_ stub: TLStubBrain? = nil) -> TimelineState {
        let tl = TimelineState()
        tl.day = tl.today
        tl.fixture = .init(replies: [0: TimelineFixture.today(cal)], now: now)
        tl.brain = (stub ?? TLStubBrain()).brain
        return tl
    }
    private func model(_ tl: TimelineState) throws -> DayTimelineModel { tl.model(try XCTUnwrap(tl.reply)) }
    private func entry(_ tl: TimelineState, _ id: String) throws -> TLEntry { try XCTUnwrap(try model(tl).entries.first { $0.id == id }, id) }
    /// the sheet for a row of the made-up day
    private func sheet(_ tl: TimelineState, _ id: String) throws -> TLSayWhat {
        tl.beginSayWhat(try entry(tl, id))
        return try XCTUnwrap(tl.sayWhat, id)
    }
    private func until(_ what: String, _ ok: () -> Bool) async {
        for _ in 0..<300 where !ok() { try? await Task.sleep(nanoseconds: 10_000_000) }
        XCTAssertTrue(ok(), what)
    }
    private func reply(_ items: String) throws -> DayReply {
        let day = #"{"date": "2026-10-08", "tz": "\#(Cal.cur.cal.timeZone.identifier)", "start_ms": \#(base), "end_ms": \#(base + 86_400_000), "now_ms": \#(at(now)), "missing": [], "items": [\#(items)]}"#
        return try JSONDecoder().decode(DayReply.self, from: Data(day.utf8))
    }
    private func item(_ id: String, _ kind: String, _ from: Double, _ to: Double?, _ more: String = "") -> String {
        #"{"id": "\#(id)", "lane": "you", "kind": "\#(kind)", "state": "done", "start_ms": \#(at(from)), "end_ms": \#(to.map { String(at($0)) } ?? "null")\#(more.isEmpty ? "" : ", " + more)}"#
    }

    // MARK: the request

    func testTheAnswerIsAPostOfTheDayTheMillisecondsAndTheTrimmedWordsAndNothingElse() async throws {
        let stub = DayActionsTests.Stub.self
        stub.handler = { _ in (200, #"{"ok": true, "span": {"id": "doing-1", "lane": "doing", "kind": "doing", "start_ms": 1, "end_ms": 2, "title": "躺着休息", "basis": "stated"}}"#) }
        let tl = state(), s = try sheet(tl, "stay-1")
        let link = BrainLink(defaults: UserDefaults(suiteName: "saywhat-tests")!, pairing: ("tok", [pi], pi))
        let answer = try await link.call { try await BrainClient.stateSpan(day: s.day, startMs: s.ms(600), endMs: s.ms(640), text: TLSayWhat.words("  躺着休息 \n")!, bases: $0, token: $1) }
        XCTAssertEqual(answer, .done)
        XCTAssertEqual(stub.seen.map(\.method), ["POST"])
        XCTAssertEqual(stub.seen.map(\.path), ["/inherent/day/answer"])
        XCTAssertEqual(stub.seen.map(\.auth), ["Bearer tok"])
        let body = try XCTUnwrap(try JSONSerialization.jsonObject(with: Data(stub.seen[0].raw.utf8)) as? [String: Any])
        XCTAssertEqual(Set(body.keys), ["day", "start_ms", "end_ms", "text"])
        XCTAssertEqual(body["day"] as? String, try XCTUnwrap(tl.reply).date)
        XCTAssertEqual(body["start_ms"] as? Int64, at(600))
        XCTAssertEqual(body["end_ms"] as? Int64, at(640))
        XCTAssertEqual(body["text"] as? String, "躺着休息", "trimmed")
        XCTAssertNotNil(stub.seen[0].raw.range(of: #""start_ms":\d+[,}]"#, options: .regularExpression), "whole milliseconds, not a float: \(stub.seen[0].raw)")
    }

    func testTheBrainsAnswerByItsStatus() async throws {
        let stub = DayActionsTests.Stub.self
        let cases: [(Int, String, SpanAnswer)] = [
            (200, #"{"ok": true, "span": {"id": "doing-1"}}"#, .done),
            (200, "not json", .done),                                                      // it took it; how it stands is in the days asked for again
            (422, #"{"detail": "end_ms must come after start_ms"}"#, .failed),
            (502, #"{"detail": "could not be written"}"#, .failed),
        ]
        for (status, text, want) in cases {
            stub.handler = { _ in (status, text) }
            let got = try await BrainClient.stateSpan(day: "2026-10-08", startMs: 1, endMs: 2, text: "x", bases: [pi], token: "tok")
            XCTAssertEqual(got, want, "\(status)")
        }
        // a brain that has no such route answers the framework's 404: the client throws, and the timeline's closure turns that into `failed`
        stub.handler = { _ in (404, #"{"detail": "Not Found"}"#) }
        do { _ = try await BrainClient.stateSpan(day: "2026-10-08", startMs: 1, endMs: 2, text: "x", bases: [pi], token: "tok"); XCTFail("a 404 is no answer") } catch {}
    }

    // MARK: which rows

    func testOnlyAStretchThatHasBegunOffersItAndItIsTheLastOfTheActionsAndOfTheMenu() throws {
        let tl = state(), m = try model(tl)
        for id in ["sleep-0", "stay-1", "stay-13", "move-3", "ride-8", "work-demo-1", "work-demo-2", "call-demo-1", "workout-demo-1", "claude-demo-1", "code-group-stay-1"] {
            let e = try entry(tl, id)
            XCTAssertEqual(e.actions.last, .sayWhat, id)
        }
        for id in ["talk-1", "talk-2", "reminder-demo-1", "reminder-demo-2", "reminder-demo-3", "picture-pic-demo-1", "payment-demo-1", "payment-demo-2", "event-demo-1", "event-demo-2", "departure-demo-1", "now"] {
            XCTAssertFalse(try entry(tl, id).actions.contains(.sayWhat), "her rows, a moment, an event, what is planned: \(id)")
        }
        XCTAssertEqual(m.entries.filter { $0.actions.contains(.sayWhat) }.count, m.entries.filter { TLSayWhat.range(of: $0, now: now) != nil }.count)
        let stay = try entry(tl, "stay-1")
        XCTAssertEqual(stay.actions, [.rename, .ask, .mark, .sayWhat])
        XCTAssertEqual(TLAction.menu.filter { stay.actions.contains($0) }, [.ask, .rename, .mark, .sayWhat])
        XCTAssertEqual(TLAction.sayWhat.title, "这段在干嘛")
        XCTAssertFalse(Set(TLAction.menu.filter { $0 != .sayWhat }.map(\.symbol)).contains(TLAction.sayWhat.symbol))
        // a stop on the way is a row of the trip and says nothing of itself
        let pause = try XCTUnwrap(DayTimelineModel.build(TimelineFixture.fragmented(cal), now: nil).entries.first { $0.kind == .pause })
        XCTAssertFalse(pause.actions.contains(.sayWhat))
    }

    func testAStretchThatHasNotBegunOrLastsUnderAMinuteIsNotOne() throws {
        let r = try reply([item("later", "work", 1400, 1430), item("blink", "call", 600, 600.5), item("instant", "payment", 600, 600), item("begun", "work", 1300, 1400)].joined(separator: ","))
        let m = DayTimelineModel.build(r, now: now)
        func offers(_ id: String) throws -> Bool { try XCTUnwrap(m.entries.first { $0.id == id }, id).actions.contains(.sayWhat) }
        XCTAssertFalse(try offers("later"), "after the line for now: planned")
        XCTAssertFalse(try offers("blink"))
        XCTAssertFalse(try offers("instant"))
        XCTAssertTrue(try offers("begun"), "begun before now, though it was to end after")
    }

    // MARK: the hours it starts with

    func testTheHoursAreTheRowsOwnTheStartNoEarlierThanTheDayAndTheEndNoLaterThanNowOrTheDaysLastMinute() throws {
        let tl = state()
        var s = try sheet(tl, "sleep-0")
        XCTAssertEqual([s.from, s.to, s.limit], [55, 545, 1310], "the night as it ended; the end can go no later than now")
        s = try sheet(tl, "stay-13")
        XCTAssertEqual([s.from, s.to], [1130, 1310], "the place he is in goes on to now")
        // began before the day did, ends after now, ends at midnight, has no end on a day gone by
        func range(_ items: String, now: Double?, _ id: String) throws -> (from: Int, to: Int)? {
            let e = try XCTUnwrap(DayTimelineModel.build(try reply(items), now: now).entries.first { $0.id == id }, id)
            return TLSayWhat.range(of: e, now: now)
        }
        XCTAssertTrue(try range(item("n", "sleep", -120, 300), now: now, "n").map { $0 == (0, 300) } ?? false, "the start is the day's first minute")
        XCTAssertTrue(try range(item("w", "work", 1300, 1400), now: now, "w").map { $0 == (1300, 1310) } ?? false, "the end is now")
        XCTAssertTrue(try range(item("w", "work", 1380, 1440), now: nil, "w").map { $0 == (1380, 1439) } ?? false, "24:00 is the next day's")
        XCTAssertTrue(try range(item("s", "stay", 1200, nil), now: nil, "s").map { $0 == (1200, 1439) } ?? false, "a place with no end on a day gone by goes on to the day's last minute")
        // whole minutes: the seconds of a row's times are dropped
        XCTAssertTrue(try range(item("w", "work", 600.9, 640.9), now: nil, "w").map { $0 == (600, 640) } ?? false)
    }

    func testThePickersAllowAnEndAfterTheStartAndNotAfterNowAndAStartFromTheDaysBeginning() throws {
        let s = try sheet(state(), "stay-1")
        XCTAssertEqual([s.from, s.to, s.limit], [545, 685, 1310])
        XCTAssertEqual(s.starts(before: 685), 0...684)
        XCTAssertEqual(s.ends(after: 545), 546...1310)
        XCTAssertEqual(s.ends(after: 1310), 1311...1311, "never an empty range, which a picker cannot take")
        XCTAssertEqual(s.starts(before: 0), 0...0)
        XCTAssertTrue(s.fits(545, 685) && s.fits(0, 1) && s.fits(1309, 1310))
        XCTAssertFalse(s.fits(600, 600), "no length")
        XCTAssertFalse(s.fits(700, 600), "reversed")
        XCTAssertFalse(s.fits(600, 1311), "after now")
        XCTAssertFalse(s.fits(-1, 600), "before the day")
        // a minute and its moment are one another's: the brain's wall clock
        for m in [0, 1, 545, 1310, 1439] { XCTAssertEqual(s.minute(of: s.date(m)), m) }
        let r = try XCTUnwrap(state().reply)
        XCTAssertEqual(s.ms(545), r.start_ms + 545 * 60_000)
        XCTAssertEqual(s.ms(0), r.start_ms)
        // a day gone by has no now: its end is the day's last minute
        let past = state()
        past.fixture?.replies[-1] = TimelineFixture.yesterday(cal)
        past.day = past.today - 1
        let e = try XCTUnwrap(try model(past).entries.first { $0.actions.contains(.sayWhat) })
        past.beginSayWhat(e)
        XCTAssertEqual(past.sayWhat?.limit, 1439)
    }

    // MARK: his words

    func testWordsAreTrimmedAndBlankOrTooLongOnesAreNone() {
        XCTAssertEqual(TLSayWhat.words("  躺着休息 \n"), "躺着休息")
        XCTAssertNil(TLSayWhat.words(""))
        XCTAssertNil(TLSayWhat.words("  \n\t "))
        XCTAssertEqual(TLSayWhat.words(String(repeating: "字", count: 200))?.count, 200)
        XCTAssertNil(TLSayWhat.words(String(repeating: "字", count: 201)))
        XCTAssertEqual(TLSayWhat.words(" " + String(repeating: "字", count: 200) + " ")?.count, 200, "the blanks do not count")
        // the brain counts characters the way Python does, one for each scalar: a family is five
        XCTAssertNotNil(TLSayWhat.words(String(repeating: "👨‍👩‍👧", count: 40)))
        XCTAssertNil(TLSayWhat.words(String(repeating: "👨‍👩‍👧", count: 41)))
        XCTAssertEqual(TLSayWhat.capped(String(repeating: "字", count: 230)).count, 200)
        XCTAssertEqual(TLSayWhat.capped("短"), "短")
        XCTAssertEqual(TLSayWhat.capped(String(repeating: "👨‍👩‍👧", count: 41)).count, 40)
    }

    // MARK: what the sheet does with the answer

    func testWhatIsSentIsTheDayTheHoursInMillisecondsAndTheTrimmedWords() async throws {
        let stub = TLStubBrain(), tl = state(stub), s = try sheet(tl, "stay-1")
        let r = try XCTUnwrap(tl.reply)
        tl.submitSayWhat(s, from: 600, to: 640, text: "  躺着休息 \n")
        await until("it was asked") { stub.answered.count == 1 }
        XCTAssertEqual(stub.answered[0].day, r.date)
        XCTAssertEqual(stub.answered[0].startMs, r.start_ms + 600 * 60_000)
        XCTAssertEqual(stub.answered[0].endMs, r.start_ms + 640 * 60_000)
        XCTAssertEqual(stub.answered[0].text, "躺着休息")
        // exactly 200 characters go
        await until("it answered") { !tl.sayBusy }
        tl.beginSayWhat(try entry(tl, "stay-1"))
        tl.submitSayWhat(try XCTUnwrap(tl.sayWhat), from: 600, to: 640, text: String(repeating: "字", count: 200))
        await until("the second was asked") { stub.answered.count == 2 }
    }

    func testNothingTheBrainWouldRefuseIsSent() async throws {
        let stub = TLStubBrain(), tl = state(stub), s = try sheet(tl, "stay-1")
        tl.submitSayWhat(s, from: 600, to: 640, text: "")
        tl.submitSayWhat(s, from: 600, to: 640, text: " \n ")
        tl.submitSayWhat(s, from: 600, to: 640, text: String(repeating: "字", count: 201))
        tl.submitSayWhat(s, from: 640, to: 600, text: "x")
        tl.submitSayWhat(s, from: 600, to: 600, text: "x")
        tl.submitSayWhat(s, from: 600, to: 1311, text: "x")
        tl.submitSayWhat(s, from: -5, to: 600, text: "x")
        try await Task.sleep(nanoseconds: 100_000_000)
        XCTAssertTrue(stub.answered.isEmpty)
        XCTAssertNotNil(tl.sayWhat, "and the sheet is as it was")
        XCTAssertFalse(tl.sayBusy)
        XCTAssertNil(tl.sayNote)
    }

    func testWhenTheBrainTakesItTheSheetClosesAndTheDaysAreAskedForAgainWithNothingPutOnTheDay() async throws {
        let stub = TLStubBrain()
        stub.hold = true
        let tl = state(stub), s = try sheet(tl, "stay-1")
        let before = try model(tl)
        tl.submitSayWhat(s, from: 600, to: 640, text: "躺着休息")
        await until("it is on its way") { stub.answered.count == 1 }
        XCTAssertTrue(tl.sayBusy)
        tl.submitSayWhat(s, from: 600, to: 640, text: "躺着休息")           // a second tap while the first is on its way
        XCTAssertEqual(stub.answered.count, 1)
        XCTAssertNotNil(tl.sayWhat, "the sheet stays until the brain has answered")
        stub.release()
        await until("the sheet closed") { tl.sayWhat == nil }
        XCTAssertFalse(tl.sayBusy)
        XCTAssertEqual(stub.refetches, 1)
        XCTAssertNil(tl.sayNote)
        XCTAssertEqual(try model(tl), before, "the brain's reply is the truth: the phone makes no span of its own")
    }

    func testWhenTheBrainWillNotTheSheetStaysWithTheFailureLineAndTheNextTryGoesThrough() async throws {
        let stub = TLStubBrain()
        stub.spanAnswer = .failed
        let tl = state(stub), s = try sheet(tl, "stay-1")
        tl.submitSayWhat(s, from: 600, to: 640, text: "躺着休息")
        await until("it failed") { tl.sayNote != nil }
        XCTAssertEqual(tl.sayNote, "没存上，稍后再试")
        XCTAssertNotNil(tl.sayWhat)
        XCTAssertFalse(tl.sayBusy)
        XCTAssertEqual(stub.refetches, 0)
        stub.spanAnswer = .done
        tl.submitSayWhat(s, from: 600, to: 640, text: "躺着休息")
        XCTAssertNil(tl.sayNote, "the line goes when it is tried again")
        await until("the sheet closed") { tl.sayWhat == nil }
        XCTAssertEqual(stub.answered.count, 2)
        XCTAssertEqual(stub.refetches, 1)
    }

    /// the client over a stubbed session, through a pairing, into the timeline: 422 and 502 are the sheet staying open with the line, 200 is it closing
    func testA422AndA502LeaveTheSheetOpenWithTheLineAnd200ClosesIt() async throws {
        let stub = DayActionsTests.Stub.self
        let link = BrainLink(defaults: UserDefaults(suiteName: "saywhat-tests")!, pairing: ("tok", [pi], pi))
        var refetched = 0
        var brain = TLStubBrain().brain
        brain.answer = { day, startMs, endMs, text in
            do { return try await link.call { try await BrainClient.stateSpan(day: day, startMs: startMs, endMs: endMs, text: text, bases: $0, token: $1) } } catch { return .failed }
        }
        brain.refetch = { refetched += 1 }
        let tl = state()
        tl.brain = brain
        for (status, text) in [(422, #"{"detail": "that touches no span"}"#), (502, #"{"detail": "could not be written"}"#), (404, #"{"detail": "Not Found"}"#)] {
            stub.handler = { _ in (status, text) }
            tl.beginSayWhat(try entry(tl, "stay-1"))
            tl.submitSayWhat(try XCTUnwrap(tl.sayWhat), from: 600, to: 640, text: "躺着休息")
            await until("\(status): the line") { tl.sayNote != nil }
            XCTAssertNotNil(tl.sayWhat, "\(status): the sheet is open")
            XCTAssertFalse(tl.sayBusy)
            XCTAssertEqual(refetched, 0)
        }
        stub.handler = { _ in (200, #"{"ok": true, "span": {}}"#) }
        tl.submitSayWhat(try XCTUnwrap(tl.sayWhat), from: 600, to: 640, text: "躺着休息")
        await until("it closed") { tl.sayWhat == nil }
        XCTAssertEqual(refetched, 1)
        XCTAssertEqual(stub.seen.map(\.path), Array(repeating: "/inherent/day/answer", count: 4))
    }

    func testPerformingTheActionOpensTheSheetOnTheRowsHoursAndOnlyWhereTheRowOffersIt() throws {
        let tl = state(), engine = StarEngine()
        tl.perform(.sayWhat, on: try entry(tl, "talk-1"), engine: engine)
        XCTAssertNil(tl.sayWhat, "her row does not offer it")
        tl.perform(.sayWhat, on: try entry(tl, "stay-1"), engine: engine)
        let s = try XCTUnwrap(tl.sayWhat)
        XCTAssertEqual([s.id, "\(s.from)", "\(s.to)"], ["stay-1", "545", "685"])
        XCTAssertEqual(s.day, try XCTUnwrap(tl.reply).date)
    }
}
