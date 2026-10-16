import XCTest
import CoreLocation
@testable import Jarvis

/// The brain asks where he is (`locate` on /phone/ws) and the phone answers `located`.
final class LocateTests: XCTestCase {
    // MARK: frames

    func testALocateFrameIsReadWithItsRequestId() {
        XCTAssertEqual(HostFrame(Data(#"{"type":"locate","request_id":"R1"}"#.utf8)), .locate(requestId: "R1"))
        XCTAssertNil(HostFrame(Data(#"{"type":"locate"}"#.utf8)), "no id, nothing to answer")
        XCTAssertNil(HostFrame(Data(#"{"type":"locate","request_id":""}"#.utf8)))
        XCTAssertEqual(HostFrame(HostFrame.locate(requestId: "R1").json), .locate(requestId: "R1"))
    }

    func testLocatedCarriesTheFixTheTimeItWasTakenAndTheSnapshotWhenThereIsOne() throws {
        let f = PhoneFrame.located(requestId: "R1", outcome: .fix(lat: 48.4521, lng: -123.3018, accuracyM: 12.5, fixTsEpochMs: 1_791_650_000_123, snapshot: "att9"))
        let o = try XCTUnwrap(JSONSerialization.jsonObject(with: f.json) as? [String: Any])
        XCTAssertEqual(o["type"] as? String, "located"); XCTAssertEqual(o["request_id"] as? String, "R1")
        XCTAssertEqual(o["lat"] as? Double, 48.4521); XCTAssertEqual(o["lng"] as? Double, -123.3018); XCTAssertEqual(o["accuracy_m"] as? Double, 12.5)
        XCTAssertEqual((o["fix_ts_epoch_ms"] as? NSNumber)?.int64Value, 1_791_650_000_123, "an integer, whole")
        XCTAssertEqual(o["snapshot"] as? String, "att9"); XCTAssertNil(o["error"])
        XCTAssertEqual(PhoneFrame(f.json), f)
        let bare = PhoneFrame.located(requestId: "R2", outcome: .fix(lat: 1, lng: 2, accuracyM: 3, fixTsEpochMs: 4, snapshot: nil))
        XCTAssertNil((try JSONSerialization.jsonObject(with: bare.json) as? [String: Any])?["snapshot"], "no picture: no key")
        XCTAssertEqual(PhoneFrame(bare.json), bare)
    }

    func testLocatedWithAnErrorHasOnlyTheErrorAndNoCoordinates() throws {
        for why in ["denied", "unavailable"] {
            let f = PhoneFrame.located(requestId: "R1", outcome: .error(why))
            let o = try XCTUnwrap(JSONSerialization.jsonObject(with: f.json) as? [String: Any])
            XCTAssertEqual(o["error"] as? String, why)
            XCTAssertNil(o["lat"]); XCTAssertNil(o["lng"]); XCTAssertNil(o["snapshot"])
            XCTAssertEqual(PhoneFrame(f.json), f)
        }
    }

    // MARK: through the line, with a fake socket and a fake location source

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

    @MainActor private struct FakeSource: LocationSource {
        let result: LocateResult
        func fix(wait: Double) async -> LocateResult { result }
    }

    private static let fix = LocateFix(lat: 48.4521, lng: -123.3018, accuracyM: 8, at: Date(timeIntervalSince1970: 1_791_650_000.5))

    /// the line, asked `R1`, and what the phone sent back
    @MainActor private func ask(_ result: LocateResult, foreground: Bool = false, snapshot: @escaping (CLLocationCoordinate2D) async -> String? = { _ in "snap1" }) async -> [PhoneFrame] {
        let fake = FakeLink()
        let service = LocateService(source: { FakeSource(result: result) }, foreground: { foreground }, snapshot: snapshot, fixWait: 1, snapshotBudget: 1)
        let line = PhoneLink(board: StatusBoard(), make: { _ in fake }, locator: service)
        line.endpoint = { (URL(string: "http://host")!, "t") }
        line.ensure()
        fake.out.yield(.locate(requestId: "R1"))
        for _ in 0..<100 where fake.sent.isEmpty { try? await Task.sleep(nanoseconds: 20_000_000) }
        return fake.sent
    }

    @MainActor func testAFreshFixIsAnsweredOnTheSocketThatAskedWithItsOwnTime() async {
        let sent = await ask(.fix(Self.fix))
        XCTAssertEqual(sent, [.located(requestId: "R1", outcome: .fix(lat: 48.4521, lng: -123.3018, accuracyM: 8, fixTsEpochMs: 1_791_650_000_500, snapshot: nil))],
                       "the app is in the background: no picture")
    }

    @MainActor func testNoPermissionIsDeniedAndNoFixInTimeIsUnavailable() async {
        let denied = await ask(.denied)
        XCTAssertEqual(denied, [.located(requestId: "R1", outcome: .error("denied"))])
        let none = await ask(.unavailable)
        XCTAssertEqual(none, [.located(requestId: "R1", outcome: .error("unavailable"))])
    }

    // MARK: the snapshot rule

    @MainActor func testOnScreenTheAnswerCarriesTheSnapshotsAttachmentId() async {
        let sent = await ask(.fix(Self.fix), foreground: true)
        guard case .located(_, .fix(_, _, _, _, let snap))? = sent.first else { return XCTFail("a fix") }
        XCTAssertEqual(snap, "snap1")
    }

    @MainActor func testInTheBackgroundTheSnapshotIsNotEvenStarted() async {
        var started = false
        let sent = await ask(.fix(Self.fix), foreground: false, snapshot: { _ in started = true; return "snap1" })
        XCTAssertFalse(started)
        guard case .located(_, .fix(_, _, _, _, let snap))? = sent.first else { return XCTFail("a fix") }
        XCTAssertNil(snap)
    }

    /// a picture that is not ready in time is dropped and the fix goes without it
    @MainActor func testASlowSnapshotIsSkippedAndTheFixStillGoes() async {
        let service = LocateService(source: { FakeSource(result: .fix(Self.fix)) }, foreground: { true },
                                    snapshot: { _ in try? await Task.sleep(nanoseconds: 3_000_000_000); return "late" }, fixWait: 1, snapshotBudget: 0.15)
        let t0 = Date()
        let frame = await service.answer("R1")
        XCTAssertLessThan(Date().timeIntervalSince(t0), 1.5, "not held for the picture")
        guard case .located(_, .fix(_, _, _, _, let snap)) = frame else { return XCTFail("a fix") }
        XCTAssertNil(snap)
    }

    func testWithinGivesTheAnswerWhenInTimeAndNilWhenNot() async {
        let quick: Int? = await within(1) { 7 }
        XCTAssertEqual(quick, 7)
        let slow: Int? = await within(0.1) { try? await Task.sleep(nanoseconds: 2_000_000_000); return 7 }
        XCTAssertNil(slow)
        XCTAssertEqual(LocateSnapshot.renderBudget, 1.5, "the map is skipped when it takes over about a second and a half")
    }
}
