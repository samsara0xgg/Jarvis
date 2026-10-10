import XCTest
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
