import XCTest
@testable import Jarvis

/// How the brain's words are shown: the markup of a response row (ADR 0040), read the way the Mac companion reads it.
final class AnswerTextTests: XCTestCase {
    func testATaggedRowShowsTheDocumentAlone() {
        let row = "<voice>明天有三个安排，我列在下面了。</voice><document>## 周五\n- 10:00 周会</document>"
        XCTAssertEqual(AnswerText.visible(row), "## 周五\n- 10:00 周会")
        XCTAssertEqual(AnswerText.spoken(row), "明天有三个安排，我列在下面了。")
    }

    func testALeadInWithOnlyVoiceLosesItsTags() {
        XCTAssertEqual(AnswerText.visible("<voice>我上网查查。</voice>"), "我上网查查。")
        XCTAssertEqual(AnswerText.spoken("<voice>我上网查查。</voice>"), "我上网查查。")
        XCTAssertEqual(AnswerText.visible("<voice>先说这个。</voice>再说这个"), "先说这个。\n再说这个")
    }

    func testAnUntaggedAnswerCountsAsBoth() {
        XCTAssertEqual(AnswerText.visible("好，我来整理。"), "好，我来整理。")
        XCTAssertEqual(AnswerText.spoken("好，**我**来 `整理`。"), "好，我来 整理。")
    }

    func testAHalfStreamedTrailingTagIsDropped() {
        XCTAssertEqual(AnswerText.visible("<voice>我上网查查。</voice><doc"), "我上网查查。")
        XCTAssertEqual(AnswerText.visible("<voice>你好</vo"), "你好")
        XCTAssertEqual(AnswerText.visible("你好<"), "你好")
        XCTAssertEqual(AnswerText.spoken("<voice>我上网"), "我上网")
    }
}
