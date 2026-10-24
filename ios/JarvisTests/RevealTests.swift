import XCTest
import SwiftUI
@testable import Jarvis

final class RevealTests: XCTestCase {
    func testWordsAChineseCharacterIsOneALatinWordIsOneAndPunctuationGoesWithTheWordBefore() {
        XCTAssertEqual(Reveal.words("好的，我来"), [0, 1, 1, 2, 3])
        XCTAssertEqual(Reveal.words("Hi there, don't go"), [0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 3, 3])
        XCTAssertEqual(Reveal.count("明天 9:20 有 32 路"), 7)       // 明 天 | 9 | 20 | 有 | 32 | 路
        XCTAssertEqual(Reveal.words("a-b c"), [0, 0, 0, 0, 1])
        XCTAssertEqual(Reveal.count(""), 0)
        XCTAssertEqual(Reveal.words("，好"), [0, 0])               // a mark before the first word goes with it
    }

    func testAWordIsDarkUntilSaidThenFadesInOverTwoTenths() {
        let r = Reveal.timed(3, from: 10, every: 0.1)
        XCTAssertEqual(r.alpha(0, at: 9.9), 0)
        XCTAssertEqual(r.alpha(0, at: 10), 0)
        XCTAssertEqual(r.alpha(0, at: 10.1), 0.75, accuracy: 1e-9)       // halfway, eased out
        XCTAssertEqual(r.alpha(0, at: 10.2), 1)
        XCTAssertEqual(r.alpha(2, at: 10.2), 0)
        XCTAssertEqual(r.alpha(2, at: 11), 1)
        XCTAssertEqual(r.alpha(3, at: 99), 0, "a word the list does not know is not said")
        XCTAssertEqual(r.end(), 10.4, accuracy: 1e-9)
        XCTAssertEqual(r.alpha(0, at: 10.05, fade: 0), 1, "no fade: lit as it is said")
        XCTAssertEqual(r.alpha(1, at: 10.05, fade: 0), 0)
    }

    func testEveryParagraphIsTaggedWithItsWordsCountedFromTheStartOfTheAnswer() {
        // the layout counts characters again from each paragraph: the tags must not
        let text = AttributedString("好的。\n\n明天见\n\nOK")
        let pieces = RevealText.pieces(text).map { (String(text[$0.range].characters), $0.word) }
        XCTAssertEqual(pieces.map(\.0), ["好", "的。\n\n", "明", "天", "见\n\n", "OK"])
        XCTAssertEqual(pieces.map(\.1), [0, 1, 2, 3, 4, 5])
        XCTAssertTrue(RevealText.pieces(AttributedString("")).isEmpty)
    }

    /// how tall her card's words are at width 340 with the first `said` of `n` words said
    @MainActor private func height(_ text: String, said: Int, of n: Int, grows: Bool = true) -> Double {
        let now = Date.timeIntervalSinceReferenceDate
        let r = Reveal(at: (0..<n).map { $0 < said ? now - 10 : now + 3600 }, voiced: true)
        let v = RevealText(text: AttributedString(text), reveal: r, grows: grows).font(.system(size: 17)).frame(width: 340)
        return UIHostingController(rootView: v).sizeThatFits(in: CGSize(width: 340, height: 10_000)).height
    }

    @MainActor func testHerCardsWordsAreAsTallAsTheLinesSaidSoFar() {
        let text = String(repeating: "明天下午三点你有一个二面，在线上进行。", count: 6), n = Reveal.count(text)
        let part = height(text, said: n * 3 / 10, of: n), all = height(text, said: n, of: n)
        XCTAssertLessThan(part, all - 40, "three tenths said: a few lines, not the whole answer's room")
        XCTAssertGreaterThan(part, 20, "what is said is there")
        XCTAssertEqual(all, height(text, said: n, of: n, grows: false), accuracy: 1, "all said: the whole text's height")
        XCTAssertEqual(height(text, said: n * 3 / 10, of: n, grows: false), all, accuracy: 1, "a row of the conversation keeps the whole height from the first word")
    }

    func testATypedAnswerFadesInWholeAtOnce() {
        let r = Reveal.timed(4, from: 3, every: 0)
        XCTAssertEqual(r.at, [3, 3, 3, 3])
        XCTAssertEqual(r.alpha(3, at: 3.2), 1)
        XCTAssertEqual(r.end(), 3.2, accuracy: 1e-9, "the whole answer is lit by the end of one fade")
    }

    func testHerVoiceLightsTheWordsItHasStartedAndNeverTakesOneBack() {
        let text = "你好 Allen，明天见"
        let wordOf = Reveal.words(text)                                  // 你 好 | Allen， | 明 天 见 -> 0 1 2 3 4 5
        XCTAssertEqual(Reveal.said(0, in: wordOf), 0)
        XCTAssertEqual(Reveal.said(1, in: wordOf), 1)
        XCTAssertEqual(Reveal.said(4, in: wordOf), 3, "into `Allen` the word is started")
        XCTAssertEqual(Reveal.said(99, in: wordOf), 6)
        var r = Reveal(voiced: true)
        r.say(2, at: 5); r.say(1, at: 6); r.say(4, at: 7)
        XCTAssertEqual(r.at, [5, 5, 7, 7])
        XCTAssertEqual(r.alpha(1, at: 5.2), 1)
        XCTAssertEqual(r.alpha(4, at: 9), 0, "the voice has not got there")
    }
}
