import XCTest
@testable import Jarvis

/// The phone's words: the language is the stored one, else the phone's; an entry is both languages; the brain's reply names one.
@MainActor
final class WordsTests: XCTestCase {
    private func words(stored: Words.Lang? = nil, preferred: [String] = []) -> (Words, UserDefaults) {
        let d = UserDefaults(suiteName: "WordsTests.\(UUID().uuidString)")!
        if let s = stored { d.set(s.rawValue, forKey: "words.language") }
        return (Words(store: d, preferred: preferred), d)
    }

    func testBeforeAnyAnswerItIsThePhonesFirstLanguage() {
        XCTAssertEqual(words(preferred: ["zh-Hans-CN", "en-US"]).0.language, .zh)
        XCTAssertEqual(words(preferred: ["en-CA", "zh-Hans-CN"]).0.language, .en)
        XCTAssertEqual(words(preferred: []).0.language, .en)
    }

    func testTheStoredLanguageWinsAndAChangeIsKept() {
        let (w, d) = words(stored: .en, preferred: ["zh-Hans"])
        XCTAssertEqual(w.language, .en)
        w.set(.zh)
        XCTAssertEqual(Words(store: d, preferred: ["en"]).language, .zh)
    }

    func testTheBrainsReplyNamesALanguage() {
        XCTAssertEqual(Words.Lang(reply: Data(#"{"language":"en"}"#.utf8)), .en)
        XCTAssertNil(Words.Lang(reply: Data(#"{"language":"fr"}"#.utf8)))
        XCTAssertNil(Words.Lang(reply: Data("[]".utf8)))
    }

    func testEntriesFollowTheLanguage() {
        let saved = Words.shared.language
        defer { Words.shared.set(saved) }
        Words.shared.set(.zh)
        XCTAssertEqual(Words.Footprint.today, "今天")
        XCTAssertEqual(Words.Footprint.summaryOne(title: "去 A", distance: "3 km", speed: "20 km/h"), "去 A，3 km，最快 20 km/h。")
        XCTAssertEqual(Words.Footprint.rowSub(distance: "3 km", speed: "20 km/h", stops: 0), "3 km · 最快 20 km/h")
        XCTAssertEqual(Words.Footprint.summaryNone(past: true), "这天没出门。")
        Words.shared.set(.en)
        XCTAssertEqual(Words.Footprint.today, "Today")
        XCTAssertEqual(Words.Footprint.summaryOne(title: "To A", distance: "3 km", speed: "20 km/h"), "To A, 3 km, fastest 20 km/h.")
        XCTAssertEqual(Words.Footprint.rowSub(distance: "3 km", speed: "20 km/h", stops: 1), "3 km · fastest 20 km/h · 1 stop")
        XCTAssertEqual(Words.Footprint.rowSub(distance: "3 km", speed: "20 km/h", stops: 3), "3 km · fastest 20 km/h · 3 stops")
        XCTAssertEqual(Words.Footprint.heartAverage(avg: "70", home: nil), "Average 70")
        XCTAssertEqual(Words.Footprint.heartAverage(avg: "70", home: "60"), "Average 70 · at home 60")
    }
}
