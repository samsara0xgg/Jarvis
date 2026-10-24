import XCTest
@testable import Jarvis

/// The conversation as `GET /inherent/conversation` serves it (jarvis/state/memory_db.py `conversation_rows`), and and what the talk panel lists of it.
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

    /// the rows in the order the conversation keeps them: by number, which is by time
    private let ordered = #"{"rows": [{"seq": 7, "ts": "2026-10-08T09:20:00", "source": "jarvis", "text": "<voice>九点二十。</voice><document>## 明天\n- 09:20 出门</document>"}, {"seq": 8, "ts": "2026-10-09T23:50:01", "source": "allen", "text": "我明天几点出门？"}, {"seq": 9, "ts": "2026-10-09T23:50:04", "source": "jarvis", "text": "<voice>我查查。</voice>"}]}"#

    func testTheStreamListsTheConversationByDayWithTheTimeAfterALongGap() throws {
        let rows = try XCTUnwrap(ConversationRow.parse(Data(ordered.utf8)))
        let days = TalkStream.convDays(rows, cal: cal, now: date("2026-10-09T23:55:00"))
        XCTAssertEqual(days.map(\.id), ["2026-10-08", "2026-10-09"])
        XCTAssertEqual(days.map(\.title), ["昨天", "今天"])
        XCTAssertEqual(days[0].rows.map(\.text), ["## 明天\n- 09:20 出门"], "her row shows the document alone")
        XCTAssertEqual(days[1].rows.map(\.id), ["c8", "c9"])
        XCTAssertEqual(days[1].rows.map(\.stamp), ["23:50", nil], "three seconds later is the same breath")
        XCTAssertEqual(days[0].rows.map(\.his), [false])
    }

    func testADayAfterAWeekSaysItsDate() {
        XCTAssertEqual(TalkStream.title(date("2026-10-08T09:00:00"), cal, date("2026-10-15T09:00:00")), "10月8日 周四")
    }

    func testTheLiveTalkJoinsTodayAndKeepsItsRowsApart() throws {
        let rows = try XCTUnwrap(ConversationRow.parse(Data(ordered.utf8)))
        let days = TalkStream.convDays(rows, cal: cal, now: date("2026-10-09T23:55:00"))
        let live = [LiveTurn(id: 1, his: true, text: "再说一遍", at: date("2026-10-09T23:55:00")), LiveTurn(id: 2, his: false, text: "九点二十。", at: date("2026-10-09T23:55:05"))]
        let all = TalkStream.compose(days, live: live, cal: cal, now: date("2026-10-09T23:55:00"))
        XCTAssertEqual(all.count, 2)
        XCTAssertEqual(all[1].rows.map(\.id), ["c8", "c9", "l1", "l2"])
        XCTAssertEqual(all[1].rows.map(\.current), [false, false, true, true])
        XCTAssertEqual(all[1].rows[2].stamp, nil, "five minutes after the last row")
        let next = TalkStream.compose(days, live: [LiveTurn(id: 3, his: true, text: "x", at: date("2026-10-10T00:30:00"))], cal: cal, now: date("2026-10-10T00:30:00"))
        XCTAssertEqual(next.count, 3, "a row after midnight starts a new day")
        XCTAssertEqual(next.last?.title, "今天")
        XCTAssertEqual(next.last?.rows.first?.stamp, "00:30")
    }

    /// 问她 puts the chip on before a word is said: it waits at the end of the conversation, and it is not a row of the talk, so the dial's reading zone shows nothing for it.
    func testAChipPutOnBeforeTheFirstLineWaitsAfterTheDaysAndIsNotARowOfTheTalk() throws {
        let rows = try XCTUnwrap(ConversationRow.parse(Data(ordered.utf8)))
        let days = TalkStream.convDays(rows, cal: cal, now: date("2026-10-09T23:55:00"))
        let about = About(kind: "stay", id: "stay-1", title: "家", startMs: 1_791_500_000_000)
        let now = date("2026-10-09T23:55:00")
        XCTAssertEqual(TalkStream.compose(days, live: [], about: nil, cal: cal, now: now), days, "no chip, no change")
        let waiting = TalkStream.compose(days, live: [], about: about, cal: cal, now: now)
        XCTAssertEqual(waiting.count, days.count, "it joins today, whose last row it follows")
        XCTAssertEqual(waiting.last?.rows.map(\.id), ["c8", "c9", "about"])
        let chip = try XCTUnwrap(waiting.last?.rows.last)
        XCTAssertEqual(chip.about, about)
        XCTAssertFalse(chip.current, "not a row of the talk yet")
        XCTAssertTrue(TalkStream.reading(waiting.flatMap(\.rows)).allSatisfy { $0 == 0 }, "the dial's reading zone does not take the last round for the newest")
        // his first line goes out: the chip is the same row, now the talk's first
        let live = [LiveTurn(id: 1, his: true, text: "它是哪年买的", at: now)]
        let said = TalkStream.compose(days, live: live, about: about, cal: cal, now: now)
        XCTAssertEqual(said.last?.rows.map(\.id), ["c8", "c9", "about", "l1"])
        XCTAssertTrue(try XCTUnwrap(said.last?.rows[2]).current)
    }

    /// The talk's own rows are on the screen as live rows while it lasts; once the conversation has them the live rows go: nothing is said twice.
    func testNoRowIsListedTwiceBeforeDuringOrAfterTheConversationBringsTheTalk() throws {
        let rows = try XCTUnwrap(ConversationRow.parse(Data(ordered.utf8)))        // seq 7, 8, 9
        let base = 8                                                               // the talk started with the conversation at 8
        XCTAssertEqual(TalkStream.shown(rows, base: base, talking: true).map(\.seq), [7, 8], "while it is on show, row 9 is the talk's, in its live rows")
        XCTAssertFalse(TalkStream.handedOver(rows, base: base, talking: true))
        XCTAssertFalse(TalkStream.handedOver(Array(rows.prefix(2)), base: base, talking: false), "the conversation has not brought its rows yet: the live rows stay")
        XCTAssertTrue(TalkStream.handedOver(rows, base: base, talking: false))
        XCTAssertEqual(TalkStream.shown(rows, base: base, talking: false).map(\.seq), [7, 8, 9], "the talk is over: the conversation lists all of it")
    }

    private var cal: Calendar { var c = Calendar(identifier: .gregorian); c.timeZone = TimeZone(identifier: "Asia/Shanghai")!; return c }
    private func date(_ s: String) -> Date { TalkStream.time(s, cal)! }
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

    func testWordsSaidToTheWatchSaySo() throws {
        let d = try XCTUnwrap(AskReply.body(" 能听见我说话吗 ", via: "watch"))
        XCTAssertEqual(try JSONSerialization.jsonObject(with: d) as? [String: String], ["text": "能听见我说话吗", "via": "watch"], "ADR 0401: her turn says he spoke to his watch")
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

/// His partial transcript: what stays put when a chunk arrives.
final class HisTextTests: XCTestCase {
    func testTheSharedFrontStaysAndOnlyTheRestBlends() {
        XCTAssertEqual(HisText.common("明天几点", "明天几点出门"), 4)
        XCTAssertEqual(HisText.common("明天几点出门", "明天几点出门？"), 6)
        XCTAssertEqual(HisText.common("明天几点", "后天几点"), 0, "a revised front is all new")
        XCTAssertEqual(HisText.common("", "你好"), 0)
    }
    func testACutThroughAnEnglishWordGoesBackToTheWordsStart() {
        XCTAssertEqual(HisText.common("what is the wea", "what is the weather"), 12)
        XCTAssertEqual(HisText.common("what is the", "what is the weather"), 11, "a space ends the word, nothing to go back over")
    }

    func testTypedWordsAreCutOnlyPastWhatTheBrainTakes() {
        let ok = String(repeating: "字", count: AttachField.maxChars)
        XCTAssertNil(AttachField.clip(ok))
        XCTAssertEqual(AttachField.clip(ok + "多")?.count, AttachField.maxChars)
        XCTAssertNil(AttachField.clip(String(repeating: "x", count: 121)), "the old 120 is gone")
    }

    func testCopyingARowGivesItsWordsAsTheyReadAndNothingForARowWithoutWords() {
        let hers = TalkRow(id: "c1", his: false, text: "**重点**：看[订位页](https://laroux.ca/a)，或 https://www.laroux.ca/b/c 。\n\n- 周五\n  - 七点\n\n| 日 | 事 |\n|---|---|\n| 五 | **会** |\n\n> 引用\n\n---\n\n```\nlet a = 1\n```")
        XCTAssertEqual(TalkStream.copyText(hers), "重点：看订位页 (https://laroux.ca/a)，或 https://www.laroux.ca/b/c 。\n\n• 周五\n  • 七点\n\n日\t事\n五\t会\n\n引用\n\n---\n\nlet a = 1")
        XCTAssertEqual(TalkStream.copyText(TalkRow(id: "c2", his: true, text: "我说 **不是** 标记")), "我说 **不是** 标记", "his words are what he typed")
        XCTAssertNil(TalkStream.copyText(TalkRow(id: "l1", his: false, text: "", status: StatusLine())))
        XCTAssertNil(TalkStream.copyText(TalkRow(id: "about", his: false, text: "", about: About(kind: "reminder", id: "r", title: "给 Mia 回电话", startMs: 0))))
        XCTAssertNil(TalkStream.copyText(TalkRow(id: "l2", his: false, text: "没答出来", failed: true)))
        XCTAssertNil(TalkStream.copyText(TalkRow(id: "l3", his: true, text: "")), "a line of pictures only")
    }
}
