import XCTest
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

    func testAnswerPaceIsSteadyAndQuickerWhenLong() {
        XCTAssertEqual(Reveal.stagger(40), 0.035)
        XCTAssertEqual(Reveal.stagger(400), 0.02, accuracy: 1e-9)
        let r = Reveal.timed(4, from: 0, every: Reveal.stagger(4))
        for (got, want) in zip(r.at, [0, 0.035, 0.07, 0.105]) { XCTAssertEqual(got, want, accuracy: 1e-9) }
    }

    func testEveryParagraphIsTaggedWithItsWordsCountedFromTheStartOfTheAnswer() {
        // the layout counts characters again from each paragraph: the tags must not
        let text = AttributedString("好的。\n\n明天见\n\nOK")
        let pieces = RevealText.pieces(text).map { (String(text[$0.range].characters), $0.word) }
        XCTAssertEqual(pieces.map(\.0), ["好", "的。\n\n", "明", "天", "见\n\n", "OK"])
        XCTAssertEqual(pieces.map(\.1), [0, 1, 2, 3, 4, 5])
        XCTAssertTrue(RevealText.pieces(AttributedString("")).isEmpty)
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
