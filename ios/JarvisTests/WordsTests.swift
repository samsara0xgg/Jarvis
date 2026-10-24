import XCTest
@testable import Jarvis

/// The phone's words: the language is the device's own (ADR 0405); an entry is both languages.
@MainActor
final class WordsTests: XCTestCase {
    private func words(stored: Words.Lang? = nil, preferred: [String] = []) -> (Words, UserDefaults) {
        let d = UserDefaults(suiteName: "WordsTests.\(UUID().uuidString)")!
        if let s = stored { d.set(s.rawValue, forKey: "words.language") }
        return (Words(store: d, preferred: preferred), d)
    }

    func testItIsTheDevicesFirstLanguage() {
        XCTAssertEqual(words(preferred: ["zh-Hans-CN", "en-US"]).0.language, .zh)
        XCTAssertEqual(words(preferred: ["zh-Hant-TW"]).0.language, .zh)
        XCTAssertEqual(words(preferred: ["en-CA", "zh-Hans-CN"]).0.language, .en)
        XCTAssertEqual(words(preferred: ["fr-CA"]).0.language, .en, "any other language is English")
        XCTAssertEqual(words(preferred: []).0.language, .en)
    }

    /// what a brain's setting once stored for the next launch (ADR 0327) no longer decides it: an iPad set to English starts in English
    func testAStoredLanguageDoesNotOutweighTheDevices() {
        XCTAssertEqual(words(stored: .zh, preferred: ["en-US"]).0.language, .en)
        XCTAssertEqual(words(stored: .en, preferred: ["zh-Hans"]).0.language, .zh)
    }

    func testADayIsWrittenOneWayInEachLanguageOnItsCalendarsDays() throws {
        let saved = Words.shared.language
        defer { Words.shared.set(saved) }
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = try XCTUnwrap(TimeZone(identifier: "America/Vancouver"))
        let d = try XCTUnwrap(ISO8601DateFormatter().date(from: "2026-10-24T05:30:00Z"))          // 22:30 on Friday the 23rd in Vancouver, Saturday in UTC
        Words.shared.set(.zh)
        XCTAssertEqual(Words.date(d, in: cal), "10月23日 周五")
        XCTAssertEqual(Words.PhoneMap.title(date: Words.date(d, in: cal)), "10月23日 周五 · 足迹", "the map's line under her")
        Words.shared.set(.en)
        XCTAssertEqual(Words.date(d, in: cal), "Fri, Oct 23", "the day of the calendar given, not the device's zone")
        XCTAssertEqual(Words.PhoneMap.title(date: Words.date(d, in: cal)), "Fri, Oct 23 · Footprint")
    }

    func testASessionTappedSaysWhereItRanAndHowOftenHeWrote() {
        let saved = Words.shared.language
        defer { Words.shared.set(saved) }
        Words.shared.set(.zh)
        XCTAssertEqual(Words.Timeline.sessionDetail(cloud: true, prompts: 6), "云端线程 · 在 Mac 上跑 · 发了 6 句")
        XCTAssertEqual(Words.Timeline.sessionDetail(cloud: false, prompts: 2), "发了 2 句")
        XCTAssertNil(Words.Timeline.sessionDetail(cloud: false, prompts: nil))
        XCTAssertEqual(Words.Timeline.cloudThread, "云端线程")
        Words.shared.set(.en)
        XCTAssertEqual(Words.Timeline.sessionDetail(cloud: true, prompts: 1), "Cloud thread · running on the Mac · 1 prompt")
        XCTAssertEqual(Words.Timeline.cloudThread, "Cloud thread")
    }

    func testEntriesFollowTheLanguage() {
        let saved = Words.shared.language
        defer { Words.shared.set(saved) }
        Words.shared.set(.zh)
        XCTAssertEqual(Words.Footprint.today, "今天")
        XCTAssertEqual(Words.Footprint.summaryOne(title: "去 A", distance: "3 km", speed: "20 km/h"), "去 A，3 km，最快 20 km/h。")
        XCTAssertEqual(Words.Footprint.rowSub(distance: "3 km", speed: "20 km/h", stops: 0), "3 km · 最快 20 km/h")
        XCTAssertEqual(Words.Footprint.summaryNone(past: true), "这天没出门。")
        XCTAssertEqual(Words.Footprint.nowAt(place: "Home"), "现在在Home。", "a name has no space round it in Chinese")
        XCTAssertEqual(Words.Footprint.outingTo("Home"), "去Home")
        Words.shared.set(.en)
        XCTAssertEqual(Words.Footprint.today, "Today")
        XCTAssertEqual(Words.Footprint.summaryOne(title: "To A", distance: "3 km", speed: "20 km/h"), "To A, 3 km, fastest 20 km/h.")
        XCTAssertEqual(Words.Footprint.rowSub(distance: "3 km", speed: "20 km/h", stops: 1), "3 km · fastest 20 km/h · 1 stop")
        XCTAssertEqual(Words.Footprint.rowSub(distance: "3 km", speed: "20 km/h", stops: 3), "3 km · fastest 20 km/h · 3 stops")
        XCTAssertEqual(Words.Footprint.nowAt(place: "Home"), "Now at Home.")
        XCTAssertEqual(Words.Footprint.outingTo("Home"), "To Home")
        XCTAssertEqual(Words.Footprint.heartAverage(avg: "70", home: nil), "Average 70")
        XCTAssertEqual(Words.Footprint.heartAverage(avg: "70", home: "60"), "Average 70 · at home 60")
    }

    func testTheSunAsVoiceOverSaysItIsWhatOfItThereIs() {
        let saved = Words.shared.language
        defer { Words.shared.set(saved) }
        Words.shared.set(.zh)
        XCTAssertEqual(Words.Home.sun(rise: "07:34", set: "18:11"), "日出 07:34，日落 18:11")
        XCTAssertEqual(Words.Home.sun(rise: "06:40", set: nil), "日出 06:40")
        Words.shared.set(.en)
        XCTAssertEqual(Words.Home.sun(rise: "07:34", set: "18:11"), "Sunrise 07:34, Sunset 18:11")
        XCTAssertEqual(Words.Home.sun(rise: nil, set: "18:20"), "Sunset 18:20")
    }
}
