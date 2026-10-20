import XCTest
@testable import Jarvis

/// Her cards (Sky/DayTimelineArc.swift): where the tiles lie, which one a finger is on, and what choosing, sliding, lifting and tapping elsewhere do.
@MainActor
final class DayTimelineArcTests: XCTestCase {
    private var cal: Cal { Cal.cur }

    private func state() -> (TimelineState, TLStubBrain, StarEngine) {
        let tl = TimelineState(), stub = TLStubBrain()
        tl.day = tl.today
        tl.fixture = .init(replies: [0: TimelineFixture.today(cal)], now: TimelineFixture.now)
        tl.brain = stub.brain
        return (tl, stub, StarEngine())
    }
    private func until(_ what: String, _ ok: () -> Bool) async {
        for _ in 0..<300 where !ok() { try? await Task.sleep(nanoseconds: 10_000_000) }
        XCTAssertTrue(ok(), what)
    }
    private func tile(_ c: CGPoint) -> CGRect { CGRect(x: c.x - TLArc.tile / 2, y: c.y - TLArc.tile / 2, width: TLArc.tile, height: TLArc.tile) }

    // MARK: where the tiles lie

    func testTheTilesLieOnAnArcFromFiveToEightyFiveDegreesBelowHerAndClearEachOtherAndHer() {
        let her = TLLayout.her
        for n in [2, 3] {
            let cs = TLArc.centers(count: n)
            XCTAssertEqual(cs.count, n)
            let angles = cs.map { atan2($0.y - her.y, $0.x - her.x) * 180 / .pi }
            XCTAssertEqual(angles.first ?? 0, 5, accuracy: 0.01)
            XCTAssertEqual(angles.last ?? 0, 85, accuracy: 0.01)
            for c in cs {
                XCTAssertEqual(hypot(c.x - her.x, c.y - her.y), TLArc.radius, accuracy: 0.01)
                XCTAssertGreaterThan(c.x - TLArc.tile / 2, 0, "on the screen")
                XCTAssertLessThan(c.x + TLArc.tile / 2, W)
                XCTAssertGreaterThan(c.y - her.y, 0, "below her, to her right")
                XCTAssertGreaterThan(c.x - her.x, 0)
                // clear of her disc (15 pt) by a margin
                let r = tile(c), nearest = CGPoint(x: min(max(her.x, r.minX), r.maxX), y: min(max(her.y, r.minY), r.maxY))
                XCTAssertGreaterThan(hypot(nearest.x - her.x, nearest.y - her.y), TLLayout.herScale * R_PS + 8)
            }
            for (a, b) in zip(cs, cs.dropFirst()) {
                XCTAssertFalse(tile(a).insetBy(dx: -4, dy: -4).intersects(tile(b)), "no two tiles touch: \(a) \(b)")
            }
        }
    }

    func testAFingerIsOnTheTileUnderItWithALittleSlopAndOnNothingElse() {
        let cs = TLArc.centers(count: 3)
        for (i, c) in cs.enumerated() {
            XCTAssertEqual(TLArc.hit(c, centers: cs), i)
            XCTAssertEqual(TLArc.hit(CGPoint(x: c.x + TLArc.tile / 2 + 3, y: c.y), centers: cs), i, "just off its edge")
            XCTAssertNil(TLArc.hit(CGPoint(x: c.x + TLArc.tile / 2 + 12, y: c.y + TLArc.tile / 2 + 12), centers: cs), "past its corner")
        }
        XCTAssertNil(TLArc.hit(TLLayout.her, centers: cs), "she is not a card")
        XCTAssertNil(TLArc.hit(CGPoint(x: 400, y: 800), centers: cs))
    }

    func testWhatSheOffersWhenHeldDown() {
        func list(_ tidy: TLTidy, shown: Bool = true) -> [String] { TLHerMenu.items(tidy: tidy, tidyShown: shown).map { "\($0.kind) \($0.title)\($0.enabled ? "" : " (off)")" } }
        XCTAssertEqual(list(.idle), ["tidy 理一下", "history 聊天记录", "today 回到今天"])
        XCTAssertEqual(list(.nothing), list(.idle), "the words of an answer are the capsule's, not the card's")
        XCTAssertEqual(list(.running), ["tidy 在理… (off)", "history 聊天记录", "today 回到今天"])
        XCTAssertEqual(list(.idle, shown: false), ["history 聊天记录", "today 回到今天"], "not on another day, and not with no brain: there is nothing to tidy")
        XCTAssertEqual(TLHerMenu.items(tidy: .idle, tidyShown: true).map(\.symbol), ["sparkles", "text.bubble", "arrow.uturn.backward"])
    }

    // MARK: sliding, lifting, tapping

    func testSlidingOntoACardLightsItWithATickAndOffItIsNone() async {
        let (tl, _, _) = state()
        tl.openArc(holding: true)
        await until("they are out") { tl.arcOpen }
        XCTAssertTrue(tl.arcMounted)
        let cs = TLArc.centers(count: tl.herItems.count)
        tl.arcFollow(cs[1])
        XCTAssertEqual(tl.arcHover, .history)
        tl.arcFollow(CGPoint(x: cs[1].x + 100, y: cs[1].y + 300))
        XCTAssertNil(tl.arcHover)
        tl.tidy = .running
        tl.arcFollow(cs[0])
        XCTAssertNil(tl.arcHover, "理一下 cannot be chosen while the brain works")
    }

    func testLiftingOnACardChoosesItAndTheyGoBack() async {
        let (tl, _, e) = state()
        XCTAssertFalse(e.ui.panelOpen)
        tl.openArc(holding: true)
        await until("out") { tl.arcOpen }
        tl.arcFollow(TLArc.centers(count: 3)[1])
        tl.arcLetGo(engine: e, cancelled: false)
        XCTAssertEqual(tl.arcPicked, .history, "it pulses first")
        await until("the history opened") { e.ui.panelOpen }
        await until("and they are back in her") { !tl.arcMounted }
        XCTAssertFalse(tl.arcOpen)
        XCTAssertNil(tl.arcPicked)
    }

    func testLiftingAnywhereElseOrTakenAwayLeavesThemOutForATap() async {
        for cancelled in [false, true] {
            let (tl, stub, e) = state()
            tl.openArc(holding: true)
            await until("out") { tl.arcOpen }
            tl.arcFollow(TLArc.centers(count: 3)[0])
            if !cancelled { tl.arcFollow(CGPoint(x: 300, y: 600)) }
            tl.arcLetGo(engine: e, cancelled: cancelled)
            try? await Task.sleep(nanoseconds: 500_000_000)
            XCTAssertTrue(tl.arcMounted && tl.arcOpen, "cancelled \(cancelled)")
            XCTAssertNil(tl.arcPicked)
            XCTAssertEqual(stub.tidies, 0)
            XCTAssertFalse(tl.arcHolding)
            tl.arcFollow(TLArc.centers(count: 3)[2])
            XCTAssertNil(tl.arcHover, "the finger is up: moving is no sliding")
        }
    }

    func testATapOnACardChoosesItAndATapElsewhereSendsThemBackWithNoChoice() async {
        let (tl, stub, e) = state()
        tl.openArc(holding: false)
        await until("out") { tl.arcOpen }
        tl.pickArc(.tidy, engine: e)
        await until("the brain was asked") { stub.tidies == 1 }
        await until("they are back") { !tl.arcMounted }
        tl.tidy = .idle
        tl.openArc(holding: false)
        await until("out again") { tl.arcOpen }
        tl.closeArc()
        XCTAssertFalse(tl.arcOpen)
        await until("back in her") { !tl.arcMounted }
        XCTAssertEqual(stub.tidies, 1, "nothing was done for the second")
        XCTAssertFalse(e.ui.panelOpen)
    }

    func testTheWayBackToTodayAndACardThatCannotBeChosenNow() async {
        let (tl, stub, e) = state()
        tl.openArc(holding: false)
        await until("out") { tl.arcOpen }
        let n = tl.request.n
        tl.pickArc(.today, engine: e)
        await until("back at now") { tl.request.n > n }
        await until("gone") { !tl.arcMounted }
        tl.tidy = .running
        tl.openArc(holding: false)
        await until("out") { tl.arcOpen }
        tl.pickArc(.tidy, engine: e)
        XCTAssertNil(tl.arcPicked, "在理…: nothing to choose")
        try? await Task.sleep(nanoseconds: 300_000_000)
        XCTAssertEqual(stub.tidies, 0)
        XCTAssertTrue(tl.arcMounted)
    }

    func testTheDayTurningSendsThemBack() async {
        let (tl, _, _) = state()
        tl.openArc(holding: false)
        await until("out") { tl.arcOpen }
        tl.letGo()
        XCTAssertFalse(tl.arcOpen)
        await until("gone") { !tl.arcMounted }
    }
}
