import XCTest
@testable import Jarvis

/// The conversation as `GET /inherent/conversation` serves it (jarvis/state/memory_db.py `conversation_rows`), and what the drawer makes of it.
final class TalkTests: XCTestCase {
    private let json = #"{"since": "", "rows": [{"seq": 7, "id": "a", "ts": "2026-10-09T23:50:01", "source": "allen", "text": "我明天几点出门？"}, {"seq": 8, "id": "b", "ts": "2026-10-09T23:50:04", "source": "jarvis", "text": "<voice>我查查。</voice>"}, {"seq": 9, "id": "c", "ts": "2026-10-08T09:20:00", "source": "jarvis", "text": "<voice>九点二十。</voice><document>## 明天\n- 09:20 出门</document>"}, {"seq": 10}]}"#

    func testRowsAreHisWhenTheSourceIsAllenAndHersOtherwise() throws {
        let rows = try XCTUnwrap(ConversationRow.parse(Data(json.utf8)))
        XCTAssertEqual(rows.map(\.seq), [7, 8, 9], "a row that is not one is left out")
        XCTAssertEqual(rows.map(\.isHis), [true, false, false])
        XCTAssertNil(ConversationRow.parse(Data("{}".utf8)))
    }

    func testALeadInIsAVoicePartAloneAndHisWordsNeverAreOne() throws {
        let rows = try XCTUnwrap(ConversationRow.parse(Data(json.utf8)))
        XCTAssertEqual(rows.map(\.isLeadIn), [false, true, false])
        XCTAssertFalse(AnswerText.isLeadIn("好，我来整理。"), "an untagged answer is the answer")
    }

    func testTheDrawerShowsTheNewestFirstWithoutMarkupAndDatesOnlyOtherDays() throws {
        let lines = ConvLine.lines(try XCTUnwrap(ConversationRow.parse(Data(json.utf8))), today: "2026-10-09")
        XCTAssertEqual(lines.map(\.id), [9, 8, 7])
        XCTAssertEqual(lines.map(\.text), ["## 明天\n- 09:20 出门", "我查查。", "我明天几点出门？"])
        XCTAssertEqual(lines.map(\.when), ["10-08 09:20", "23:50", "23:50"])
        XCTAssertEqual(lines.map(\.his), [false, false, true])
    }
}
