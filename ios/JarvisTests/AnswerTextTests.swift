import XCTest
import SwiftUI
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

    func testStrayHTMLIsDealtWith() {
        XCTAssertEqual(AnswerText.visible("第一行<br>第二行<BR/>第三行<br />末"), "第一行\n第二行\n第三行\n末")
        XCTAssertEqual(AnswerText.visible("<p>你好</p> <b>x</b> <span style='c'>y</span>"), "你好 x y")
        XCTAssertEqual(AnswerText.visible("a < b and <3 and List<String>"), "a < b and <3 and List<String>")
        XCTAssertEqual(AnswerText.visible("`<br>` stays"), "`<br>` stays")
        XCTAssertEqual(AnswerText.visible("<voice>好的<br>晚安</voice><document>一<br>二</document>"), "一\n二")
        XCTAssertEqual(AnswerText.spoken("<voice>好的<br>晚安</voice><document>一<br>二</document>"), "好的\n晚安")
        XCTAssertEqual(AnswerText.visible("好的<br"), "好的")
    }

    // MARK: links

    /// the text and address of every link in `a`
    private func links(_ a: AttributedString) -> [(String, String)] {
        a.runs.compactMap { r in r.link.map { (String(a[r.range].characters), $0.absoluteString) } }
    }

    func testAMarkdownLinkShowsItsTitleAndABareAddressIsALinkWithChineseAroundThem() {
        let a = AnswerText.linked("看[这篇文章](https://example.com/a?b=1)，或者 https://example.org/x/y，还有 www.example.net 都行。")
        XCTAssertEqual(String(a.characters), "看这篇文章，或者 example.org，还有 example.net 都行。", "a bare address shows as its host")
        let l = links(a)
        XCTAssertEqual(l.map(\.0), ["这篇文章", "example.org", "example.net"])
        XCTAssertEqual(l.map(\.1), ["https://example.com/a?b=1", "https://example.org/x/y", "http://www.example.net"])
    }

    func testAnAddressRightBeforeChineseWordsEndsWhereTheyStart() {
        XCTAssertEqual(links(AnswerText.linked("详见https://example.com/a很好")).map(\.0), ["example.com"])
        XCTAssertEqual(links(AnswerText.linked("详见https://example.com/a很好")).map(\.1), ["https://example.com/a"])
    }

    func testALongBareAddressShowsItsHostWithoutWwwAndStillOpensTheWholeAddress() {
        let a = AnswerText.linked("订位在 https://www.laroux.ca/reservations/book?date=2026-10-16&size=4 ，电话 http://a.example.com:8080/x 也行")
        XCTAssertEqual(String(a.characters), "订位在 laroux.ca ，电话 a.example.com 也行")
        XCTAssertEqual(links(a).map(\.1), ["https://www.laroux.ca/reservations/book?date=2026-10-16&size=4", "http://a.example.com:8080/x"])
        XCTAssertEqual(String(styledWords("见 https://www.laroux.ca/a", base: .white).characters), "见 laroux.ca")
        XCTAssertEqual(links(AnswerText.linked("[订位页](https://www.laroux.ca/a)")).map(\.0), ["订位页"], "a written link keeps its title")
        XCTAssertEqual(links(AnswerText.linked("**https://laroux.ca/a** 好")).map(\.0), ["laroux.ca"])
    }

    func testAFileNameACodeSpanAndAWrittenLinkAreNotLinkedTwiceOrAtAll() {
        XCTAssertTrue(links(AnswerText.linked("改 main.py 和 setup.py 就行")).isEmpty)
        XCTAssertTrue(links(AnswerText.linked("运行 `curl https://example.com` 就行")).isEmpty)
        XCTAssertEqual(links(AnswerText.linked("[https://example.com](https://example.com/b)")).map(\.1), ["https://example.com/b"])
        XCTAssertEqual(String(AnswerText.linked("这是**重点**").characters), "这是重点")
    }

    func testALinkIsSpokenAsItsTitleAndABareAddressIsLeftOut() {
        XCTAssertEqual(AnswerText.spoken("看[这篇文章](https://example.com/a)，详见 https://example.org/x/y。还有 **一条**"), "看这篇文章，详见 。还有 一条")
        XCTAssertEqual(AnswerText.spoken("<voice>网址是https://example.com/a很好</voice>"), "网址是很好")
    }

    func testLinksAreAmberAndUnderlined() {
        let amber = Color(red: 1, green: 217 / 255, blue: 176 / 255)
        let a = styledWords("好 [链接](https://example.com) 呢", base: .white)
        XCTAssertEqual(String(a.characters), "好 链接 呢")
        for r in a.runs { XCTAssertEqual(r.foregroundColor, r.link == nil ? .white : amber); XCTAssertEqual(r.underlineStyle != nil, r.link != nil) }
        XCTAssertNotNil(a.runs.first { $0.link != nil })
    }

    // MARK: the blocks of a written answer

    func testParagraphsAndHeadingsAreBlocks() {
        let blocks = AnswerBlocks.parse("## 本周\n先看这个\n再看那个\n\n最后一段")
        XCTAssertEqual(blocks, [.heading("本周"), .paragraph("先看这个\n再看那个"), .paragraph("最后一段")])
        XCTAssertEqual(AnswerBlocks.parse("好，我来整理。"), [.paragraph("好，我来整理。")])
        XCTAssertEqual(AnswerBlocks.parse(""), [])
    }

    func testBulletsAndNumbersAreListsAndAnIndentedLineContinuesItsItem() {
        let blocks = AnswerBlocks.parse("先做：\n- 周会\n* 爬山\n  带伞\n• 整理\n\n1. 首页\n2) 简历")
        XCTAssertEqual(blocks, [
            .paragraph("先做："),
            .list([.init(marker: "•", text: "周会"), .init(marker: "•", text: "爬山\n带伞"), .init(marker: "•", text: "整理")]),
            .list([.init(marker: "1.", text: "首页"), .init(marker: "2)", text: "简历")]),
        ])
    }

    func testNestedItemsKeepTheirLevelWhateverTheStepOfTheIndent() {
        XCTAssertEqual(AnswerBlocks.parse("- 一\n  - 一a\n    - 一a1\n  - 一b\n- 二"), [.list([
            .init(marker: "•", text: "一"), .init(marker: "•", text: "一a", level: 1), .init(marker: "•", text: "一a1", level: 2),
            .init(marker: "•", text: "一b", level: 1), .init(marker: "•", text: "二"),
        ])])
        XCTAssertEqual(AnswerBlocks.parse("1. 首页\n    - 图一\n    - 图二\n2. 简历"), [.list([
            .init(marker: "1.", text: "首页"), .init(marker: "•", text: "图一", level: 1), .init(marker: "•", text: "图二", level: 1), .init(marker: "2.", text: "简历"),
        ])])
        XCTAssertEqual(AnswerBlocks.parse("  - 缩进开头\n  - 同级"), [.list([.init(marker: "•", text: "缩进开头"), .init(marker: "•", text: "同级")])], "the first item is level 0 wherever it starts")
        XCTAssertEqual(AnswerBlocks.parse("- 甲\n\n  - 乙"), [.list([.init(marker: "•", text: "甲")]), .list([.init(marker: "•", text: "乙")])], "a list that was closed starts again")
    }

    func testQuotesAndRulesAreBlocksAndTheirMarksAreGone() {
        XCTAssertEqual(AnswerBlocks.parse("前言\n> 引用第一行\n>第二行\n>> 再深一层\n后话"), [.paragraph("前言"), .quote("引用第一行\n第二行\n再深一层"), .paragraph("后话")])
        for rule in ["---", "***", "___", "- - -", " ----- "] { XCTAssertEqual(AnswerBlocks.parse("上\n\(rule)\n下"), [.paragraph("上"), .rule, .paragraph("下")], rule) }
        XCTAssertEqual(AnswerBlocks.parse("* 一\n* 二"), [.list([.init(marker: "•", text: "一"), .init(marker: "•", text: "二")])], "a bullet is not a rule")
        XCTAssertEqual(AnswerBlocks.parse("**粗**\n--"), [.paragraph("**粗**\n--")], "too short for a rule")
        XCTAssertEqual(AnswerBlocks.parse("```\n---\n> x\n```"), [.code("---\n> x")], "inside code they are code")
        XCTAssertTrue(AnswerBlocks.isRich("> 引用"))
    }

    func testFencedCodeKeepsItsLinesAndAnOpenFenceIsStillCode() {
        XCTAssertEqual(AnswerBlocks.parse("看这段：\n```swift\nlet a = 1\n  let b = 2\n```\n就这样"),
                       [.paragraph("看这段："), .code("let a = 1\n  let b = 2"), .paragraph("就这样")])
        XCTAssertEqual(AnswerBlocks.parse("```\nlet a"), [.code("let a")], "the answer is still arriving")
    }

    func testATableLosesItsSeparatorRow() {
        let blocks = AnswerBlocks.parse("| 日期 | 事项 |\n|---|:--:|\n| 周五 | 周会 |\n| 周六 | Hike |\n之后")
        XCTAssertEqual(blocks, [.table([["日期", "事项"], ["周五", "周会"], ["周六", "Hike"]]), .paragraph("之后")])
    }

    func testOnlyPlainWordsAreNotRich() {
        XCTAssertFalse(AnswerBlocks.isRich("好，我来整理。\n第二行"))
        for t in ["# 标题", "- 一项", "1. 一项", "| a | b |", "```\ncode\n```"] { XCTAssertTrue(AnswerBlocks.isRich(t), t) }
    }
}
