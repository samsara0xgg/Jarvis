import XCTest
@testable import Jarvis

/// Where she is on the phone's home (Sky/DayTimelineSeat.swift): top centre on all four pages and still across switches; smaller at the top right on a page stepped into (a trip); a little
/// bigger while she listens.
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

    func testSheSitsTopCentreOnEveryPageAndStaysThereAcrossSwitches() {
        XCTAssertEqual(TLSeat.home, CGPoint(x: 220, y: 83))
        XCTAssertEqual(TLSeat.radius, 26)
        let e = StarEngine()
        var now = nowMs()
        let pages: [(String, (StarUI) -> Void)] = [
            ("今天", { _ in }),
            ("聊天", { $0.panelOpen = true }),
            ("地图", { $0.panelOpen = false; $0.footprintOpen = true }),
            ("设置", { $0.footprintOpen = false; $0.settingsOpen = true }),
            ("今天 again", { $0.settingsOpen = false }),
        ]
        for (name, go) in pages {
            go(e.ui)
            now = frames(e, from: now, for: 2500)
            XCTAssertLessThan(away(e, from: TLSeat.home), 0.5, name)
            XCTAssertEqual(e.seatR.value, TLSeat.radius, accuracy: 0.05, name)
        }
    }

    func testOnAPageSteppedIntoSheIsSmallerAtTheTopRightAndGlidesThereAndBack() {
        let e = StarEngine()
        var now = nowMs()
        e.ui.footprintOpen = true
        now = frames(e, from: now, for: 2500)
        XCTAssertLessThan(away(e, from: TLSeat.home), 0.5, "the map itself is a page of the bar: she stays")
        e.ui.footprint.openOuting = "outing-1"
        now = frames(e, from: now, for: 100)
        XCTAssertGreaterThan(away(e, from: TLSeat.corner), 20, "she glides, she does not jump")
        now = frames(e, from: now, for: 2500)
        XCTAssertLessThan(away(e, from: TLSeat.corner), 0.5)
        XCTAssertEqual(TLSeat.corner, CGPoint(x: 400, y: 88))
        XCTAssertEqual(e.seatR.value, TLSeat.cornerRadius, accuracy: 0.05)
        e.ui.footprint.openOuting = nil
        frames(e, from: now, for: 2500)
        XCTAssertLessThan(away(e, from: TLSeat.home), 0.5, "the way back from the trip")
    }

    func testSheIsBiggerWhileSheListensExceptOnAPageSteppedInto() {
        let e = StarEngine()
        var now = nowMs()
        e.voiceState = .listening
        now = frames(e, from: now, for: 2500)
        XCTAssertEqual(e.seatR.value, TLSeat.radius * TLSeat.listening, accuracy: 0.05)
        e.ui.footprintOpen = true
        e.ui.footprint.openOuting = "outing-1"
        frames(e, from: now, for: 2500)
        XCTAssertEqual(e.seatR.value, TLSeat.cornerRadius, accuracy: 0.05)
    }
}
