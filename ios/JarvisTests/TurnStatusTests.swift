import XCTest
@testable import Jarvis

/// What she is doing in a turn (ADR 0228): the host's step frames, the model that keeps them against the phone's own clock, and the line to the host that
/// feeds it. The clock is passed in, so every second here is made up.
final class TurnStatusTests: XCTestCase {
    private func step(_ n: Int, _ state: StepFrame.State = .running, label: String = "查公交 · 去 UVic", turn: String = "T1", started: Int64 = 1_000, ended: Int64? = nil) -> StepFrame {
        StepFrame(turnId: turn, step: n, tool: "transit", label: label, state: state, startedMs: started, endedMs: state == .running ? nil : ended ?? started + 4_000)
    }
    private func row(_ type: String, _ payload: [String: Any] = [:], turn: String = "T1") -> RowFrame {
        RowFrame(eventType: type, eventUid: String(repeating: "a", count: 32), payload: payload.merging(["turn_id": turn]) { a, _ in a }, tsEpochMs: 0)
    }
    private func bound(at now: Double = 0) -> TurnStatus {
        var t = TurnStatus(utterance: "u1", at: now)
        t.bind(turn: "T1")
        return t
    }

    // MARK: the frames

    func testAStepFrameIsReadAsTheHostSendsItAndNothingElseIsGuessed() throws {
        let running = #"{"type":"step","turn_id":"T1","step":1,"tool":"transit","label":"查公交 · 去 UVic","state":"running","started_ms":1700000000000,"ended_ms":null}"#
        XCTAssertEqual(HostFrame(Data(running.utf8)), .step(StepFrame(turnId: "T1", step: 1, tool: "transit", label: "查公交 · 去 UVic", state: .running, startedMs: 1_700_000_000_000, endedMs: nil)))
        let failed = running.replacingOccurrences(of: #""running""#, with: #""failed""#).replacingOccurrences(of: "null", with: "1700000004000")
        XCTAssertEqual(HostFrame(Data(failed.utf8)), .step(StepFrame(turnId: "T1", step: 1, tool: "transit", label: "查公交 · 去 UVic", state: .failed, startedMs: 1_700_000_000_000, endedMs: 1_700_000_004_000)))
        XCTAssertNil(HostFrame(Data(running.replacingOccurrences(of: #""running""#, with: #""paused""#).utf8)), "a state it does not know is dropped")
        XCTAssertNil(HostFrame(Data(#"{"type":"step","turn_id":"T1","label":"x","state":"done"}"#.utf8)), "no step number")
    }

    func testATypedSayCarriesItsFilesAndAnOlderHostSeesTheSayItKnows() throws {
        let plain = try XCTUnwrap(JSONSerialization.jsonObject(with: PhoneFrame.say(utteranceId: "u", text: "hi", language: nil, confidence: nil, spoken: false).json) as? [String: Any])
        XCTAssertNil(plain["attachments"])
        let files = PhoneFrame.say(utteranceId: "u", text: "", language: nil, confidence: nil, spoken: false, attachments: ["a1", "b2"])
        let o = try XCTUnwrap(JSONSerialization.jsonObject(with: files.json) as? [String: Any])
        XCTAssertEqual(o["attachments"] as? [String], ["a1", "b2"])
        XCTAssertEqual(o["text"] as? String, "", "blank words are fine with files")
        XCTAssertEqual(PhoneFrame(files.json), files)
    }

    func testTheHostsWordsAboutFilesAreErrorsTheLinkTellsApart() {
        XCTAssertEqual(HostFrame(Data(#"{"type":"error","code":"attachment_refused","message":"文件太大了"}"#.utf8)), .error(code: "attachment_refused", message: "文件太大了"))
        XCTAssertEqual(TypedSend.of(LinkError.refused("文件太大了")), .refused("文件太大了"), "the store's sentence is shown as it is")
        XCTAssertEqual(TypedSend.of(LinkError.noAttachments), .useSubmit)
        XCTAssertEqual(TypedSend.of(LinkError.host("say's attachments are the ids of files")), .useSubmit, "an older brain: the other route")
        XCTAssertEqual(TypedSend.of(LinkError.notConnected), .useSubmit)
        XCTAssertEqual(TypedSend.of(LinkError.timeout), .refused("Jarvis 没有回应"))
    }

    func testTheConversationBringsTheStepsOfAnAnswerAndNothingWhenNoToolRan() throws {
        let json = #"{"rows":[{"seq":1,"source":"jarvis","ts":"2026-10-11T09:00:00","text":"九点二十。","steps":[{"label":"查公交 · 去 UVic","ms":5200},{"label":"上网搜","ms":300,"failed":true}],"steps_ms":5600,"steps_summary":"查了公交、搜了网页"},{"seq":2,"source":"jarvis","ts":"2026-10-11T09:01:00","text":"好。"},{"seq":3,"source":"jarvis","ts":"2026-10-11T09:02:00","text":"嗯。","steps":[]}]}"#
        let rows = try XCTUnwrap(ConversationRow.parse(Data(json.utf8)))
        XCTAssertEqual(rows[0].steps, StepsInfo(summary: "查了公交、搜了网页", totalMs: 5_600, list: [StepRecord(label: "查公交 · 去 UVic", ms: 5_200, failed: false), StepRecord(label: "上网搜", ms: 300, failed: true)]))
        XCTAssertNil(rows[1].steps)
        XCTAssertNil(rows[2].steps, "an empty list is no steps")
        XCTAssertEqual(rows[0].steps?.line, "查了公交、搜了网页 · 6 秒")
        XCTAssertEqual(rows[0].steps?.list.map(\.time), ["5 秒", "不到 1 秒"])
    }

    func testTheLineUnderAnAnswerHasNoTimeUnderHalfASecondAndCountsWhenTheBrainHasNotSummarized() {
        XCTAssertEqual(StepsInfo(summary: "查了公交", totalMs: 400, list: []).line, "查了公交")
        XCTAssertEqual(StepsInfo(summary: "", totalMs: 9_400, list: [StepRecord(label: "a", ms: 1, failed: false), StepRecord(label: "b", ms: 1, failed: false)]).line, "2 步 · 9 秒")
    }

    // MARK: the turn

    func testTheTurnGoesFromSentThroughWorkingToAnsweringAndOver() {
        var t = bound()
        XCTAssertEqual(t.phase, .sent)
        t.step(step(1), at: 1)
        XCTAssertEqual(t.phase, .working)
        t.step(step(1, .done), at: 5)
        XCTAssertEqual(t.phase, .sent, "between two tools she is thinking again")
        t.row(row("surface.response_chunk", ["text": "好", "phase": "final"]), at: 6)
        XCTAssertEqual(t.phase, .answering)
        t.row(row("surface.response_emitted", ["text": "好", "phase": "final"]), at: 7)
        XCTAssertEqual(t.phase, .over)
        XCTAssertEqual(t.answerAt, 6)
    }

    func testTheStepOnShowCountsFromTheFramesArrivalOnThePhonesClock() {
        var t = bound(at: 0)
        XCTAssertEqual(t.line(at: 0.1), StatusLine(label: nil, since: 0), "the dots, at once")
        t.step(step(1, started: 999_999_999), at: 2)             // the host's own clock is another world
        XCTAssertEqual(t.line(at: 2.01), StatusLine(label: "查公交 · 去 UVic", since: 2))
        XCTAssertEqual(t.line(at: 6.0).since, 2, "four seconds in, still counted from the frame")
    }

    func testALabelStaysOnShowForAtLeastFourHundredMillisecondsBeforeAnythingReplacesIt() {
        var t = bound()
        t.step(step(1, label: "查公交"), at: 1)
        XCTAssertEqual(t.line(at: 1.0).label, "查公交")
        t.step(step(1, .done, label: "查公交"), at: 1.1)
        t.step(step(2, label: "上网搜"), at: 1.2)
        XCTAssertEqual(t.line(at: 1.3).label, "查公交", "the second step is waited for")
        XCTAssertEqual(t.line(at: 1.39).label, "查公交")
        XCTAssertEqual(t.line(at: 1.45), StatusLine(label: "上网搜", since: 1.2), "and counts from its own frame")
        t.step(step(2, .done, label: "上网搜"), at: 1.5)
        XCTAssertEqual(t.line(at: 1.6).label, "上网搜", "the dots wait as well")
        XCTAssertNil(t.line(at: 1.9).label)
    }

    func testWithSeveralStepsRunningTheLatestOneShows() {
        var t = bound()
        t.step(step(1, label: "a"), at: 1)
        _ = t.line(at: 1)
        t.step(step(2, label: "b"), at: 1.1)
        XCTAssertEqual(t.line(at: 2).label, "b")
        t.step(step(2, .done, label: "b"), at: 2.1)
        XCTAssertEqual(t.line(at: 3).label, "a", "when the later one ends the earlier one is still going")
    }

    func testAStepFrameToldTwiceOrOutOfOrderChangesNothing() {
        var t = bound()
        t.step(step(1), at: 1)
        t.step(step(1), at: 3)                                    // the same running again
        XCTAssertEqual(t.steps[1]?.arrivedAt, 1, "its seconds do not start over")
        t.step(step(1, .done, ended: 5_000), at: 4)
        t.step(step(1, .failed), at: 5)                           // an end after its end
        t.step(step(1), at: 6)                                    // a running after its end
        XCTAssertEqual(t.steps[1]?.state, .done)
        XCTAssertEqual(t.steps[1]?.endedAt, 4)
        t.step(step(2, .done, label: "上网搜"), at: 7)                    // its end came and its start never did
        XCTAssertEqual(t.steps[2]?.state, .done)
        XCTAssertEqual(t.steps[2]?.arrivedAt, 7)
        XCTAssertEqual(t.steps.count, 2)
    }

    func testARowOfAnotherPartOfTheTurnDoesNotEndIt() {
        var t = bound()
        t.row(row("surface.response_open", ["response_id": "R1", "phase": "commentary"]), at: 1)
        t.row(row("surface.response_chunk", ["text": "我上网查查", "phase": "commentary"]), at: 2)
        XCTAssertEqual(t.leadInAt, 2)
        XCTAssertNil(t.answerAt, "a lead-in is not the answer")
        t.step(step(1), at: 2.5)
        XCTAssertEqual(t.line(at: 2.6).label, "查公交 · 去 UVic", "the steps go on after the lead-in")
        t.row(row("response.cancelled", ["response_id": "R1"]), at: 3)
        XCTAssertNotEqual(t.phase, .over, "the cancel of the lead-in is not the turn's")
        t.row(row("response.failed", ["response_id": "R2"]), at: 4)
        XCTAssertEqual(t.phase, .over)
    }

    func testWhenTheAnswerHasBegunTheStepGoesAndTheTurnsEndClosesWhatWasRunning() {
        var t = bound()
        t.step(step(1), at: 1)
        _ = t.line(at: 1)
        t.row(row("surface.response_chunk", ["text": "好", "phase": "final"]), at: 3)
        XCTAssertNil(t.line(at: 3.5).label, "no label once she is answering")
        t.end(at: 4)
        XCTAssertEqual(t.steps[1]?.state, .failed, "the host closes a step that was running at the turn's end the same way")
        t.step(step(2), at: 5)
        XCTAssertNil(t.steps[2], "nothing is added to a turn that is over")
    }

    func testTheStepsOfAnAnsweredTurnForTheLineUnderIt() {
        var t = bound()
        XCTAssertNil(t.info, "no tool, no line")
        t.step(step(1, started: 1_000), at: 1)
        t.step(step(1, .done, started: 1_000, ended: 5_200), at: 5)
        t.step(step(2, label: "上网搜", started: 5_200), at: 5)
        t.step(step(2, .failed, label: "上网搜", started: 5_200, ended: 11_300), at: 11)
        let info = t.info
        XCTAssertEqual(info?.list, [StepRecord(label: "查公交 · 去 UVic", ms: 4_200, failed: false), StepRecord(label: "上网搜", ms: 6_100, failed: true)], "durations are the host's, from one clock")
        XCTAssertEqual(info?.totalMs, 10_300)
        XCTAssertEqual(info?.summary, "", "the phone writes no summary of its own")
    }

    // MARK: the board

    @MainActor func testStepsThatBeatTheHostsSaidAreKeptAndCountFromTheirOwnArrival() {
        var now = 10.0
        let board = StatusBoard()
        board.clock = { now }
        board.begin(utterance: "u1")
        board.feed(.step(step(1)))                                // the pump was quicker than the caller awaiting `said`
        now = 12
        board.bind(utterance: "u1", turn: "T1")
        XCTAssertEqual(board.turn?.steps[1]?.arrivedAt, 10)
        now = 14
        XCTAssertEqual(board.line(), StatusLine(label: "查公交 · 去 UVic", since: 10))
    }

    @MainActor func testAStepOfAnotherTurnIsNotThisOnesAndAnOldBindIsIgnored() {
        let board = StatusBoard()
        board.clock = { 1 }
        board.begin(utterance: "u2")
        board.bind(utterance: "u2", turn: "T2")
        board.feed(.step(step(1, turn: "T1")))
        XCTAssertTrue(board.turn?.steps.isEmpty ?? false)
        board.bind(utterance: "u1", turn: "T1")                   // the host answered an older say late
        XCTAssertEqual(board.turn?.turn, "T2")
        XCTAssertEqual(board.turn?.utterance, "u2")
    }

    @MainActor func testASayThatIsSentAgainIsTheSameTurnAndANewOneStartsOver() {
        let board = StatusBoard()
        board.clock = { 1 }
        board.begin(utterance: "u1")
        board.begin(utterance: "u1")
        board.bind(utterance: "u1", turn: "T1")
        board.bind(utterance: "u1", turn: "T1")                   // the host answers a resend with the same turn
        board.feed(.step(step(1)))
        board.begin(utterance: "u1")
        XCTAssertEqual(board.turn?.steps.count, 1, "a resend keeps what it has")
        board.begin(utterance: "u2")
        XCTAssertNil(board.turn?.turn)
        XCTAssertNil(board.info)
    }

    @MainActor func testTheBoardEndsOnlyTheTurnItIsAskedTo() {
        let board = StatusBoard()
        board.clock = { 1 }
        board.begin(utterance: "u2")
        board.end(utterance: "u1")
        XCTAssertEqual(board.turn?.phase, .sent)
        board.end(utterance: "u2")
        XCTAssertEqual(board.turn?.phase, .over)
        XCTAssertNil(StatusBoard().line(), "no turn, no line")
    }

    // MARK: the line to the host

    @MainActor private final class FakeLink: VoiceLink {
        let events: AsyncStream<LinkEvent>
        let out: AsyncStream<LinkEvent>.Continuation
        var outcomes: [Result<(turnId: String?, verdict: String), LinkError>]
        private(set) var says: [(id: String, files: [String])] = []
        init(_ outcomes: [Result<(turnId: String?, verdict: String), LinkError>]) { self.outcomes = outcomes; (events, out) = AsyncStream.makeStream(of: LinkEvent.self) }
        func connect() { out.yield(.ready(device: "phone", voice: true, sampleRate: kWireRate, judges: true)) }
        func disconnect() {}
        func say(utteranceId: String, text: String, language: VoiceLang?, confidence: Double?, spoken: Bool, overHer: Bool, conversation: Bool,
                 about: [String: Any]?, attachments: [String], timeout: Double) async throws -> (turnId: String?, verdict: String) {
            says.append((utteranceId, attachments))
            return try outcomes.removeFirst().get()
        }
        func barge() {}
        func cancel() {}
        func send(_ frames: [PlayerOut]) {}
    }

    @MainActor private func line(_ outcomes: [Result<(turnId: String?, verdict: String), LinkError>]) -> (PhoneLink, FakeLink, StatusBoard) {
        let fake = FakeLink(outcomes), board = StatusBoard()
        board.clock = { 1 }
        let link = PhoneLink(board: board, make: { _ in fake })
        link.endpoint = { (URL(string: "http://host")!, "t") }
        return (link, fake, board)
    }

    @MainActor func testATypedSendThatTheHostTakesBindsTheBoardsTurn() async {
        let (link, fake, board) = line([.success(("T9", "turn"))])
        let r = await link.sendTyped(utterance: "u1", text: "hi", attachments: ["f1"], about: nil)
        XCTAssertEqual(r, .taken(turn: "T9"))
        XCTAssertEqual(fake.says.map(\.files), [["f1"]])
        XCTAssertEqual(board.turn?.turn, "T9")
    }

    @MainActor func testASayThatGetsNoAnswerIsSentOnceMoreWithTheSameIdAndThenGivenUp() async {
        let (link, fake, board) = line([.failure(.timeout), .success(("T9", "turn"))])
        let r = await link.sendTyped(utterance: "u1", text: "hi", attachments: [], about: nil)
        XCTAssertEqual(r, .taken(turn: "T9"))
        XCTAssertEqual(fake.says.map(\.id), ["u1", "u1"], "the host knows a resent say by its id and answers the same turn")
        XCTAssertEqual(board.turn?.utterance, "u1")
        let (link2, fake2, _) = line([.failure(.timeout), .failure(.timeout), .success(("T9", "turn"))])
        let r2 = await link2.sendTyped(utterance: "u1", text: "hi", attachments: [], about: nil)
        XCTAssertEqual(r2, .refused("Jarvis 没有回应"))
        XCTAssertEqual(fake2.says.count, 2, "once more, not forever")
    }

    @MainActor func testTheHostsErrorsDecideBetweenTheSentenceAndTheOtherRoute() async {
        let (a, _, _) = line([.failure(.refused("这个文件太大了"))])
        let refused = await a.sendTyped(utterance: "u1", text: "", attachments: ["f1"], about: nil)
        XCTAssertEqual(refused, .refused("这个文件太大了"))
        let (b, fakeB, _) = line([.failure(.noAttachments)])
        let none = await b.sendTyped(utterance: "u1", text: "", attachments: ["f1"], about: nil)
        XCTAssertEqual(none, .useSubmit)
        XCTAssertEqual(fakeB.says.count, 1, "asked once: the next send is the host's to refuse again")
        let (c, _, boardC) = line([.failure(.host("say needs utterance_id, text and spoken"))])
        let old = await c.sendTyped(utterance: "u1", text: "hi", attachments: [], about: nil)
        XCTAssertEqual(old, .useSubmit)
        XCTAssertEqual(boardC.turn?.utterance, "u1", "the board's turn waits for the other route to be taken")
    }

    @MainActor func testWithoutAPairingThereIsNoLineAndTheOtherRouteIsTheOnlyOne() async {
        let link = PhoneLink(board: StatusBoard(), make: { _ in FakeLink([]) })
        let r = await link.sendTyped(utterance: "u1", text: "hi", attachments: [], about: nil)
        XCTAssertEqual(r, .useSubmit)
    }

    // MARK: the rows

    @MainActor func testHerFirstWordsTakeTheTypingLinesRowAndTheyNeverShowTwo() {
        let e = StarEngine()
        e.beginTalk(base: 0)
        let his = e.addTurn(his: true, "明天几点出门")
        e.showStatus()
        e.showStatus()
        XCTAssertEqual(e.ui.liveTurns.map(\.id), [his, his + 1])
        XCTAssertNotNil(e.ui.liveTurns[1].status)
        let steps = StepsInfo(summary: "查了公交", totalMs: 4_000, list: [])
        let hers = e.addTurn(his: false, "九点二十。", leadIn: false, steps: steps)
        XCTAssertEqual(hers, his + 1, "the same row, so nothing moves")
        XCTAssertEqual(e.ui.liveTurns.count, 2)
        XCTAssertNil(e.ui.liveTurns[1].status)
        XCTAssertEqual(e.ui.liveTurns[1].text, "九点二十。")
        XCTAssertEqual(e.ui.liveTurns[1].steps, steps)
        XCTAssertNil(e.statusTurn)
        e.dropStatus()
        XCTAssertEqual(e.ui.liveTurns.count, 2, "nothing to drop once she has spoken")
    }

    @MainActor func testATurnThatEndsWithoutAWordTakesTheTypingLineAwayAndTheBoardSaysItsLineNow() {
        let e = StarEngine()
        e.beginTalk(base: 0)
        e.addTurn(his: true, "x")
        let board = StatusBoard.shared
        board.begin(utterance: "rows-test")
        e.showStatus()
        board.bind(utterance: "rows-test", turn: "TR")
        board.feed(.step(step(1, turn: "TR")), at: board.clock() - 3)
        e.syncStatus()
        XCTAssertEqual(e.ui.liveTurns[1].status?.label, "查公交 · 去 UVic")
        e.dropStatus()
        XCTAssertEqual(e.ui.liveTurns.map(\.his), [true])
        board.end()
    }

    func testTheStreamSaysTheStepsUnderHerAnswerAndNotUnderHisWordsOrALeadIn() throws {
        let json = #"{"rows":[{"seq":1,"source":"allen","ts":"2026-10-11T09:00:00","text":"几点","steps":[{"label":"a","ms":1}]},{"seq":2,"source":"jarvis","ts":"2026-10-11T09:00:01","text":"<voice>我查查。</voice>","steps":[{"label":"a","ms":1000}]},{"seq":3,"source":"jarvis","ts":"2026-10-11T09:00:05","text":"九点。","steps":[{"label":"a","ms":4000}],"steps_ms":4000,"steps_summary":"查了公交"}]}"#
        let rows = try XCTUnwrap(ConversationRow.parse(Data(json.utf8)))
        var cal = Calendar(identifier: .gregorian); cal.timeZone = TimeZone(identifier: "Asia/Shanghai")!
        let days = TalkStream.convDays(rows, pics: [:], cal: cal, now: TalkStream.time("2026-10-11T09:01:00", cal)!)
        XCTAssertEqual(days[0].rows.map { $0.steps?.summary }, [nil, nil, "查了公交"])
        let live = TalkStream.compose([], live: [LiveTurn(id: 1, his: false, text: "", status: StatusLine(label: "查公交", since: 3)), LiveTurn(id: 2, his: false, text: "好", steps: StepsInfo(summary: "x", totalMs: 1, list: []))], cal: cal, now: Date())
        XCTAssertEqual(live[0].rows.map { $0.status?.label }, ["查公交", nil])
        XCTAssertEqual(live[0].rows.map { $0.steps?.summary }, [nil, "x"])
    }
}
