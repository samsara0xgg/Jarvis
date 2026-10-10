import Security
import XCTest
@testable import Jarvis

/// The share extension's request (docs/spec.html `POST /inherent/share`) and the pairing it shares with the app.
final class ShareTests: XCTestCase {
    private func body(_ item: SharedItem, note: String = "", ask: Bool = false) -> NSDictionary? { SharePayload.body(item, note: note, ask: ask) as NSDictionary? }

    func testAPageIsSharedAsUrlWithItsTitleAndTheNote() {
        let item = SharedItem(url: URL(string: "https://example.com/a?b=1"), title: "  一篇文章 ")
        XCTAssertEqual(body(item), ["url": "https://example.com/a?b=1", "title": "一篇文章"])
        XCTAssertEqual(body(item, note: " 周末看 ", ask: true), ["url": "https://example.com/a?b=1", "title": "一篇文章", "note": "周末看", "ask": true])
    }

    func testTextIsSharedAsTextAndNeverWithATitle() {
        XCTAssertEqual(body(SharedItem(text: "  记一下：明天 9 点  ", title: "ignored")), ["text": "记一下：明天 9 点"])
    }

    func testATextThatIsOneWebAddressIsThatAddress() {
        XCTAssertEqual(body(SharedItem(text: "https://example.com/x\n")), ["url": "https://example.com/x"])
        XCTAssertEqual(body(SharedItem(text: "see https://example.com/x")), ["text": "see https://example.com/x"], "a sentence stays text")
    }

    func testAnAddressTheBrainWouldRefuseGoesAsText() {
        XCTAssertEqual(body(SharedItem(url: URL(string: "file:///tmp/a.txt"))), ["text": "file:///tmp/a.txt"])
        let long = "https://example.com/" + String(repeating: "a", count: 2000)
        XCTAssertEqual((body(SharedItem(url: URL(string: long))))?["text"] as? String, long)
        XCTAssertNil(body(SharedItem(url: URL(string: long)))?["url"])
    }

    func testWhatIsOverALimitIsCutNotRefused() {
        let b = body(SharedItem(url: URL(string: "https://example.com"), title: String(repeating: "题", count: 400)), note: String(repeating: "n", count: 1200))
        XCTAssertEqual((b?["title"] as? String)?.count, 300)
        XCTAssertEqual((b?["note"] as? String)?.count, 1000)
        XCTAssertEqual((body(SharedItem(text: String(repeating: "x", count: 5000)))?["text"] as? String)?.count, 4000)
    }

    func testNothingToShareIsNoRequestAndAFalseAskIsLeftOut() {
        XCTAssertNil(body(SharedItem(text: "   ")))
        XCTAssertNil(body(SharedItem()))
        XCTAssertNil(body(SharedItem(text: "x"), ask: false)?["ask"])
    }

    func testFailuresAreToldInChinese() {
        XCTAssertEqual(SharePayload.failure(status: 413), "内容太长，大脑没收")
        XCTAssertEqual(SharePayload.failure(status: 400), "大脑没收这个内容")
        XCTAssertEqual(SharePayload.failure(status: 500), "大脑没有答应，稍后再试")
        XCTAssertEqual(SharePayload.unpaired, "还没连上大脑，先在 Jarvis 里配对")
    }

    func testAddressesPairedBeforeTheAppGroupMoveOverOnce() throws {
        let old = try XCTUnwrap(UserDefaults(suiteName: "test.old.\(UUID())")), new = try XCTUnwrap(UserDefaults(suiteName: "test.new.\(UUID())"))
        old.set(["http://100.64.0.1:8765"], forKey: BrainStore.Key.addresses)
        old.set("my-phone", forKey: BrainStore.Key.device)
        BrainStore.adopt(from: old, into: new)
        XCTAssertEqual(new.stringArray(forKey: BrainStore.Key.addresses), ["http://100.64.0.1:8765"])
        XCTAssertEqual(new.string(forKey: BrainStore.Key.device), "my-phone")
        XCTAssertNil(old.object(forKey: BrainStore.Key.addresses), "moved, so an unpair does not bring them back")
        old.set(["http://elsewhere:1"], forKey: BrainStore.Key.addresses)
        BrainStore.adopt(from: old, into: new)
        XCTAssertEqual(new.stringArray(forKey: BrainStore.Key.addresses), ["http://100.64.0.1:8765"], "what the group already has wins")
    }

    /// A token saved before the shared group existed sits in the app's own group: reading it moves it into the shared one, where the extension can read it.
    func testATokenSavedBeforeTheSharedGroupIsMovedWhenRead() throws {
        let account = "test.token.\(UUID())"
        defer { Keychain.delete(account) }
        XCTAssertTrue(Keychain.set("new-token", account))
        let shared = try XCTUnwrap(group(of: account), "an item is written to a group")
        XCTAssertTrue(shared.hasSuffix("com.yilunshi.jarvis.shared"), "the shared group is where items are written: \(shared)")
        // the old way: a token in the app's own group
        let own = shared.replacingOccurrences(of: ".shared", with: "")
        Keychain.delete(account)
        var q: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: "com.allen.jarvis.brain", kSecAttrAccount as String: account,
                                kSecAttrAccessGroup as String: own, kSecValueData as String: Data("old-token".utf8)]
        XCTAssertEqual(SecItemAdd(q as CFDictionary, nil), errSecSuccess, "the app can write its own group")
        XCTAssertEqual(group(of: account), own)
        XCTAssertEqual(Keychain.get(account), "old-token")
        XCTAssertEqual(group(of: account), shared, "moved into the shared group")
        q[kSecAttrAccessGroup as String] = own; q[kSecValueData as String] = nil
        XCTAssertEqual(SecItemCopyMatching(q as CFDictionary, nil), errSecItemNotFound, "and gone from the old one")
        XCTAssertEqual(Keychain.get(account), "old-token")
    }

    private func group(of account: String) -> String? {
        let q: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: "com.allen.jarvis.brain", kSecAttrAccount as String: account,
                                kSecReturnAttributes as String: true, kSecMatchLimit as String: kSecMatchLimitOne]
        var out: CFTypeRef?
        guard SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess else { return nil }
        return (out as? [String: Any])?[kSecAttrAccessGroup as String] as? String
    }
}
