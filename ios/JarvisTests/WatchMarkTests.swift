import XCTest
@testable import Jarvis

/// 标一下's answer as the wrist is told it (Watch/WatchRelay.swift `told`): the item marked with its name, or why nothing was.
@MainActor
final class WatchMarkTests: XCTestCase {
    private var saved = Words.Lang.zh
    override func setUp() async throws { saved = Words.shared.language; Words.shared.set(.zh) }
    override func tearDown() async throws { Words.shared.set(saved) }

    func testWhatTheBrainMarkedIsToldWithItsName() {
        let t = WatchRelay.told(.marked(id: "stay-13", day: "2026-10-24", when: "21:30-"), title: { $0 == "stay-13" ? "Studio 4" : "?" })
        XCTAssertEqual(t, .marked(id: "stay-13", day: "2026-10-24", when: "21:30-", title: "Studio 4"))
    }

    func testNothingToMarkAndAFailureAreToldApart() {
        XCTAssertEqual(WatchRelay.told(.nothing, title: { _ in "" }), .markMissed(reason: "这会儿没有可标的"))
        XCTAssertEqual(WatchRelay.told(.failed, title: { _ in "" }), .markMissed(reason: "没标上"))
    }
}
