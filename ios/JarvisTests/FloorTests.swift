import XCTest
@testable import Jarvis

/// The phone's half of the voice floor (ADR 0407) on `/phone/ws`: what the brain is told of this phone.
final class FloorTests: XCTestCase {
    @MainActor private final class FakeLink: VoiceLink {
        let events: AsyncStream<LinkEvent>
        let out: AsyncStream<LinkEvent>.Continuation
        private(set) var sent: [PhoneFrame] = []
        init() { (events, out) = AsyncStream.makeStream(of: LinkEvent.self) }
        func connect() { out.yield(.ready(device: "phone", voice: true, sampleRate: kWireRate, judges: true)) }
        func disconnect() {}
        func say(utteranceId: String, text: String, language: VoiceLang?, confidence: Double?, spoken: Bool, overHer: Bool, conversation: Bool,
                 about: [String: Any]?, attachments: [String], continues: String?, timeout: Double) async throws -> SayResult { throw LinkError.timeout }
        func barge() {}
        func cancel() {}
        func send(_ frames: [PlayerOut]) {}
        func sendFrame(_ frame: PhoneFrame) { sent.append(frame) }
    }

    /// the floor frames the line sent once it was ready, after `before` ran on the closed line
    @MainActor private func floorOnReady(before: (PhoneLink) -> Void) async -> [PhoneFrame] {
        let fake = FakeLink()
        let line = PhoneLink(board: StatusBoard(), make: { _ in fake })
        before(line)
        line.endpoint = { (URL(string: "http://host")!, "t") }
        line.ensure()
        for _ in 0..<100 where fake.sent.isEmpty { try? await Task.sleep(nanoseconds: 20_000_000) }
        return fake.sent.filter { if case .floor = $0 { return true }; return false }
    }

    private static let touch = PhoneFrame.floor(busy: false, hears: false, touched: true)

    /// He opened the app (a touch) before its line was up: the brain hears of it as soon as the line is ready, once.
    @MainActor func testATouchBeforeTheLineIsReadyIsReportedWhenItIs() async {
        let sent = await floorOnReady { $0.touched() }
        XCTAssertEqual(sent.first, Self.touch)
        XCTAssertEqual(sent.filter { $0 == Self.touch }.count, 1, "a later ready does not tell it again")
    }

    /// the control: with no touch, the ready report says nothing of use
    @MainActor func testWithNoTouchTheReadyReportIsNotATouch() async {
        let sent = await floorOnReady { _ in }
        XCTAssertEqual(sent.first, .floor(busy: false, hears: false, touched: false))
        XCTAssertFalse(sent.contains(Self.touch))
    }
}
