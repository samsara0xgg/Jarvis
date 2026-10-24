import XCTest
@testable import Jarvis

/// The chat page's parts that need no screen: the search's hits and her topics, the cards an answer carries that jump to the day or the map, and her voice on or off in the hello.
/// Nothing here asks the brain.
@MainActor
final class ChatSearchTests: XCTestCase {
    private func row(_ seq: Int, _ his: Bool, _ ts: String, _ text: String) -> ConversationRow { ConversationRow(seq: seq, source: his ? "allen" : "jarvis", ts: ts, text: text) }

    func testASearchAnswerIsReadByDayAndOneNotOfThatShapeIsNotAnAnswer() {
        let ok = #"{"words":["牙医"],"days":[{"day":"2026-10-20","hits":[{"id":"r7","ts":"2026-10-20T10:00:00","who":"user","text":"约牙医"},{"id":"r8","ts":"2026-10-20T10:00:09","who":"jarvis","text":"…周二上午十点…"}]}],"total":2,"truncated":false}"#
        let days = PastParse.search(Data(ok.utf8))
        XCTAssertEqual(days?.first?.day, "2026-10-20")
        XCTAssertEqual(days?.first?.hits.map(\.his), [true, false])
        XCTAssertEqual(days?.first?.hits.first?.id, "r7", "the hit's id is the conversation row's record id: it finds its row in the stream")
        XCTAssertNil(PastParse.search(Data(#"{"detail":"nope"}"#.utf8)))
    }

    func testTheHitsOfASearchStepOldestFirstAcrossDays() {
        let line = { (id: String) in PastLine(id: id, ts: "2026-10-20T10:00:00", who: "user", text: id) }
        let days = [PastHitDay(day: "2026-10-22", hits: [line("c"), line("d")]), PastHitDay(day: "2026-10-20", hits: [line("a"), line("b")])]
        XCTAssertEqual(PastParse.inOrder(days).map(\.id), ["a", "b", "c", "d"], "the brain sends days newest first and a day's hits oldest first: the bar steps oldest to newest")
    }

    func testTheAgreedTopicsAreReadAndATopicWithNoPlaceIsLeftOut() {
        let json = #"{"topics":[{"id":"t1","name":"陶艺课","state":"open","line":"22:00 那节还没确认","recap":"今晚 22:00，Studio 4。还没回邮件确认。","count":4,"last_ms":1761264000000,"positions":[{"record_id":"r1","seq":10,"ts_ms":1,"by":"him"},{"record_id":"r9","seq":90,"ts_ms":2,"by":"night"}]},{"id":"t3","name":"牙医","state":"done","line":"周二看过了","recap":"","count":1,"last_ms":1,"positions":[{"record_id":"r2","seq":2,"ts_ms":1,"by":"night"}]},{"id":"t2","name":"空","line":"","count":0,"last_ms":0,"positions":[]}]}"#
        let topics = PastParse.topics(Data(json.utf8))
        XCTAssertEqual(topics?.map(\.name), ["陶艺课", "牙医"], "a topic with no place is left out; the brain's order is kept")
        XCTAssertEqual(topics?.first?.places.map(\.seq), [10, 90])
        XCTAssertEqual(topics?.first?.recap, "今晚 22:00，Studio 4。还没回邮件确认。")
        XCTAssertEqual(topics?.map(\.done), [false, true], "a done topic is drawn dimmed under the open ones")
        XCTAssertNil(PastParse.topics(Data(#"{"detail":"Not Found"}"#.utf8)), "while the brain does not serve topics the answer is not one: the card has no topics")
    }

    func testWhatIsSentWhileGoingOnWithATopicCarriesItsIdAndNothingElseDoes() throws {
        defer { TopicFollow.id = nil }
        func field(_ d: Data) -> Any? { ((try? JSONSerialization.jsonObject(with: d)) as? [String: Any])?["topic"] }
        let say = PhoneFrame.say(utteranceId: "u", text: "接着说", language: nil, confidence: nil, spoken: false, topic: "t1")
        XCTAssertEqual(field(say.json) as? String, "t1")
        XCTAssertEqual(PhoneFrame(say.json), say, "the frame reads back with its topic")
        XCTAssertNil(field(PhoneFrame.say(utteranceId: "u", text: "x", language: nil, confidence: nil, spoken: false).json))
        TopicFollow.id = nil
        XCTAssertNil(field(try BrainClient.submitBody("x", attachments: [], about: nil)))
        TopicFollow.id = "t1"
        XCTAssertEqual(field(try BrainClient.submitBody("x", attachments: [], about: nil)) as? String, "t1")
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
