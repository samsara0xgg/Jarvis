import XCTest
@testable import Jarvis

/// The short names on the dial: whole up to eight cells, cut at a word break after that.
final class FitNameTests: XCTestCase {
    func testShortNamesStayWhole() {
        for n in ["Jarvis", "Claude Code", "给妈妈打电话", "图书馆"] { XCTAssertEqual(RealDay.shared.fit(n, 8), n) }
    }

    func testALongNameIsCutAtAWordBreakOrWhereItFits() {
        XCTAssertEqual(RealDay.shared.fit("Visual Studio Code", 8), "Visual Studio…")
        XCTAssertEqual(RealDay.shared.fit("给 Rachel 写感谢信然后发出去", 8), "给 Rachel…")
        XCTAssertEqual(RealDay.shared.fit("把作品集网站的首页做完", 8), "把作品集网站的…")
    }
}
