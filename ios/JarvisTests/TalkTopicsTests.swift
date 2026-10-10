import XCTest
@testable import Jarvis

/// What a talk was about: the day's records as `GET /inherent/memory/day/{day}/records` serves them, and the first words of his in a talk.
final class TalkTopicsTests: XCTestCase {
    private var cal: Calendar { var c = Calendar(identifier: .gregorian); c.timeZone = TimeZone(identifier: "Asia/Shanghai")!; return c }
    private func ms(_ s: String) -> Int64 { Int64(TalkStream.time(s, cal)!.timeIntervalSince1970 * 1000) }
    private let tz = TimeZone(identifier: "Asia/Shanghai")!

    private let json = #"{"day": "2026-10-09", "total": 4, "offset": 0, "records": [{"id": "1", "ts": "2026-10-09T09:00:00", "who": "user", "text": "早上随便说一句"}, {"id": "2", "ts": "2026-10-09T18:12:04", "who": "user", "text": "Jarvis, 明天几点出门？"}, {"id": "3", "ts": "2026-10-09T18:12:09", "who": "jarvis", "text": "<voice>九点二十。</voice>"}, {"id": "4"}]}"#

    func testTheRecordsOfADayAreParsedAndARowThatIsNotOneIsLeftOut() throws {
        let rows = try XCTUnwrap(RecordRow.parse(Data(json.utf8)))
        XCTAssertEqual(rows.map(\.who), ["user", "user", "jarvis"])
        XCTAssertNil(RecordRow.parse(Data("{}".utf8)))
    }

    func testALeadingWakeWordAndWhatFollowsItAreTakenOff() {
        XCTAssertEqual(TalkTopic.clean("Jarvis, 明天几点出门？"), "明天几点出门？")
        XCTAssertEqual(TalkTopic.clean("hey jarvis 帮我看看天气"), "帮我看看天气")
        XCTAssertEqual(TalkTopic.clean("Hi，贾维斯。把灯关了"), "把灯关了")
        XCTAssertEqual(TalkTopic.clean("贾维斯"), "")
        XCTAssertEqual(TalkTopic.clean("今天贾维斯有空吗"), "今天贾维斯有空吗", "only a leading one")
        XCTAssertEqual(TalkTopic.clean("  明早八点叫我 "), "明早八点叫我")
    }

    func testTheTopicIsHisUtteranceWithinTwoSecondsOfTheStartElseTheFirstInsideTheRange() throws {
        let rows = try XCTUnwrap(RecordRow.parse(Data(json.utf8)))
        let near = TalkTopic.first(rows, start: ms("2026-10-09T18:12:03"), end: ms("2026-10-09T18:12:30"), tz: tz)
        XCTAssertEqual(near?.his, "明天几点出门？")
        XCTAssertEqual(near?.hers, "九点二十。", "hers is the first record after his, without markup")
        // the start was not the utterance's second: the first of his inside the range
        let inside = TalkTopic.first(rows, start: ms("2026-10-09T18:11:00"), end: ms("2026-10-09T18:13:00"), tz: tz)
        XCTAssertEqual(inside?.his, "明天几点出门？")
        // two seconds off the early record decides nothing: that one is far from the start
        XCTAssertEqual(TalkTopic.first(rows, start: ms("2026-10-09T09:00:01"), end: nil, tz: tz)?.his, "早上随便说一句")
        XCTAssertNil(TalkTopic.first(rows, start: ms("2026-10-09T20:00:00"), end: ms("2026-10-09T20:10:00"), tz: tz), "none of his in the range")
    }

    func testTheRecordsAreAskedForAgainOnlyWhenTheDaysTalksChanged() async throws {
        let day = #"{"date": "2026-10-09", "tz": "Asia/Shanghai", "start_ms": 0, "end_ms": 1, "now_ms": 1, "missing": [], "items": [{"id": "talk-1", "lane": "her", "kind": "talk", "state": "done", "start_ms": START, "end_ms": END, "turns": 2}]}"#
            .replacingOccurrences(of: "START", with: String(ms("2026-10-09T18:12:03"))).replacingOccurrences(of: "END", with: String(ms("2026-10-09T18:12:30")))
        let reply = try JSONDecoder().decode(DayReply.self, from: Data(day.utf8))
        let rows = try XCTUnwrap(RecordRow.parse(Data(json.utf8)))
        let topics = TalkTopics()
        var asked: [String] = []
        topics.fetch = { d in asked.append(d); return rows }
        await topics.ensure(["2026-10-09": reply])
        XCTAssertEqual(asked, ["2026-10-09"])
        XCTAssertEqual(topics.topic("talk-1"), "明天几点出门？")
        XCTAssertEqual(topics.exchange("talk-1")?.hers, "九点二十。")
        XCTAssertEqual(topics.version, 1)
        await topics.ensure(["2026-10-09": reply])
        XCTAssertEqual(asked.count, 1, "the day's talks are as they were")
    }
}
