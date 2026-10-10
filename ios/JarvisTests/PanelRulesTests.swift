import XCTest
@testable import Jarvis

/// The talk panel while he types (spec-2c): where the composer sits over the keys, the chip, keeping to the bottom, and when the panel is there.
final class PanelRulesTests: XCTestCase {
    func testTheComposerSitsEightPointsOverTheKeysNotThirtyFour() {
        for h in [0.0, 300, 345, 410] {
            let bottom = H - StarUI.restGap - StarUI.lift(keyboardHeight: h)       // the composer's bottom, in the layout's own terms
            let keysTop = H - h
            if h == 0 { XCTAssertEqual(bottom, H - 34, "at rest it keeps its place over the home indicator") }
            else { XCTAssertEqual(bottom, keysTop - 8, accuracy: 0.001) }
        }
    }

    func testTheChipNamesWhatWaitsAndWhatSheIsDoingAndIsAbsentWhenBothAreNone() {
        XCTAssertNil(PanelRules.chip(waits: 0, works: 0))
        XCTAssertEqual(PanelRules.chip(waits: 2, works: 0), "等你 2 件")
        XCTAssertEqual(PanelRules.chip(waits: 0, works: 1), "她在做 1")
        XCTAssertEqual(PanelRules.chip(waits: 2, works: 1), "等你 2 件 · 她在做 1")
    }

    func testTheStreamFollowsItsBottomUntilHeScrollsUpFartherThanEightyPoints() {
        XCTAssertTrue(PanelRules.follows(was: true, byHand: false, away: 400), "a row came or the keyboard moved: the answer stands")
        XCTAssertFalse(PanelRules.follows(was: false, byHand: false, away: 0), "and nothing brings it back but his own finger")
        XCTAssertTrue(PanelRules.follows(was: true, byHand: true, away: 80))
        XCTAssertFalse(PanelRules.follows(was: true, byHand: true, away: 80.5))
        XCTAssertTrue(PanelRules.follows(was: false, byHand: true, away: 12), "he scrolled back down")
    }

    // MARK: when the panel is there

    private func engine() -> StarEngine {
        let e = StarEngine()
        e.ui.talking = true
        return e
    }

    func testACardThatArrivesDuringATalkIsARowInTheTalkAndTheSkyStaysShut() throws {
        let c = try XCTUnwrap(WaitingCards.parse(Data(#"{"confirmation": {"id": "c1", "tool": "mcp__gmail__gmail_send", "action": "发这封邮件", "source": "gmail", "letter": true, "args": {"to": ["rachel@x.com"], "subject": "谢谢", "body": "Hi", "content_sha256": "ab"}}}"#.utf8))?.first)
        let e = engine()
        e.phoneEvent(.card(c))
        XCTAssertFalse(e.ui.panelOpen)
        XCTAssertEqual(e.ui.liveTurns.first?.card, c)
    }

    func testClosingThePanelWhileTypingTakesTheKeyboardDownWithIt() {
        let e = StarEngine()
        var down = false
        e.endEditing = { down = true }
        e.openPanel(quiet: true)
        e.ui.typing = true
        e.closePanel(quiet: true)
        XCTAssertFalse(e.ui.panelOpen)
        XCTAssertTrue(down)
    }

    func testAShortVoiceTalkDoesNotOpenThePanelButARichAnswerStillDoes() {
        let e = engine()
        e.addTurn(his: false, "明天下午三点有一个二面。")
        XCTAssertFalse(e.ui.panelOpen)
        e.addTurn(his: false, "## 本周\n- 周五 10:00 周会\n- 周六 09:00 爬山")
        XCTAssertTrue(e.ui.panelOpen)
    }
}
