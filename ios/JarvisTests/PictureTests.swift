import XCTest
import UIKit
@testable import Jarvis

/// The pictures he sent (ADR 0255): the conversation's rows carry them, the phone finds each one again (its own copy, else the brain's), and the talk lists a row of pictures alone.
final class PictureTests: XCTestCase {
    private let json = #"""
    {"rows": [
      {"seq": 1, "id": "rec-1", "ts": "2026-10-12T09:00:00", "source": "allen", "text": "看看这个", "attachments": [{"id": "att-a", "kind": "image", "name": "a.jpg", "gone": false}, {"id": "att-b", "kind": "text", "name": "notes.md", "gone": false}]},
      {"seq": 2, "id": "rec-2", "ts": "2026-10-12T09:01:00", "source": "allen", "text": "", "attachments": [{"id": "att-c", "kind": "image", "name": "c.jpg", "gone": true}]},
      {"seq": 3, "id": "rec-3", "ts": "2026-10-12T09:02:00", "source": "allen", "text": "没有文件", "attachments": []},
      {"seq": 4, "ts": "2026-10-12T09:03:00", "source": "jarvis", "text": "<voice>好</voice>"},
      {"seq": 5, "id": "rec-5", "ts": "2026-10-12T09:04:00", "source": "allen", "text": "x", "attachments": [{"kind": "image"}, {"id": "att-d"}]}
    ]}
    """#

    private var cal: Calendar { var c = Calendar(identifier: .gregorian); c.timeZone = TimeZone(identifier: "Asia/Shanghai")!; return c }

    func testARowCarriesItsIdAndItsFilesAndAnAttachmentOnlyTurnHasNoWords() throws {
        let rows = try XCTUnwrap(ConversationRow.parse(Data(json.utf8)))
        XCTAssertEqual(rows.map(\.id), ["rec-1", "rec-2", "rec-3", "", "rec-5"])
        XCTAssertEqual(rows[0].attachments, [RowAttachment(id: "att-a", kind: "image", name: "a.jpg", gone: false), RowAttachment(id: "att-b", kind: "text", name: "notes.md", gone: false)])
        XCTAssertEqual(rows[0].attachments.map(\.isImage), [true, false])
        XCTAssertEqual(rows[1].text, "")
        XCTAssertEqual(rows[1].attachments.first?.gone, true)
        XCTAssertEqual(rows[2].attachments, [], "an empty list is no files")
        XCTAssertEqual(rows[3].attachments, [], "a row of hers has none")
        XCTAssertEqual(rows[4].attachments.map(\.id), ["att-d"], "a file with no id cannot be opened and is left out")
        XCTAssertEqual(rows[4].attachments.first?.kind, "text", "no kind: a file chip, not a picture")
    }

    func testAttachmentOnlyRowOfHisIsListedWithItsPicturesAndAnEmptyRowOfHersStillIsNot() throws {
        var rows = try XCTUnwrap(ConversationRow.parse(Data(json.utf8)))
        rows.append(ConversationRow(seq: 6, source: "allen", ts: "2026-10-12T09:05:00", text: ""))
        rows.append(ConversationRow(seq: 7, source: "jarvis", ts: "2026-10-12T09:06:00", text: "", attachments: [RowAttachment(id: "z", kind: "image", name: "", gone: false)]))
        let now = TalkStream.time("2026-10-12T09:10:00", cal)!
        let listed = TalkStream.convDays(rows, cal: cal, now: now).flatMap(\.rows)
        XCTAssertEqual(listed.map(\.id), ["c1", "c2", "c3", "c4", "c5"], "no words and no files, or a row of hers with no words, is not a row")
        XCTAssertEqual(listed[1].text, "")
        XCTAssertEqual(listed[1].pics.map(\.attachment), ["att-c"])
        XCTAssertEqual(listed[1].pics.map(\.gone), [true])
        XCTAssertEqual(listed[0].pics.map(\.isImage), [true, false])
        XCTAssertEqual(listed[0].pics[1].title, "notes.md")
    }

    // MARK: finding a picture again

    final class Calls: @unchecked Sendable {
        private let lock = NSLock()
        private var ids: [String] = []
        func add(_ id: String) { lock.lock(); ids.append(id); lock.unlock() }
        var all: [String] { lock.lock(); defer { lock.unlock() }; return ids }
    }

    private var dir: URL!
    override func setUp() { dir = FileManager.default.temporaryDirectory.appendingPathComponent("pictures-\(UUID().uuidString)") }
    override func tearDown() { try? FileManager.default.removeItem(at: dir) }

    private func store(_ calls: Calls, _ answer: @escaping @Sendable (String) -> PictureFetch) -> PictureStore {
        PictureStore(directory: dir) { id in calls.add(id); return answer(id) }
    }
    private func jpeg(_ w: Int, _ h: Int) -> Data {
        let f = UIGraphicsImageRendererFormat(); f.scale = 1
        return UIGraphicsImageRenderer(size: CGSize(width: w, height: h), format: f).image { c in UIColor.red.setFill(); c.fill(CGRect(x: 0, y: 0, width: w, height: h)) }.jpegData(compressionQuality: 0.8)!
    }

    func testThePhonesOwnCopyIsUsedFirstAndTheBrainIsNotAsked() async {
        let calls = Calls(), s = store(calls) { _ in .bytes(Data([1])) }
        await s.keep(id: "att-a", data: jpeg(40, 20))
        let src = await s.source(id: "att-a", gone: false)
        XCTAssertEqual(src, .file(s.url("att-a")))
        XCTAssertEqual(calls.all, [])
        if case .image(let img) = await s.image(id: "att-a", gone: true, maxPixel: 100) { XCTAssertEqual(img.size.width, 40, accuracy: 1) } else { XCTFail("a swept file is still shown from the phone's own copy") }
    }

    func testWithoutOwnCopyTheBrainsIsFetchedOnceAndKept() async {
        let calls = Calls(), s = store(calls) { _ in .bytes(self.jpeg(30, 30)) }
        let first = await s.source(id: "att-b", gone: false), second = await s.source(id: "att-b", gone: false)
        XCTAssertEqual(first, .file(s.url("att-b")))
        XCTAssertEqual(second, first)
        XCTAssertEqual(calls.all, ["att-b"], "kept in the same folder: the second look is local")
        XCTAssertTrue(FileManager.default.fileExists(atPath: s.url("att-b").path))
    }

    func testSeveralViewsOfOnePictureAskTheBrainOnce() async {
        let calls = Calls(), s = store(calls) { _ in .bytes(self.jpeg(30, 30)) }
        async let a = s.source(id: "att-s", gone: false), b = s.source(id: "att-s", gone: false), c = s.image(id: "att-s", gone: false, maxPixel: 50)
        _ = await (a, b, c)
        XCTAssertEqual(calls.all.count, 1)
    }

    func testAPictureTheRowCallsGoneIsNotFetchedAndShowsThePlaceholder() async {
        let calls = Calls(), s = store(calls) { _ in .bytes(Data([1])) }
        let src = await s.source(id: "att-c", gone: true)
        XCTAssertEqual(src, .gone)
        XCTAssertEqual(calls.all, [])
        if case .gone = await s.image(id: "att-c", gone: true, maxPixel: 100) {} else { XCTFail("gone") }
    }

    func testA404IsGoneForGoodAndAFailureIsAskedAgainNextTime() async {
        let calls = Calls(), s = store(calls) { $0 == "swept" ? .gone : .failed }
        let a = await s.source(id: "swept", gone: false), b = await s.source(id: "swept", gone: false)
        XCTAssertEqual([a, b], [.gone, .gone])
        XCTAssertEqual(calls.all, ["swept"], "the brain said 404: not asked again")
        let c = await s.source(id: "offline", gone: false), d = await s.source(id: "offline", gone: false)
        XCTAssertEqual([c, d], [.failed, .failed])
        XCTAssertEqual(calls.all, ["swept", "offline", "offline"], "no answer is not a verdict")
    }

    func testTheOriginalIsKeptByCopyingTheFileAndTheFileNameIsSafe() async throws {
        let s = store(Calls()) { _ in .failed }
        let src = FileManager.default.temporaryDirectory.appendingPathComponent("orig-\(UUID().uuidString).jpg")
        try jpeg(64, 48).write(to: src)
        defer { try? FileManager.default.removeItem(at: src) }
        await s.keep(id: "../evil/id", copying: src)
        XCTAssertEqual(s.url("../evil/id").deletingLastPathComponent().path, dir.path, "an id cannot leave the folder")
        let src2 = await s.source(id: "../evil/id", gone: false)
        XCTAssertEqual(src2, .file(s.url("../evil/id")))
        if case .image(let img) = await s.image(id: "../evil/id", gone: false, maxPixel: 32) { XCTAssertEqual(max(img.size.width, img.size.height), 32, accuracy: 1, "decoded at the size asked, not whole") } else { XCTFail() }
        XCTAssertEqual(s.aspect(id: "../evil/id") ?? 0, 64.0 / 48.0, accuracy: 0.05)
    }

    func testALoneSinglePictureKeepsItsShapeWithinTheLimits() {
        XCTAssertEqual(PictureLayout.single(aspect: 1.5), CGSize(width: 220, height: 220 / 1.5))
        XCTAssertEqual(PictureLayout.single(aspect: 0.5), CGSize(width: 140, height: 280), "tall: capped at 280 high")
        XCTAssertEqual(PictureLayout.single(aspect: 0.1).width, 88, "a sliver is not narrower than 88")
        XCTAssertEqual(PictureLayout.single(aspect: nil).width, PictureLayout.single(aspect: nil).height, "a square until the shape is known")
    }

    // MARK: the route

    final class Stub: URLProtocol {
        nonisolated(unsafe) static var status = 200
        nonisolated(unsafe) static var paths: [String] = []
        override class func canInit(with request: URLRequest) -> Bool { true }
        override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
        override func startLoading() {
            Stub.paths.append(request.url!.path)
            client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: Stub.status, httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: Data([9, 8, 7]))
            client?.urlProtocolDidFinishLoading(self)
        }
        override func stopLoading() {}
    }

    func testTheStoredFileIsReadFromItsRouteAnd404MeansGone() async throws {
        let c = URLSessionConfiguration.ephemeral
        c.protocolClasses = [Stub.self]
        let saved = BrainClient.slowSession
        BrainClient.slowSession = URLSession(configuration: c)
        defer { BrainClient.slowSession = saved }
        let base = URL(string: "http://pi.test:8765")!
        Stub.status = 200; Stub.paths = []
        let got = try await BrainClient.attachment(id: "att-a", bases: [base], token: "tok")
        XCTAssertEqual(got, Data([9, 8, 7]))
        XCTAssertEqual(Stub.paths, ["/inherent/attachments/att-a"])
        Stub.status = 404
        let none = try await BrainClient.attachment(id: "att-a", bases: [base], token: "tok")
        XCTAssertNil(none)
    }
}
