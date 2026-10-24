import XCTest
@testable import Jarvis

/// The chat page's parts that need no screen: 以前的聊天's days and search hits, the cards an answer carries that jump to the day or the map, and her voice on or off in the hello.
/// Nothing here asks the brain.
@MainActor
final class PastChatsTests: XCTestCase {
    private func row(_ seq: Int, _ his: Bool, _ ts: String, _ text: String) -> ConversationRow { ConversationRow(seq: seq, source: his ? "allen" : "jarvis", ts: ts, text: text) }

    func testTheDaysOfTheHeldConversationAreNewestFirstWithHisFirstWords() {
        let cal = Cal.cur.cal, now = TalkStream.time("2026-10-23T12:00:00", cal)!
        let rows = [row(1, true, "2026-10-21T18:12:00", "明天几点出门？"), row(2, false, "2026-10-21T18:12:05", "九点二十。"),
                    row(3, true, "2026-10-23T09:05:00", "帮我整理一下这周的安排"), row(4, false, "2026-10-23T09:06:00", "好。")]
        let days = PastChats.days(rows, cal: cal, now: now)
        XCTAssertEqual(days.map(\.id), ["2026-10-23", "2026-10-21"])
        XCTAssertEqual(days.map(\.title).first, "今天")
        XCTAssertEqual(days.map(\.first), ["帮我整理一下这周的安排", "明天几点出门？"])
        XCTAssertEqual(days.map(\.firstSeq), [3, 1], "a day goes to its first row in the stream")
        XCTAssertEqual(days.map(\.count), [2, 2])
    }

    func testASearchAnswerIsReadByDayAndOneNotOfThatShapeIsNotAnAnswer() {
        let ok = #"{"words":["牙医"],"days":[{"day":"2026-10-20","hits":[{"id":"r7","ts":"2026-10-20T10:00:00","who":"user","text":"约牙医"},{"id":"r8","ts":"2026-10-20T10:00:09","who":"jarvis","text":"…周二上午十点…"}]}],"total":2,"truncated":false}"#
        let days = PastParse.search(Data(ok.utf8))
        XCTAssertEqual(days?.first?.day, "2026-10-20")
        XCTAssertEqual(days?.first?.hits.map(\.his), [true, false])
        XCTAssertEqual(days?.first?.hits.first?.id, "r7", "the hit's id is the conversation row's record id: it finds its row in the stream")
        XCTAssertNil(PastParse.search(Data(#"{"detail":"nope"}"#.utf8)))
        XCTAssertEqual(PastParse.records(Data(#"{"day":"2026-10-20","total":1,"offset":0,"records":[{"id":"r7","ts":"2026-10-20T10:00:00","who":"user","text":"约牙医"}]}"#.utf8))?.count, 1)
    }

    func testTheAgreedListOfChatsIsReadWhenTheBrainServesIt() {
        let json = #"{"chats":[{"id":"chat-41","start_ms":1,"end_ms":2,"first_seq":41,"last_seq":50,"his":"几点出门","hers":"九点二十","title":"出门"}],"has_more":true}"#
        let page = ChatsPage.parse(Data(json.utf8))
        XCTAssertEqual(page?.chats.first?.firstSeq, 41)
        XCTAssertEqual(page?.hasMore, true)
    }

    func testAnAnswerWithNoLinksHasNoCardAndOneWithMoreThanThreeKeepsThree() {
        XCTAssertEqual(AnswerLink.parse(nil), [])
        XCTAssertEqual(AnswerLink.parse([] as [[String: Any]]), [])
        let one: [String: Any] = ["kind": "row", "id": "a", "date": "2026-10-23", "start_ms": 5, "end_ms": NSNull(), "title": "写作业"]
        let outing: [String: Any] = ["kind": "outing", "id": "outing-9", "date": "2026-10-23", "start_ms": 9, "end_ms": 10, "title": "27 路"]
        let bad: [String: Any] = ["kind": "star", "id": "x", "date": "2026-10-23", "start_ms": 1]
        let links = AnswerLink.parse([one, bad, outing, one, outing])
        XCTAssertEqual(links.map(\.kind), [.row, .outing, .row], "a link of a kind the phone does not know is left out; three at most")
        XCTAssertNil(links.first?.endMs)
        XCTAssertEqual(links[1].endMs, 10)
    }

    func testHerVoiceOffGoesInTheHelloAndBackOnComesBack() {
        let kept = VoicePrefs.spoken
        defer { VoicePrefs.spoken = kept }
        func voice(_ f: PhoneFrame) -> Bool? { ((try? JSONSerialization.jsonObject(with: f.json)) as? [String: Any])?["voice"] as? Bool }
        XCTAssertEqual(voice(.hello(voice: false)), false)
        XCTAssertEqual(voice(.hello(voice: true)), true)
        XCTAssertEqual(PhoneFrame(PhoneFrame.hello(voice: false).json), .hello(voice: false))
        VoicePrefs.spoken = false
        XCTAssertFalse(VoicePrefs.spoken)
        VoicePrefs.spoken = true
        XCTAssertTrue(VoicePrefs.spoken)
    }
}
