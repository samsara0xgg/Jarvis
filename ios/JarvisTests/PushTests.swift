import XCTest
import UserNotifications
@testable import Jarvis

/// What the phone tells the brain about its APNs tokens (docs/spec.html `POST /inherent/device/push`).
final class PushRegistrationTests: XCTestCase {
    func testTheTokenIsLowercaseHexWithoutSeparators() {
        XCTAssertEqual(PushWire.hex(Data([0x00, 0x0a, 0xAB, 0xff, 0x10])), "000aabff10")
        XCTAssertEqual(PushWire.hex(Data()), "")
        XCTAssertEqual(PushWire.hex(Data(repeating: 0xCD, count: 32)).count, 64, "a device token is 64 characters; the brain takes 32 to 512")
    }

    func testRegistrationCarriesTheDeviceTokenAndEnvironmentAndNothingElse() {
        let body = PushWire.registration(deviceToken: "ab12", environment: "sandbox", activity: .keep) as NSDictionary
        XCTAssertEqual(body, ["device_token": "ab12", "environment": "sandbox"], "no live_activity_token key keeps the stored one")
    }

    func testLiveActivityTokenIsSetOrClearedWithNull() {
        let set = PushWire.registration(deviceToken: "ab12", environment: "production", activity: .set("cd34")) as NSDictionary
        XCTAssertEqual(set, ["device_token": "ab12", "environment": "production", "live_activity_token": "cd34"])
        let clear = PushWire.registration(deviceToken: "ab12", environment: "sandbox", activity: .clear)
        XCTAssertTrue(clear["live_activity_token"] is NSNull)
        let json = String(decoding: (try? JSONSerialization.data(withJSONObject: clear, options: [.sortedKeys])) ?? Data(), as: UTF8.self)
        XCTAssertEqual(json, #"{"device_token":"ab12","environment":"sandbox","live_activity_token":null}"#)
    }

    private func profile(_ aps: String?) -> Data {
        let entitlements = aps.map { "<key>Entitlements</key><dict><key>aps-environment</key><string>\($0)</string></dict>" } ?? ""
        let xml = "<?xml version=\"1.0\" encoding=\"UTF-8\"?><plist version=\"1.0\"><dict><key>Name</key><string>x</string>\(entitlements)</dict></plist>"
        // a provisioning profile is a signed blob around the plist
        return Data([0x30, 0x82, 0x01, 0xff]) + Data(xml.utf8) + Data([0x00, 0x01, 0x02])
    }

    func testTheEnvironmentIsReadFromTheProfileTheBuildIsSignedWith() {
        XCTAssertEqual(PushWire.environment(inProfile: profile("development")), "sandbox")
        XCTAssertEqual(PushWire.environment(inProfile: profile("production")), "production")
        XCTAssertNil(PushWire.environment(inProfile: profile(nil)))
        XCTAssertNil(PushWire.environment(inProfile: Data("not a profile".utf8)))
    }

    func testThisBuildNamesAnEnvironmentTheBrainAccepts() {
        XCTAssertTrue(["sandbox", "production"].contains(PushWire.environment))
    }
}

/// The notification shapes the brain pushes (docs/spec.html; jarvis/surface/apns.py `alert_payload`) and what their buttons do.
final class PushNotificationTests: XCTestCase {
    private func cards(_ json: String) -> [WaitingCard] { WaitingCards.parse(Data(json.utf8)) ?? [] }
    private func body(_ r: (path: String, body: [String: Any])?) -> NSDictionary { (r?.body ?? [:]) as NSDictionary }

    private let confirmation = #"{"confirmation": {"id": "c1", "tool": "t", "action": "发这封邮件", "source": "gmail", "letter": true, "args": {"to": ["a@x.com"], "subject": "s", "body": "b"}}}"#
    private func claude(_ items: String) -> String { #"{"claude_requests": ["# + items + "]}" }
    private let bash = #"{"id": "r1", "tool": "Bash", "input": {"command": "npm test"}, "cwd": "/Users/a/Projects/jarvis", "always": "", "session_id": "s", "title": "", "project": ""}"#
    private let bashOther = #"{"id": "r2", "tool": "Bash", "input": {"command": "ls"}, "cwd": "/Users/a/Projects/jarvis/", "always": "", "session_id": "s2", "title": "", "project": ""}"#
    private let write = #"{"id": "r3", "tool": "Write", "input": {"file_path": "/tmp/a"}, "cwd": "/tmp/notes", "always": "", "session_id": "s3", "title": "", "project": ""}"#
    private let ask = #"{"id": "r4", "tool": "AskUserQuestion", "input": {"questions": [{"question": "用哪个库？"}]}, "cwd": "/x/proj", "always": "", "session_id": "s4", "title": "", "project": ""}"#

    func testWaitingCategoriesAreTheSpecsAndMapToCards() {
        XCTAssertEqual(PushShape.Waiting.allCases.map(\.rawValue), ["confirmation", "question", "claude"])
        XCTAssertEqual(PushShape.Waiting.question.cardKind, .clarification, "the brain's question card is the app's ask card")
        XCTAssertEqual(PushShape.waitingThread, "waiting")
        XCTAssertNil(PushShape.Waiting(rawValue: "reminder"))
    }

    func testCategoriesCarryTheButtonsAndOnlyYesNeedsTheUnlock() {
        let byId = Dictionary(uniqueKeysWithValues: PushAction.categories().map { ($0.identifier, $0.actions) })
        XCTAssertEqual(Set(byId.keys), ["confirmation", "question", "claude"])
        XCTAssertEqual(byId["confirmation"]?.map(\.title), ["确认", "取消"])
        XCTAssertEqual(byId["claude"]?.map(\.title), ["允许", "拒绝"])
        XCTAssertEqual(byId["question"]?.count, 0, "an ask card needs typed words: its tap opens the cards")
        for a in byId.values.joined() {
            XCTAssertEqual(a.options.contains(.authenticationRequired), ["jarvis.confirm", "jarvis.allow"].contains(a.identifier), a.identifier)
        }
    }

    func testEachButtonMakesTheRequestOfThatCardsOwnRoute() throws {
        let c = try XCTUnwrap(cards(confirmation).first)
        XCTAssertEqual(PushAction.confirm.answer.request(for: c)?.path, "/inherent/confirmation")
        XCTAssertEqual(body(PushAction.confirm.answer.request(for: c)), ["confirmation_id": "c1", "decision": "accept"])
        XCTAssertEqual(body(PushAction.cancel.answer.request(for: c)), ["confirmation_id": "c1", "decision": "reject"])
        let r = try XCTUnwrap(cards(claude(bash)).first)
        XCTAssertEqual(PushAction.allow.answer.request(for: r)?.path, "/inherent/claude-requests/r1")
        XCTAssertEqual(body(PushAction.allow.answer.request(for: r)), ["decision": "allow"])
        XCTAssertEqual(body(PushAction.deny.answer.request(for: r)), ["decision": "deny"])
    }

    func testAConfirmationButtonAnswersTheOneConfirmationThatWaits() {
        let c = cards(confirmation)
        XCTAssertEqual(PushAnswer.target(.confirm, body: "发这封邮件？", in: c), .card(c[0]))
        XCTAssertEqual(PushAnswer.target(.cancel, body: "", in: []), .gone, "answered at the Mac meanwhile")
        XCTAssertEqual(PushAnswer.target(.confirm, body: "", in: cards(claude(bash))), .gone, "a Claude request is not a confirmation")
    }

    func testAClaudeButtonAnswersTheRequestThatSaysWhatTheNotificationSays() {
        let one = cards(claude(bash + "," + write))
        XCTAssertEqual(one[0].pushLine, "Bash · jarvis")
        XCTAssertEqual(one[1].pushLine, "Write · notes")
        XCTAssertEqual(PushAnswer.target(.allow, body: "Write · notes", in: one), .card(one[1]))
        XCTAssertEqual(PushAnswer.target(.deny, body: "Bash · jarvis", in: one), .card(one[0]))
        XCTAssertEqual(PushAnswer.target(.allow, body: "Edit · notes", in: one), .gone, "that request is not waiting any more; the others are not what he was told about")
    }

    func testTwoRequestsThatSayTheSameThingAreNotGuessedBetween() {
        let two = cards(claude(bash + "," + bashOther))
        XCTAssertEqual(two.map(\.pushLine), ["Bash · jarvis", "Bash · jarvis"], "a trailing slash on the folder is the same folder")
        XCTAssertEqual(PushAnswer.target(.allow, body: "Bash · jarvis", in: two), .ambiguous)
    }

    func testAClaudeQuestionCannotBeAllowedFromTheNotification() {
        let q = cards(claude(ask))
        XCTAssertEqual(PushAnswer.target(.allow, body: "AskUserQuestion · proj", in: q), .ambiguous, "its card has no allow")
        XCTAssertEqual(PushAnswer.target(.deny, body: "AskUserQuestion · proj", in: q), .card(q[0]))
    }

    func testAPushNamesItsCardBesideAps() throws {
        func payload(_ card: String) throws -> [AnyHashable: Any] {
            try XCTUnwrap(JSONSerialization.jsonObject(with: Data((#"{"aps": {"alert": {"title": "t", "body": "b"}, "thread-id": "waiting", "category": "claude"}"# + card + "}").utf8)) as? [AnyHashable: Any])
        }
        XCTAssertEqual(PushShape.cardId(try payload(#", "card": {"kind": "claude", "id": "r1"}"#)), "claude-r1")
        XCTAssertEqual(PushShape.cardId(try payload(#", "card": {"kind": "confirmation", "id": "c1"}"#)), "confirmation-c1")
        XCTAssertEqual(PushShape.cardId(try payload(#", "card": {"kind": "question", "id": "q1"}"#)), "clarification-q1", "the brain's question is the app's ask card")
        XCTAssertNil(PushShape.cardId(try payload("")), "a push from before ADR 0232")
        XCTAssertNil(PushShape.cardId(try payload(#", "card": {"kind": "reminder", "id": "x"}"#)))
        XCTAssertNil(PushShape.cardId(try payload(#", "card": {"kind": "claude"}"#)))
        XCTAssertNil(PushShape.cardId(try payload(#", "card": {"kind": "claude", "id": ""}"#)))
        XCTAssertNil(PushShape.cardId(try payload(#", "card": "claude-r1""#)))
    }

    func testAButtonAnswersTheCardThePushNamesNotOneThatSaysTheSame() {
        let two = cards(claude(bash + "," + bashOther))
        XCTAssertEqual(PushAnswer.target(.allow, body: "星盘 · Bash", cardId: "claude-r2", in: two), .card(two[1]), "the body is a session title now; the id decides")
        XCTAssertEqual(PushAnswer.target(.deny, body: "Bash · jarvis", cardId: "claude-r1", in: two), .card(two[0]))
        XCTAssertEqual(PushAnswer.target(.allow, body: "", cardId: "claude-r9", in: two), .gone, "answered elsewhere meanwhile")
        let c = cards(confirmation)
        XCTAssertEqual(PushAnswer.target(.confirm, body: "", cardId: "confirmation-c1", in: c), .card(c[0]))
        XCTAssertEqual(PushAnswer.target(.confirm, body: "", cardId: "claude-c1", in: c), .gone, "an id of another kind is not this card")
        XCTAssertEqual(PushAnswer.target(.allow, body: "", cardId: "claude-r4", in: cards(claude(ask))), .ambiguous, "its card has no allow")
    }

    func testTheUrlBesideApsIsWhatATapOpens() throws {
        let payload = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(#"{"aps": {"alert": {"title": "Uber", "body": "车到了"}, "category": "ride"}, "url": "uber://?action=setPickup"}"#.utf8)) as? [AnyHashable: Any])
        XCTAssertEqual(PushShape.url(payload)?.absoluteString, "uber://?action=setPickup")
        XCTAssertEqual(PushShape.url(["url": "https://example.com/a"])?.host, "example.com")
        XCTAssertNil(PushShape.url(["aps": [:]]))
        XCTAssertNil(PushShape.url(["url": "no scheme here"]))
        XCTAssertNil(PushShape.url(["url": 5]))
        XCTAssertNil(PushShape.url(["url": "x://" + String(repeating: "a", count: 1030)]), "the brain allows 1024")
    }
}

/// The Live Activity's content-state as the brain pushes it (docs/spec.html "Live Activity"; jarvis/runtime/push.py `_update_activity`).
final class LiveActivityTests: XCTestCase {
    private typealias State = JarvisActivityAttributes.ContentState
    private func decode(_ json: String) throws -> State { try JSONDecoder().decode(State.self, from: Data(json.utf8)) }

    func testAConversationWithAReminderComingDecodes() throws {
        let s = try decode(#"{"state": "conversation", "next_text": "取快递", "next_at_ms": 1760000000000}"#)
        XCTAssertEqual(s, State(state: "conversation", nextText: "取快递", nextAtMs: 1_760_000_000_000))
        XCTAssertEqual(s.nextAt, Date(timeIntervalSince1970: 1_760_000_000))
        XCTAssertEqual(s.stateWord, "正在对话")
    }

    func testNoReminderIsNullForBothAndEveryQuietLevelHasAWord() throws {
        let s = try decode(#"{"state": "idle", "next_text": null, "next_at_ms": null}"#)
        XCTAssertEqual(s, .idle)
        XCTAssertNil(s.nextAt)
        XCTAssertEqual(["idle", "quiet", "no-pop", "dnd"].map { State(state: $0, nextText: nil, nextAtMs: nil).stateWord }, ["在这儿", "安静", "不弹卡片", "勿扰"])
        XCTAssertEqual(State(state: "future-state", nextText: nil, nextAtMs: nil).stateWord, "future-state", "a state the app does not know is shown as it is")
    }

    func testTheStateIsEncodedWithTheBrainsKeys() throws {
        let data = try JSONEncoder().encode(State(state: "dnd", nextText: "a", nextAtMs: 5))
        let o = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? NSDictionary)
        XCTAssertEqual(o, ["state": "dnd", "next_text": "a", "next_at_ms": 5])
    }
}

/// The bus ride's two pushes (docs/spec.html `POST /inherent/ride`): buttons by category, and the ids come from `userInfo`, never from the words.
final class RidePushTests: XCTestCase {
    private func content(title: String = "上 32 路了吗？", body: String = "32 路，下一站 光华路", _ ride: [String: Any]) -> UNNotificationContent {
        let c = UNMutableNotificationContent()
        c.title = title; c.body = body; c.userInfo = ["ride": ride]
        return c
    }

    func testTheCategoriesCarryTheButtonsAndNeedNoUnlock() {
        let byId = Dictionary(uniqueKeysWithValues: RideAction.categories().map { ($0.identifier, $0.actions) })
        XCTAssertEqual(Set(byId.keys), ["ride_board", "ride_alight"])
        XCTAssertEqual(byId["ride_board"]?.map(\.title), ["不是", "上车了"])
        XCTAssertEqual(byId["ride_alight"]?.map(\.title), ["下车了"])
        for a in byId.values.joined() { XCTAssertTrue(a.options.isEmpty, "background, no unlock: " + a.identifier) }
    }

    func testEachButtonPostsItsActionWithTheIdOfWhatItAnswers() throws {
        let board = try XCTUnwrap(RidePush(content(["kind": "board", "candidate_id": "C1", "route": "32"])))
        let alight = try XCTUnwrap(RidePush(content(["kind": "alight", "ride_id": "R1"])))
        func req(_ a: RideAction, _ p: RidePush) -> (String, NSDictionary)? { a.answer.request(for: p.card).map { ($0.path, $0.body as NSDictionary) } }
        XCTAssertEqual(req(.board, board)?.0, "/inherent/ride")
        XCTAssertEqual(req(.board, board)?.1, ["action": "board", "candidate_id": "C1"])
        XCTAssertEqual(req(.not, board)?.1, ["action": "not", "candidate_id": "C1"])
        XCTAssertEqual(req(.end, alight)?.1, ["action": "end", "ride_id": "R1"])
    }

    func testTheCardShowsTheQuestionAndTheSameButtonsAsTheNotification() throws {
        let card = try XCTUnwrap(RidePush(content(["kind": "board", "candidate_id": "C1", "route": "32"]))).card
        XCTAssertEqual(card.kind, .ride)
        XCTAssertEqual(card.title, "上 32 路了吗？")
        XCTAssertEqual(card.meta, "32 路，下一站 光华路")
        XCTAssertEqual(card.actions.map(\.label), ["不是", "上车了"])
        XCTAssertEqual(try XCTUnwrap(RidePush(content(["kind": "alight", "ride_id": "R1"]))).card.actions.map(\.label), ["下车了"])
    }

    func testAPushWithoutItsIdIsNotARidePush() {
        XCTAssertNil(RidePush(content(["kind": "board"])))
        XCTAssertNil(RidePush(content(["kind": "alight", "candidate_id": "C1"])), "a warning names the ride, not a question")
        XCTAssertNil(RidePush(content(["kind": "other", "ride_id": "R1"])))
    }
}
