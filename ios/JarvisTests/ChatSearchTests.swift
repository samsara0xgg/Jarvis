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

    // The examples are the brain's contracts on feat/chat-topics, copied as they are: contracts/topics/list.json, contracts/topics/around.json and contracts/phone_ws/say.json.

    func testTheTopicsContractExampleIsRead() {
        let json = #"{"topics":[{"id":"t1","name":"陶艺课","state":"open","line":"还在进行","recap":"他报了 2026-10-17 周六上午十点的陶艺课 要带一条旧围裙","count":3,"last_ms":1791990600000,"positions":[{"record_id":"a1","seq":1,"ts_ms":1791734400000,"by":"night","lines":["a1","a2"]},{"record_id":"b1","seq":5,"ts_ms":1791943200000,"by":"night","lines":["b1","b2"]},{"record_id":"c1","seq":8,"ts_ms":1791990000000,"by":"him","lines":["c1","c2","c3"]}]},{"id":"t2","name":"搬家","state":"done","line":"箱子订好了","recap":"搬家的前情","count":1,"last_ms":1791946800000,"positions":[{"record_id":"b3","seq":7,"ts_ms":1791946800000,"by":"night","lines":["b3"]}]}]}"#
        let topics = PastParse.topics(Data(json.utf8))
        XCTAssertEqual(topics?.map(\.name), ["陶艺课", "搬家"], "the brain's order: open ones first")
        XCTAssertEqual(topics?.map(\.done), [false, true], "a done topic is drawn dimmed under the open ones")
        XCTAssertEqual(topics?.first?.count, 3, "count is days")
        XCTAssertEqual(topics?.first?.places.map(\.seq), [1, 5, 8], "one place a day, oldest first: the bar steps day by day")
        XCTAssertEqual(topics?.first?.places.last?.lines, ["c1", "c2", "c3"], "a day lights exactly its lines")
        XCTAssertEqual(topics?.first?.places.last?.by, "him")
        XCTAssertEqual(topics?.first?.recap, "他报了 2026-10-17 周六上午十点的陶艺课 要带一条旧围裙")
        XCTAssertNil(PastParse.topics(Data(#"{"detail":"Not Found"}"#.utf8)), "while the brain does not serve topics the answer is not one: the card has no topics")
    }

    func testTheAroundContractExampleIsReadWithEachRowsTopic() {
        let json = #"{"since":"","rows":[{"seq":7,"id":"b3","ts":"2026-10-13T20:00:00-07:00","source":"allen","text":"搬家的箱子订好了","attachments":[]},{"seq":8,"id":"c1","ts":"2026-10-14T08:00:00-07:00","source":"allen","text":"陶艺课改到周日了","attachments":[],"topic":{"id":"t1","name":"陶艺课"}},{"seq":9,"id":"c2","ts":"2026-10-14T08:00:03-07:00","source":"jarvis","text":"记下了 周日上午十点","topic":{"id":"t1","name":"陶艺课"}},{"seq":10,"id":"c3","ts":"2026-10-14T08:10:00-07:00","source":"allen","text":"拉坯工具在哪买","attachments":[]}],"has_more":true,"has_newer":false}"#
        let page = ConversationPage.parse(Data(json.utf8))
        XCTAssertEqual(page?.rows.map(\.seq), [7, 8, 9, 10])
        XCTAssertEqual(page?.hasMore, true)
        XCTAssertEqual(page?.hasNewer, false)
        XCTAssertEqual(page?.rows.map { $0.topic?.name }, [nil, "陶艺课", "陶艺课", nil], "a row said inside a topic carries it")
    }

    func testTheSayContractInATopicIsTheFrameThePhoneSends() throws {
        let json = #"{"type":"say","text":"把它推到明天","spoken":false,"utterance_id":"t30","topic":"t1"}"#
        let frame = PhoneFrame(Data(json.utf8))
        XCTAssertEqual(frame, .say(utteranceId: "t30", text: "把它推到明天", language: nil, confidence: nil, spoken: false, topic: "t1"))
        let back = try XCTUnwrap(frame.map { try JSONSerialization.jsonObject(with: $0.json) as? NSDictionary })
        XCTAssertEqual(back, try JSONSerialization.jsonObject(with: Data(json.utf8)) as? NSDictionary, "what the phone sends is the contract's frame, field for field")
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
