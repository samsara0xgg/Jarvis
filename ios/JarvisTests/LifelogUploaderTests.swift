import XCTest
@testable import Jarvis

/// What the uploader does with the brain's answer to a piece, and how long it waits after a failure (docs/spec.html, lifelog audio).
final class LifelogUploaderTests: XCTestCase {
    private let sha = String(repeating: "ab", count: 32)
    private func v(_ status: Int?, _ body: String? = nil) -> LifelogVerdict {
        LifelogUploader.verdict(status: status, body: body.map { Data($0.utf8) }, localSha: sha)
    }
    private func reply(_ s: String) -> String { "{\"id\":\"x\",\"sha256\":\"\(s)\",\"bytes\":4}" }

    func testStoredWithTheSameShaDeletes() {
        XCTAssertEqual(v(201, reply(sha)), .delete)
        XCTAssertEqual(v(200, reply(sha)), .delete, "already stored identical")
        XCTAssertEqual(v(201, reply(sha.uppercased())), .delete, "hex case does not matter")
    }

    func testStoredButTheShaDiffersOrIsUnreadableKeeps() {
        XCTAssertEqual(v(201, reply(String(repeating: "00", count: 32))), .backoff)
        XCTAssertEqual(v(200, "not json"), .backoff)
        XCTAssertEqual(v(201, nil), .backoff)
        XCTAssertEqual(v(201, "{\"id\":\"x\"}"), .backoff)
    }

    func testDiskFloorWaitsThreeHours() { XCTAssertEqual(v(507), .wait(3 * 3_600_000)) }

    func testConflictAndRejectionAreHeld() {
        XCTAssertEqual(v(409), .hold("conflict"))
        for s in [400, 411, 413, 415, 422] { XCTAssertEqual(v(s), .hold("rejected"), "\(s)") }
    }

    func testRefusedTokenAsksToPairAgain() {
        XCTAssertEqual(v(401), .repair)
        XCTAssertEqual(v(403), .repair)
    }

    func testNoAnswerAndOtherFailuresBackOff() {
        XCTAssertEqual(v(nil), .backoff)
        for s in [500, 502, 503, 504, 404, 429] { XCTAssertEqual(v(s, reply(sha)), .backoff, "\(s)") }
    }

    func testBackoffDoublesFromAMinuteToAnHour() {
        let got = (1...9).map { LifelogUploader.backoffMs(attempts: $0) / 60_000 }
        XCTAssertEqual(got, [1, 2, 4, 8, 16, 32, 60, 60, 60])
        XCTAssertEqual(LifelogUploader.backoffMs(attempts: 0), 60_000)
        XCTAssertEqual(LifelogUploader.backoffMs(attempts: 500), 3_600_000)
    }

    func testShaOfAFileIsStreamedHex() throws {
        let f = FileManager.default.temporaryDirectory.appendingPathComponent("lifelog-\(UUID().uuidString).bin")
        try Data("abc".utf8).write(to: f)
        defer { try? FileManager.default.removeItem(at: f) }
        XCTAssertEqual(LifelogUploader.sha256Hex(of: f), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
    }

    func testRecordedText() {
        XCTAssertEqual(SettingsModel.recordedText(ms: 20_000), "今天还没录")
        XCTAssertEqual(SettingsModel.recordedText(ms: 12 * 60_000), "今天录了 12 分")
        XCTAssertEqual(SettingsModel.recordedText(ms: 72 * 60_000), "今天录了 1 小时 12 分")
    }
}
