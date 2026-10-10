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

/// `POST /inherent/ask` (docs/spec.html): the body the brain accepts, and the three ways it answers.
final class AskTests: XCTestCase {
    func testTheBodyIsTheTrimmedWordsAndNothingTheBrainWouldRefuse() throws {
        let d = try XCTUnwrap(AskReply.body("  明天几点出门？ \n"))
        XCTAssertEqual(try JSONSerialization.jsonObject(with: d) as? [String: String], ["text": "明天几点出门？"])
        XCTAssertNil(AskReply.body("   "))
        XCTAssertNil(AskReply.body(String(repeating: "a", count: 2001)))
        XCTAssertNotNil(AskReply.body(String(repeating: "a", count: 2000)))
        XCTAssertNil(AskReply.body(String(repeating: "字", count: 1500)), "within 2000 characters but over 4 KiB as bytes: the brain answers 413")
    }

    func testAnAnswerHasSpokenAndMaybeWrittenAnd504IsStillWorking() {
        XCTAssertEqual(AskReply.parse(status: 200, data: Data(#"{"turn_id": "t", "spoken": "九点二十。", "written": ""}"#.utf8)), .answered(spoken: "九点二十。", written: ""))
        XCTAssertEqual(AskReply.parse(status: 200, data: Data(#"{"turn_id": "t", "spoken": "我列在下面。", "written": "- 09:20 出门"}"#.utf8)), .answered(spoken: "我列在下面。", written: "- 09:20 出门"))
        XCTAssertEqual(AskReply.parse(status: 504, data: Data(#"{"turn_id": "t", "detail": "no answer yet; the turn goes on"}"#.utf8)), .working)
        XCTAssertEqual(AskReply.parse(status: 502, data: Data(#"{"turn_id": "t", "detail": "x"}"#.utf8)), .failed)
        XCTAssertNil(AskReply.parse(status: 200, data: Data("{}".utf8)))
        XCTAssertNil(AskReply.parse(status: 413, data: Data()))
    }
}
