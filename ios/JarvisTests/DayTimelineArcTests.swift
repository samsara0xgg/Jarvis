import XCTest
@testable import Jarvis

/// Her hold (Sky/StarFlow.swift): on the phone's home a hold on her, or a touch that slides off her, opens 聊天 with the field taking the keyboard (`beginEditing`); a page stepped into leaves her
/// alone.
@MainActor
final class DayTimelineArcTests: XCTestCase {
    private var kept = HomeModule.Kind.timeline
    override func setUp() { kept = HomeModule.shared.kind; HomeModule.shared.kind = .timeline }
    override func tearDown() { HomeModule.shared.kind = kept }

    private func home() -> (StarEngine, () -> Int) {
        let e = StarEngine()
        e.tms.value = 1
        var typed = 0
        e.beginEditing = { typed += 1 }
        return (e, { typed })
    }

    func testATouchThatSlidesOffHerGoesToTheFieldAtOnce() {
        let (e, typed) = home()
        let her = e.seat
        e.pointerDown(1, her, 0)
        e.pointerMove(1, [(CGPoint(x: her.x + 12, y: her.y + 30), 20)])
        XCTAssertEqual(typed(), 1)
        e.pointerMove(1, [(CGPoint(x: her.x + 60, y: her.y + 90), 40)])
        e.pointerUp(1, 60, cancelled: false)
        XCTAssertEqual(typed(), 1, "once: the finger going on, or lifting, asks for nothing more")
    }

    func testAHoldOnHerGoesToTheField() async {
        let (e, typed) = home()
        e.pointerDown(1, e.seat, 0)
        try? await Task.sleep(nanoseconds: 450_000_000)
        XCTAssertEqual(typed(), 1)
        e.pointerUp(1, 500, cancelled: false)
    }

    func testOnAPageSteppedIntoASlideOffHerDoesNotGoToTheField() {
        let (e, typed) = home()
        e.ui.footprintOpen = true
        e.ui.footprint.openOuting = "outing-1"
        let her = e.seat
        e.pointerDown(1, her, 0)
        e.pointerMove(1, [(CGPoint(x: her.x + 12, y: her.y + 30), 20)])
        e.pointerUp(1, 60, cancelled: false)
        XCTAssertEqual(typed(), 0)
    }
}
