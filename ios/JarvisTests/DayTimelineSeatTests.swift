import XCTest
@testable import Jarvis

/// Where she is on the timeline (Sky/DayTimelineSeat.swift): at home in the top row; down where her sentence is, behind its words, when nothing has happened for a while, and back as
/// slowly as she went; home at once for her cards, and quickly on her way back when something happens; in the corner while a footprint page is open.
@MainActor
final class DayTimelineSeatTests: XCTestCase {
    private var kept = HomeModule.Kind.timeline
    override func setUp() { kept = HomeModule.shared.kind; HomeModule.shared.kind = .timeline }
    override func tearDown() { HomeModule.shared.kind = kept }

    /// `ms` of frames, 20 a second as when the sky only breathes; the time after them
    @discardableResult private func frames(_ e: StarEngine, from t: Double, for ms: Double) -> Double {
        var now = t
        while now < t + ms { now += 50; e.stepSeat(now, 0.05) }
        return now
    }
    private func away(_ e: StarEngine, from p: CGPoint) -> Double { hypot(e.seat.x - p.x, e.seat.y - p.y) }

    func testWhereSheDriftsIsHerSkyUnderTheTopRowInsideTheMarginsAndOverTheBar() {
        let r = TLSeat.radius
        for p in TLSeat.perches {
            XCTAssertGreaterThanOrEqual(p.y - r, TLLayout.lineY, "under the top row: clear of the date, its weather, the buttons and the island")
            XCTAssertLessThanOrEqual(p.y + r, TLLayout.barY - 12, "and 12 pt over the bar")
            XCTAssertGreaterThanOrEqual(p.x - r, 30, "inside the screen's margins")
            XCTAssertLessThanOrEqual(p.x + r, W - 30)
            XCTAssertGreaterThan(hypot(p.x - TLSeat.home.x, p.y - TLSeat.home.y), 3 * r, "far enough off to be seen going")
        }
    }

    func testSheRestsAtHomeAndDriftsOnlyAfterAWhileOfNothingAndBackAsSlowlyAsSheWent() throws {
        let e = StarEngine()
        var now = nowMs()
        e.seatCalmSince = now; e.seatWait = 2000
        now = frames(e, from: now, for: 1800)
        XCTAssertNil(e.wander, "not yet")
        XCTAssertLessThan(away(e, from: TLSeat.home), 0.01)
        XCTAssertEqual(e.seatR.value, TLSeat.radius, accuracy: 0.01)
        now = frames(e, from: now, for: 400)
        let off = try XCTUnwrap(e.wander)
        XCTAssertTrue(TLSeat.perches.contains(off.to))
        XCTAssertGreaterThanOrEqual(e.seatWait, TLSeat.calmFor.lowerBound, "and the next drift is a while off")
        now = frames(e, from: now, for: 4000)
        XCTAssertLessThan(away(e, from: off.to), 1, "she got there")
        now = frames(e, from: now, for: off.until - now + 100)
        XCTAssertEqual(e.wander?.to, TLSeat.home, "time up: she drifts back")
        now = frames(e, from: now, for: 500)
        XCTAssertGreaterThan(away(e, from: TLSeat.home), 20, "as slowly as she went")
        frames(e, from: now, for: TLSeat.backFor)
        XCTAssertNil(e.wander)
        XCTAssertLessThan(away(e, from: TLSeat.home), 0.5, "home")
    }

    func testOffOnADriftSheStaysWhileAnythingHappensAndOnHerWayBackSheHurries() throws {
        let e = StarEngine()
        var now = nowMs()
        e.seatCalmSince = now - 60_000; e.seatWait = 1000
        now = frames(e, from: now, for: 2500)
        let to = try XCTUnwrap(e.wander).to
        e.ui.tl.finger = TLGaze.bar(x: 200)
        now = frames(e, from: now, for: 15_000)
        XCTAssertEqual(e.wander?.to, to, "a finger on the bar: she stays where she is, where a touch on her is a touch on her")
        XCTAssertLessThan(away(e, from: to), 1)
        e.ui.tl.follow(nil, in: nil)
        now = frames(e, from: now, for: 1000)
        XCTAssertEqual(e.wander?.to, to, "a moment after")
        now = frames(e, from: now, for: 1000)
        XCTAssertEqual(e.wander?.to, TLSeat.home, "then she drifts back")
        e.ui.tl.finger = TLGaze.bar(x: 120)
        frames(e, from: now, for: 1500)
        XCTAssertNil(e.wander, "something happening on her way: she hurries")
        XCTAssertLessThan(away(e, from: TLSeat.home), 0.5)
    }

    func testHerCardsComeOutRoundHerHomeAndOffOnADriftSheHurriesBackForThem() {
        let e = StarEngine()
        var now = nowMs()
        e.seatCalmSince = now - 60_000; e.seatWait = 0
        now = frames(e, from: now, for: 5000)
        XCTAssertNotNil(e.wander)
        e.ui.tl.openArc(holding: true)
        frames(e, from: now, for: 1500)
        XCTAssertNil(e.wander)
        XCTAssertLessThan(away(e, from: TLSeat.home), 0.5, "home, where the fan is")
        e.ui.tl.closeArc()
    }

    func testHerAnswerOnShowBringsHerHomeAndKeepsHerThere() {
        let e = StarEngine()
        var now = nowMs()
        e.seatCalmSince = now - 60_000; e.seatWait = 0
        now = frames(e, from: now, for: 3000)
        XCTAssertNotNil(e.wander)
        e.ui.tlTalk = true
        now = frames(e, from: now, for: 1500)
        XCTAssertNil(e.wander, "she does not stay behind her own words")
        XCTAssertLessThan(away(e, from: TLSeat.home), 0.5)
        e.seatWait = 0
        frames(e, from: now, for: 3000)
        XCTAssertNil(e.wander, "nor go there while they are on show")
    }

    func testAFootprintPageTakesHerToItsCornerAtItsSizeAndNeverOffOnADrift() {
        let e = StarEngine()
        var now = nowMs()
        e.seatCalmSince = now - 60_000; e.seatWait = 0
        e.ui.footprintOpen = true
        now = frames(e, from: now, for: 2500)
        XCTAssertNil(e.wander)
        XCTAssertLessThan(away(e, from: TLSeat.corner), 0.5)
        XCTAssertEqual(e.seatR.value, TLSeat.cornerRadius, accuracy: 0.05)
        e.ui.footprintOpen = false
        e.seatWait = 5000
        frames(e, from: now, for: 2500)
        XCTAssertNil(e.wander, "the page shut: nothing has happened only since it did")
        XCTAssertLessThan(away(e, from: TLSeat.home), 0.5, "back to her place")
    }

    func testOnTheDialSheIsNotMovedAbout() {
        HomeModule.shared.kind = .stars
        let e = StarEngine()
        e.seatCalmSince = nowMs() - 60_000; e.seatWait = 0
        frames(e, from: nowMs(), for: 2000)
        XCTAssertNil(e.wander)
        XCTAssertLessThan(away(e, from: TLSeat.home), 0.01)
    }
}
