import XCTest
@testable import Jarvis

/// The glass bar at the foot of the phone's home (Sky/HomeBar.swift): one page at a time, the conversation being the history come up, the tab under a finger anywhere along the bar,
/// the light kept inside it, the field resting over it on the conversation, and the list fading out into it.
@MainActor
final class HomeBarTests: XCTestCase {
    private var kept = HomeModule.Kind.timeline
    override func setUp() { kept = HomeModule.shared.kind; HomeModule.shared.kind = .timeline }
    override func tearDown() { HomeModule.shared.kind = kept }

    func testThePageIsOneOfTheFourAndShowingOneLetsTheOthersGo() {
        let e = StarEngine(), ui = e.ui
        XCTAssertEqual(ui.page, .day)
        e.showPage(.map)
        XCTAssertTrue(ui.footprintOpen); XCTAssertFalse(ui.settingsOpen); XCTAssertFalse(ui.panelOpen)
        e.showPage(.talk)
        XCTAssertTrue(ui.panelOpen, "the conversation is the history come up"); XCTAssertFalse(ui.footprintOpen)
        XCTAssertEqual(ui.page, .talk)
        e.showPage(.settings)
        XCTAssertFalse(ui.panelOpen, "the history goes for another page"); XCTAssertTrue(ui.settingsOpen)
        XCTAssertEqual(ui.page, .settings)
        e.showPage(.day)
        XCTAssertFalse(ui.footprintOpen || ui.settingsOpen || ui.panelOpen)
        e.openPanel(quiet: true)
        XCTAssertEqual(ui.page, .talk, "the history come up any other way is the bar's conversation too")
    }

    func testTheTabUnderAFingerIsTheOneItIsOnAndPastEitherEndTheEnds() {
        let mid = { (i: Double) in HomeBar.inset + (i + 0.5) * HomeBar.slot }
        XCTAssertEqual(HomePage.allCases.map { HomeBar.page(at: mid(Double(HomePage.allCases.firstIndex(of: $0)!))) }, [.day, .talk, .map, .settings])
        XCTAssertEqual(HomeBar.page(at: -40), .day)
        XCTAssertEqual(HomeBar.page(at: HomeBar.width + 40), .settings)
        XCTAssertEqual(HomeBar.page(at: HomeBar.inset + HomeBar.slot - 0.5), .day, "a tab is the whole of its slot")
        XCTAssertEqual(HomeBar.page(at: HomeBar.inset + HomeBar.slot + 0.5), .talk)
    }

    func testTheLightIsUnderItsTabOrCentredUnderTheFingerAndNeverOutOfTheBar() {
        XCTAssertEqual(HomeBar.light(on: .day, finger: nil), HomeBar.inset)
        XCTAssertEqual(HomeBar.light(on: .settings, finger: nil), HomeBar.inset + 3 * HomeBar.slot, accuracy: 1e-9)
        XCTAssertEqual(HomeBar.light(on: .day, finger: HomeBar.width / 2), HomeBar.width / 2 - HomeBar.slot / 2, accuracy: 1e-9)
        XCTAssertEqual(HomeBar.light(on: .map, finger: 0), HomeBar.inset)
        XCTAssertEqual(HomeBar.light(on: .map, finger: HomeBar.width), HomeBar.width - HomeBar.inset - HomeBar.slot, accuracy: 1e-9)
    }

    func testOnTheConversationTheFieldRestsOverTheBarAndTheKeyboardTakesItHigher() {
        let e = StarEngine(), ui = e.ui
        XCTAssertEqual(ui.footLift, 0, "on the day the field rests at the foot: the bar gives way to it")
        e.showPage(.talk)
        XCTAssertEqual(H - StarUI.restGap - ui.footLift, HomeBar.top - 8, "its foot 8 pt over the bar")
        ui.kbLift = StarUI.lift(keyboardHeight: 336)
        XCTAssertEqual(ui.footLift, ui.kbLift, "with the keyboard up it sits on the keys")
    }

    func testTheBarFloatsOverTheFootWithTheListRunningOnUnderIt() {
        XCTAssertEqual(HomeBar.top + HomeBar.h + HomeBar.foot, H)
        XCTAssertEqual(HomeBar.side * 2 + HomeBar.width, W)
        let end = TLLayout.listBottom + TLLayout.fade
        XCTAssertGreaterThan(end, HomeBar.top, "the list shows through the top of the glass")
        XCTAssertLessThanOrEqual(end - HomeBar.top, 12, "and has faded out before the glass's rounded ends, where it would show round them")
    }

    func testWhileTheFieldIsOutTheListEndsOverIt() {
        let fieldTop = H - StarUI.restGap - 48
        XCTAssertEqual(TLLayout.listBottom - TLLayout.composerRoom + TLLayout.fade, fieldTop - 8, "its fade ends 8 pt over the field")
    }
}
