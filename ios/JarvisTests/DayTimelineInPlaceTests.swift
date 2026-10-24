import XCTest
@testable import Jarvis

/// The phone's timeline as one page: the bar that gives every stretch room for its icon, the chips that fit the row whole, the part of the day the list shows framed on the bar, a tap on a
/// row or on the bar (a row with more to say opens where it stands, any other has its time framed for a moment), and his answer to what a stretch asks. The brain is a stub (TLStubBrain).
@MainActor
final class DayTimelineInPlaceTests: XCTestCase {
    private var cal: Cal { Cal.cur }

    private func state(_ stub: TLStubBrain? = nil, doing: Bool = false) -> TimelineState {
        let tl = TimelineState()
        tl.day = tl.today
        tl.fixture = .init(replies: [0: TimelineFixture.today(cal, doing: doing)], now: TimelineFixture.now)
        tl.brain = (stub ?? TLStubBrain()).brain
        return tl
    }
    private func model(_ tl: TimelineState) throws -> DayTimelineModel { tl.model(try XCTUnwrap(tl.reply)) }
    private func entry(_ tl: TimelineState, _ id: String) throws -> TLEntry { try XCTUnwrap(try model(tl).entries.first { $0.id == id }, id) }
    private func until(_ what: String, _ ok: () -> Bool) async {
        for _ in 0..<300 where !ok() { try? await Task.sleep(nanoseconds: 10_000_000) }
        XCTAssertTrue(ok(), what)
    }

    // MARK: the bar

    private let work = RGB(91, 140, 255)
    private func scene(_ pieces: [(Double, Double, TLPiece.Fill)], now: Double? = nil) -> TLBarScene {
        TLBarScene(span: 360...DAY, now: now, pieces: pieces.enumerated().map { TLPiece(id: "p\($0.offset)", t0: $0.element.0, t1: $0.element.1, fill: $0.element.2) }, ahead: [], ticks: [])
    }

    func testTheBarGivesEveryStretchRoomForItsIconAndSharesTheRestByTime() {
        let s = scene([(360, 960, .doing(work)), (960, 962, .doing(work)), (962, 1300, .doing(work)), (1300, 1302, .blank), (1302, 1310, .doing(work)), (1310, DAY, .ahead)], now: 1310)
        let a = TLAxis.fair(s, width: 380)
        let k = (380 - 2 * TLAxis.floor) / (600 + 338 + 2 + 130)          // the points a minute of the rest is given, once the two short stretches have theirs
        XCTAssertEqual(a.x(360), 0)
        XCTAssertEqual(a.x(960), 600 * k, accuracy: 1e-6)
        XCTAssertEqual(a.x(962) - a.x(960), TLAxis.floor, accuracy: 1e-6, "two minutes drawn wide enough for an icon")
        XCTAssertEqual(a.x(1310) - a.x(1302), TLAxis.floor, accuracy: 1e-6, "and eight")
        XCTAssertEqual(a.x(1302) - a.x(1300), 2 * k, accuracy: 1e-6, "a blank of two minutes is not lifted")
        XCTAssertEqual(a.x(DAY) - a.x(1310), 130 * k, accuracy: 1e-6, "nor what is still to come")
        XCTAssertEqual(a.x(DAY), 380, accuracy: 1e-6)
        XCTAssertEqual(a.x(661) - a.x(660), k, accuracy: 1e-6, "inside a piece time runs evenly")
        for t in [360.0, 500, 961, 1000, 1301, 1305, 1400, DAY] { XCTAssertEqual(a.minute(a.x(t)), t, accuracy: 1e-6, "\(t)") }
        XCTAssertEqual([a.x(100), a.x(2000), a.minute(-5), a.minute(400)], [0, 380, 360, DAY], "off the bar: its start or its end")
    }

    func testTheBarIsLinearWhereTheIconsWouldTakeNineTenthsOfItAndWhenItHasNothing() {
        func minutes(_ n: Int) -> TLBarScene { scene((0..<n).map { (360 + Double($0), 361 + Double($0), .doing(work)) } + [(360 + Double(n), DAY, .blank)]) }
        let linear = TLAxis.linear(360...DAY, width: 380)
        XCTAssertEqual(TLAxis.fair(minutes(21), width: 380), linear, "twenty-one stretches of a minute would take 357 of its 380 points")
        XCTAssertNotEqual(TLAxis.fair(minutes(19), width: 380), linear, "nineteen would take 323: each has its room")
        XCTAssertEqual(TLAxis.fair(scene([]), width: 380), linear)
        XCTAssertEqual(linear.x(900), 190, accuracy: 1e-9)
        XCTAssertEqual(linear.minute(190), 900, accuracy: 1e-9)
    }

    func testOnlyTheChipsThatFitTheRowWholeAreShownAndInTheirOrder() {
        XCTAssertEqual(TLChips.fitting([100, 100, 100], room: 310, gap: 6), 2, "the third would end at 312")
        XCTAssertEqual(TLChips.fitting([100, 100, 100], room: 312, gap: 6), 3, "to the point")
        XCTAssertEqual(TLChips.fitting([100, 300, 10], room: 300, gap: 6), 1, "a small one after one that does not fit is left out too: the order is kept")
        XCTAssertEqual(TLChips.fitting([400], room: 380, gap: 6), 0)
        XCTAssertEqual(TLChips.fitting([], room: 380, gap: 6), 0)
        XCTAssertGreaterThan(TLChips.faceWidth("睡", "7 小时 5 分"), TLChips.faceWidth("睡", "7"), "a chip is as wide as its words")
    }

    func testTheBarFramesThePartOfTheDayTheListShows() throws {
        let m = DayTimelineModel.build(TimelineFixture.today(cal), now: nil)
        let all = try XCTUnwrap(m.window(y: 0, room: m.height))
        XCTAssertEqual(all.lowerBound, m.span.lowerBound, "the whole list: from the bar's start")
        let top = try XCTUnwrap(m.window(y: 0, room: 300)), lower = try XCTUnwrap(m.window(y: 400, room: 300))
        XCTAssertLessThan(top.lowerBound, lower.lowerBound)
        XCTAssertLessThan(top.upperBound, lower.upperBound, "scrolled down, it is later in the day")
        XCTAssertNil(m.window(y: m.height + 100, room: 300), "nothing of the day shows")
        // today, the room under the line for now carries it past now, and what shows under that line alone begins at now
        let today = DayTimelineModel.build(TimelineFixture.today(cal), now: TimelineFixture.now)
        let foot = try XCTUnwrap(today.window(y: max(0, today.height - 300), room: 300))
        XCTAssertLessThanOrEqual(foot.lowerBound, TimelineFixture.now)
        XCTAssertGreaterThan(foot.upperBound, TimelineFixture.now)
        let under = try XCTUnwrap(today.top(of: "now")) + 40
        XCTAssertEqual(try XCTUnwrap(today.window(y: under, room: 300)).lowerBound, TimelineFixture.now, "only what is still to come shows: the frame starts at now")
    }

    func testScrollingFramesWhatTheListShowsUntilAMomentAfterItStops() async {
        let tl = state()
        tl.scrolling(true)
        XCTAssertTrue(tl.viewportOn)
        tl.scrolling(false)
        XCTAssertTrue(tl.viewportOn, "it stays a moment")
        try? await Task.sleep(nanoseconds: 600_000_000)
        tl.scrolling(true); tl.scrolling(false)
        try? await Task.sleep(nanoseconds: 600_000_000)
        XCTAssertTrue(tl.viewportOn, "scrolled again: the moment starts again")
        await until("a moment after it came to rest") { !tl.viewportOn }
    }

    // MARK: a tap

    func testATapOpensARowWithMoreToSayWhereItStandsAndASecondTapClosesIt() throws {
        let tl = state(), m = try model(tl)
        let stay = try entry(tl, "stay-1")
        XCTAssertTrue(stay.expands)
        tl.tapRow(stay, in: m, room: 564)
        XCTAssertEqual(tl.openRow, "stay-1")
        XCTAssertTrue(tl.request.unfold, "the list carries it up as it opens")
        XCTAssertEqual(tl.request.y, try XCTUnwrap(m.top(of: "stay-1")) + stay.gap - 4, accuracy: 1e-9)
        XCTAssertNil(tl.blink, "nothing is framed")
        tl.tapRow(stay, in: m, room: 564)
        XCTAssertNil(tl.openRow, "a second tap closes it")
    }

    func testATapOnARowWithNothingMoreToSayFramesItsTimeOnTheBarForAMoment() async throws {
        let tl = state(), m = try model(tl)
        let paid = try entry(tl, "payment-demo-1")
        XCTAssertFalse(paid.expands)
        tl.tapRow(paid, in: m, room: 564)
        XCTAssertNil(tl.openRow, "it does not open")
        XCTAssertEqual(tl.flash, "payment-demo-1", "it lights")
        XCTAssertEqual(tl.blink?.spans, [800...800])
        XCTAssertEqual(tl.blink?.length, TLBlink.row)
        let n = try XCTUnwrap(tl.blink?.n)
        tl.tapRow(paid, in: m, room: 564)
        XCTAssertEqual(tl.blink?.n, n + 1, "the same time framed again frames it again")
        await until("the light goes") { tl.flash == nil }
        // several sessions of Claude Code in one row: each is framed
        let sessions = try entry(tl, "code-group-stay-1")
        tl.flashRow(sessions)
        XCTAssertEqual(tl.blink?.spans, [570...605, 612...650, 656...680])
    }

    func testATapOnTheBarOpensABlockWithMoreToSayLightsTheLineOfTheOpenOneFramesAnyOtherAndPastNowGoesBackToNow() throws {
        let tl = state(doing: true), m = try model(tl)
        tl.tapBar(600, in: m, room: 564)
        XCTAssertEqual(tl.openRow, "doing-575", "the morning's work has more to say: it opens where it stands")
        tl.tapBar(670, in: m, room: 564)
        XCTAssertEqual(tl.openRow, "doing-575", "on its own block it stays open")
        XCTAssertEqual(tl.litLine, "doing-575-run-1", "and the line at that time lights")
        let run = try XCTUnwrap(m.entries.first { $0.id == "doing-575" }?.lines.first { $0.id == "doing-575-run-1" })
        XCTAssertEqual([run.title, run.detail], ["统一时间线的行高", nil], "a run in Claude Code says what it was on, not the app's name")
        XCTAssertEqual(run.mark, .claude(cloud: false), "and wears the Claude mark")
        XCTAssertEqual(tl.blink?.spans, [640...685])
        XCTAssertEqual(tl.blink?.length, TLBlink.line)
        tl.tapBar(810, in: m, room: 564)
        XCTAssertNil(tl.openRow, "lunch has nothing more to say: the open row closes")
        XCTAssertEqual(tl.flash, "doing-795", "the list goes to its row, which lights")
        XCTAssertEqual(tl.blink?.spans, [795...835])
        XCTAssertEqual(tl.request.y, max(0, try XCTUnwrap(m.top(of: "doing-795")) + (try entry(tl, "doing-795")).gap - TLLayout.fadeTop - 6), accuracy: 1e-9)
        tl.tapBar(TimelineFixture.now + 30, in: m, room: 564)
        XCTAssertEqual(tl.request.y, -1, "past now: back to where now rests")
    }

    // MARK: what a stretch asks

    func testHisAnswerToWhatAStretchAsksGoesByItsIdOneAtATimeAndIsSaidUnderItOnceTheBrainTakesIt() async throws {
        let stub = TLStubBrain()
        let tl = state(stub, doing: true)
        let rest = try entry(tl, "doing-835")
        XCTAssertEqual(rest.ask?.options, ["休息", "刷手机", "等车"])
        tl.choose(rest, choice: "刷手机", text: nil)
        await until("it is said under the row") { tl.chosen["doing-835"] == "刷手机" }
        XCTAssertEqual(stub.chosen.map(\.spanId), ["doing-835"])
        XCTAssertEqual(stub.chosen.first?.choice, "刷手机")
        XCTAssertNil(stub.chosen.first?.text)
        XCTAssertEqual(stub.chosen.first?.day, try XCTUnwrap(tl.reply).date)
        XCTAssertEqual(stub.refetches, 1, "and the day is asked for again")
        // an answer and words, or neither: nothing is sent
        tl.choose(rest, choice: "休息", text: "也在休息")
        tl.choose(rest, choice: nil, text: nil)
        XCTAssertEqual(stub.chosen.count, 1)
        // one at a time
        stub.hold = true
        tl.choose(rest, choice: "休息", text: nil)
        tl.choose(rest, choice: "等车", text: nil)
        await until("the first is on its way") { stub.chosen.count == 2 }
        XCTAssertEqual(tl.choosing, "doing-835")
        try? await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertEqual(stub.chosen.map(\.choice), ["刷手机", "休息"], "the second was not sent")
        stub.release()
        await until("its answer came") { tl.choosing == nil }
        stub.hold = false
        // a stretch nothing was recorded for is answered the same way, in his words; when the brain will not take it the row says so
        stub.spanAnswer = .failed
        let hole = try entry(tl, "doing-1130")
        XCTAssertEqual(hole.ask?.hole, true)
        tl.choose(hole, choice: nil, text: "在车上睡着了")
        await until("the row says why") { tl.notes["doing-1130"] != nil }
        XCTAssertEqual(tl.notes["doing-1130"], Words.Timeline.notNoted)
        XCTAssertNil(tl.chosen["doing-1130"])
        XCTAssertEqual(tl.openRow, "doing-1130", "opened to say it")
        XCTAssertEqual(stub.chosen.last?.spanId, "doing-1130")
        XCTAssertEqual(stub.chosen.last?.text, "在车上睡着了")
        XCTAssertEqual(stub.refetches, 2, "nothing changed on the brain")
    }
}
