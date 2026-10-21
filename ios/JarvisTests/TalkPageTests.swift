import XCTest
@testable import Jarvis

/// The conversation as a page of its own on the phone's timeline (Sky/StarPanel.swift's `TalkPage`): she goes to its head in the middle of the top and back home with the day, the stream
/// runs from under her, the sky is dark under her and clear round her and at the foot, and she is not taken for covered by it.
@MainActor
final class TalkPageTests: XCTestCase {
    private var kept = HomeModule.Kind.timeline
    override func setUp() { kept = HomeModule.shared.kind; HomeModule.shared.kind = .timeline }
    override func tearDown() { HomeModule.shared.kind = kept }

    func testOnTheConversationSheGoesToItsHeadAndHomeAgainWithTheDay() {
        let e = StarEngine(), ui = e.ui
        XCTAssertFalse(ui.talkPage)
        XCTAssertEqual(e.restSeat.at, TLSeat.home)
        e.showPage(.talk)
        XCTAssertTrue(ui.talkPage)
        XCTAssertEqual(e.restSeat.at, TLSeat.talk)
        XCTAssertEqual(e.restSeat.r, TLSeat.talkRadius)
        for i in 0..<240 { e.stepSeat(Double(i) * 1000 / 60, 1.0 / 60) }
        XCTAssertEqual(e.seat.x, TLSeat.talk.x, accuracy: 0.5, "she is at the head of the page")
        XCTAssertEqual(e.seat.y, TLSeat.talk.y, accuracy: 0.5)
        e.showPage(.map)
        XCTAssertEqual(e.restSeat.at, TLSeat.corner)
        e.showPage(.day)
        XCTAssertEqual(e.restSeat.at, TLSeat.home)
    }

    func testTheStreamStartsUnderHerAndTheDarkLeavesHerSkyAndTheFoot() {
        let her: Double = TLSeat.talk.y + TLSeat.talkRadius * TLSeat.listening          // her foot while she listens, the biggest she is there
        XCTAssertGreaterThan(TalkPage.top, her, "no row is drawn over her")
        let s = TalkPage.scrim()
        XCTAssertTrue(zip(s, s.dropFirst()).allSatisfy { $0.y <= $1.y }, "the stops are in order down the page")
        XCTAssertGreaterThanOrEqual(s.first!.y, her - 1e-9, "no black over her")
        XCTAssertEqual(s.first!.alpha, 0)
        XCTAssertEqual(s.first { $0.y >= TalkPage.top + TalkPage.darkBy - 1e-9 }!.alpha, 0.8, accuracy: 1e-9, "the talk sits on near-black a little way under her")
        XCTAssertGreaterThanOrEqual(TalkPage.darkBy, 2 * TalkPage.topFade, "a long ramp: no edge across the sky of an empty page")
        XCTAssertEqual(s.last!.y, HomeBar.top + 12, "under the bar the sky is back, as under the day")
        XCTAssertEqual(s.last!.alpha, 0, accuracy: 1e-9)
    }

    func testTheConversationsPageDoesNotCoverHerButThePanelOverTheDialDoes() {
        let e = StarEngine()
        e.showPage(.talk)
        e.panelSince = -1e9
        XCTAssertFalse(e.covered, "she heads the page: she keeps moving")
        HomeModule.shared.kind = .stars
        XCTAssertTrue(e.covered, "under the dial the panel is a sheet over it")
    }

    func testOnTheIPadsOwnHomeTheHistoryIsASheetOverHerNotThePage() {
        let e = StarEngine(), ui = e.ui
        ui.padHome = true
        e.openPanel()          // as her menu on the iPad opens it (Pad/PadHer.swift)
        e.panelSince = -1e9
        XCTAssertFalse(ui.talkPage, "the iPad's history is a sheet, not the phone's page")
        XCTAssertEqual(e.restSeat.at, TLSeat.home, "she stays where she is")
        XCTAssertTrue(e.covered, "she slows behind the sheet")
        ui.padHome = false          // Split View or Slide Over: the phone's home, with its page
        XCTAssertTrue(ui.talkPage)
        XCTAssertFalse(e.covered)
    }
}
