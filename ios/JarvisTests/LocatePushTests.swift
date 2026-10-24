import XCTest
@testable import Jarvis

/// A silent push asks where he is now (`{"aps": {"content-available": 1}, "locate": "<request_id>"}`): one fix, reported with the request's id.
@MainActor
final class LocatePushTests: XCTestCase {
    private var dir: URL!

    override func setUp() async throws {
        dir = FileManager.default.temporaryDirectory.appending(path: "locate-push-\(UUID().uuidString)")
    }

    override func tearDown() async throws { try? FileManager.default.removeItem(at: dir) }

    private final class Sent: @unchecked Sendable { var frames: [ReportFrame] = [] }

    private struct Capture: ReportTransport {
        let sent: Sent
        func send(_ frames: [ReportFrame]) async throws -> [FrameAck] {
            sent.frames += frames
            return frames.map { FrameAck(eventUid: $0.eventUid, ok: true, code: nil) }
        }
    }

    private struct FakeSource: LocationSource {
        let result: LocateResult
        func fix(wait: Double) async -> LocateResult { result }
    }

    private func reporter() -> (Reporter, Sent) {
        let r = Reporter(queue: ReportQueue(url: dir.appending(path: "q.sqlite"))), sent = Sent()
        r.autoFlush = false
        r.transport = Capture(sent: sent)
        return (r, sent)
    }

    private func payload(_ f: ReportFrame) throws -> [String: Any] {
        let o = try XCTUnwrap(JSONSerialization.jsonObject(with: f.json) as? [String: Any])
        return try XCTUnwrap(o["payload"] as? [String: Any])
    }

    func testOnlyAPushThatNamesALocateRequestIsOne() {
        XCTAssertEqual(LocatePush.requestId(["aps": ["content-available": 1], "locate": "R7"]), "R7")
        XCTAssertNil(LocatePush.requestId(["aps": ["content-available": 1]]))
        XCTAssertNil(LocatePush.requestId(["locate": ""]))
        XCTAssertNil(LocatePush.requestId(["locate": 5]), "an id is a string")
        XCTAssertNil(LocatePush.requestId(["card": ["kind": "claude", "id": "x"]]), "a waiting-card push is not a locate")
    }

    func testTheFixGoesOutAsALocationFrameWithTheRequestsId() async throws {
        let (r, sent) = reporter()
        let at = Date(timeIntervalSinceNow: -2)
        let ok = await LocatePush.handle("R7", source: FakeSource(result: .fix(LocateFix(lat: 48.4521, lng: -123.3018, accuracyM: 9, at: at))), reporter: r)
        XCTAssertTrue(ok)
        XCTAssertEqual(sent.frames.count, 1, "sent before the wake ends, not left in the queue")
        let o = try XCTUnwrap(JSONSerialization.jsonObject(with: sent.frames[0].json) as? [String: Any])
        XCTAssertEqual(o["event_type"] as? String, "phone.location_observed", "the report layer's own location frame")
        let p = try payload(sent.frames[0])
        XCTAssertEqual(p["request_id"] as? String, "R7")
        XCTAssertEqual(p["lat"] as? Double, 48.4521); XCTAssertEqual(p["lng"] as? Double, -123.3018); XCTAssertEqual(p["accuracy_m"] as? Double, 9)
        XCTAssertNil(p["place"], "no place name is waited for in a wake that has seconds")
        XCTAssertEqual(r.queue.count, 0)
    }

    func testAnOrdinaryLocationFrameHasNoRequestId() throws {
        let f = try XCTUnwrap(ReportFrame(.location(lat: 1, lng: 2, accuracyM: 3, at: Date(), place: nil, speedMps: nil)))
        XCTAssertNil(try payload(f)["request_id"])
    }

    func testTwoRequestsAtTheSameSpotAreTwoFrames() throws {
        let t = Date()
        let a = try XCTUnwrap(ReportFrame(.location(lat: 1, lng: 2, accuracyM: 3, at: t, place: nil, speedMps: nil, requestId: "R1")))
        let b = try XCTUnwrap(ReportFrame(.location(lat: 1, lng: 2, accuracyM: 3, at: t, place: nil, speedMps: nil, requestId: "R2")))
        XCTAssertNotEqual(a.eventUid, b.eventUid, "the brain stores a frame once per uid: each request needs its own")
    }

    func testNoPermissionOrNoFixReportsNothing() async {
        for result in [LocateResult.denied, .unavailable] {
            let (r, sent) = reporter()
            let ok = await LocatePush.handle("R7", source: FakeSource(result: result), reporter: r)
            XCTAssertFalse(ok)
            XCTAssertTrue(sent.frames.isEmpty)
            XCTAssertEqual(r.queue.count, 0)
        }
    }

    /// the delegate always calls the completion handler: a push that is not a locate, and one that finds no permission, end the wake at once. The permission is a
    /// stand-in's: a simulator that once granted location would take a real fix and send it through the shared reporter.
    func testTheDelegateEndsTheWakeEitherWay() async {
        let delegate = ReportAppDelegate()
        delegate.locateSource = { FakeSource(result: .denied) }
        let other = expectation(description: "not a locate"), none = expectation(description: "no permission")
        var results: [String: UIBackgroundFetchResult] = [:]
        delegate.application(.shared, didReceiveRemoteNotification: ["card": ["kind": "claude", "id": "x"]]) { results["other"] = $0; other.fulfill() }
        delegate.application(.shared, didReceiveRemoteNotification: ["aps": ["content-available": 1], "locate": "R9"]) { results["locate"] = $0; none.fulfill() }
        await fulfillment(of: [other, none], timeout: 20)
        XCTAssertEqual(results["other"], .noData)
        XCTAssertNotEqual(results["locate"], .newData, "no fix was taken, so nothing new")
    }

    func testTheBackgroundModesKeepTheOldOnesAndAddRemoteNotification() throws {
        let modes = try XCTUnwrap(Bundle.main.object(forInfoDictionaryKey: "UIBackgroundModes") as? [String])
        for m in ["audio", "location", "fetch", "remote-notification"] { XCTAssertTrue(modes.contains(m), m) }
    }
}
