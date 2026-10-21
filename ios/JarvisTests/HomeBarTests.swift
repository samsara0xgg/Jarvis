import XCTest
@testable import Jarvis

/// The glass bar at the foot of the phone's home (Sky/HomeBar.swift): one page at a time, the tab under a finger anywhere along it, the light kept inside it, the list running on under it,
/// and what sits over the foot coming up clear of it.
@MainActor
final class HomeBarTests: XCTestCase {
    func testThePageIsOneOfTheThreeAndPickingOneLetsTheOthersGo() {
        let ui = StarEngine().ui
        XCTAssertEqual(ui.page, .day)
        ui.page = .map
        XCTAssertTrue(ui.footprintOpen); XCTAssertFalse(ui.settingsOpen)
        ui.page = .settings
        XCTAssertFalse(ui.footprintOpen, "the map gives way to settings"); XCTAssertTrue(ui.settingsOpen)
        XCTAssertEqual(ui.page, .settings)
        ui.page = .day
        XCTAssertFalse(ui.footprintOpen); XCTAssertFalse(ui.settingsOpen)
        ui.footprintOpen = true
        XCTAssertEqual(ui.page, .map, "a page opened from elsewhere is the bar's too")
    }

    func testTheTabUnderAFingerIsTheOneItIsOnAndPastEitherEndTheEnds() {
        let mid = { (i: Double) in HomeBar.inset + (i + 0.5) * HomeBar.slot }
        XCTAssertEqual(HomeBar.page(at: mid(0)), .day)
        XCTAssertEqual(HomeBar.page(at: mid(1)), .map)
        XCTAssertEqual(HomeBar.page(at: mid(2)), .settings)
        XCTAssertEqual(HomeBar.page(at: -40), .day)
        XCTAssertEqual(HomeBar.page(at: HomeBar.width + 40), .settings)
        XCTAssertEqual(HomeBar.page(at: HomeBar.inset + HomeBar.slot - 0.5), .day, "a tab is the whole of its slot")
        XCTAssertEqual(HomeBar.page(at: HomeBar.inset + HomeBar.slot + 0.5), .map)
    }

    func testTheLightIsUnderItsTabOrCentredUnderTheFingerAndNeverOutOfTheBar() {
        XCTAssertEqual(HomeBar.light(on: .day, finger: nil), HomeBar.inset)
        XCTAssertEqual(HomeBar.light(on: .settings, finger: nil), HomeBar.inset + 2 * HomeBar.slot)
        XCTAssertEqual(HomeBar.light(on: .day, finger: HomeBar.width / 2), HomeBar.width / 2 - HomeBar.slot / 2, accuracy: 1e-9)
        XCTAssertEqual(HomeBar.light(on: .map, finger: 0), HomeBar.inset)
        XCTAssertEqual(HomeBar.light(on: .map, finger: HomeBar.width), HomeBar.width - HomeBar.inset - HomeBar.slot, accuracy: 1e-9)
    }

    func testTheBarFloatsOverTheFootWithTheListRunningOnUnderIt() {
        XCTAssertEqual(HomeBar.top + HomeBar.h + HomeBar.foot, H)
        XCTAssertEqual(HomeBar.side * 2 + HomeBar.width, W)
        XCTAssertEqual(TLLayout.listBottom + TLLayout.fade, HomeBar.top + HomeBar.h, "the list has faded out by the bar's bottom edge: nothing of it shows under the bar")
        XCTAssertGreaterThan(TLLayout.listBottom, HomeBar.top + HomeBar.h / 2, "and it shows through the glass")
    }

    func testWhileTheFieldIsOutTheListEndsOverIt() {
        let fieldTop = H - StarUI.restGap - 48
        XCTAssertEqual(TLLayout.listBottom - TLLayout.composerRoom + TLLayout.fade, fieldTop - 8, "its fade ends 8 pt over the field")
    }
}
