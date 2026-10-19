import XCTest
@testable import Jarvis

/// A place he keeps under another name, and that taken back: the routes, the body each carries, and what each answer of the brain comes to.
final class PlaceRenameTests: XCTestCase {
    final class Stub: URLProtocol {
        nonisolated(unsafe) static var handler: ((URLRequest) -> (Int, String))?
        nonisolated(unsafe) static var seen: [(method: String, path: String, auth: String?, body: [String: Any])] = []
        override class func canInit(with request: URLRequest) -> Bool { true }
        override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
        override func startLoading() {
            var body = Data()
            if let s = request.httpBodyStream {
                s.open()
                var buf = [UInt8](repeating: 0, count: 4096)
                while s.hasBytesAvailable { let n = s.read(&buf, maxLength: buf.count); if n <= 0 { break }; body.append(buf, count: n) }
                s.close()
            } else { body = request.httpBody ?? Data() }
            Stub.seen.append((request.httpMethod ?? "", request.url!.path, request.value(forHTTPHeaderField: "Authorization"), ((try? JSONSerialization.jsonObject(with: body)) as? [String: Any]) ?? [:]))
            let (status, text) = Stub.handler!(request)
            client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: Data(text.utf8))
            client?.urlProtocolDidFinishLoading(self)
        }
        override func stopLoading() {}
    }

    private let pi = URL(string: "http://pi.test:8765")!, mac = URL(string: "http://mac.test:8765")!
    private var savedSession: URLSession!, savedSlow: URLSession!

    override func setUp() {
        let c = URLSessionConfiguration.ephemeral
        c.protocolClasses = [Stub.self]
        savedSession = BrainClient.session; savedSlow = BrainClient.slowSession
        BrainClient.session = URLSession(configuration: c); BrainClient.slowSession = BrainClient.session
        Stub.seen = []
    }
    override func tearDown() { BrainClient.session = savedSession; BrainClient.slowSession = savedSlow; Stub.handler = nil }

    func testARenameIsAPostToThePlaceWithOnlyTheNameAndTheChangeIsTheWayBack() async throws {
        Stub.handler = { _ in (200, #"{"ok": true, "place": {"id": "p-home", "name": "我的家"}, "change": "chg-7"}"#) }
        let r = try await BrainClient.renamePlace(id: "p-home", name: "我的家", bases: [pi], token: "tok")
        XCTAssertEqual(r, .done(change: "chg-7"))
        XCTAssertEqual(Stub.seen.count, 1)
        XCTAssertEqual(Stub.seen[0].method, "POST")
        XCTAssertEqual(Stub.seen[0].path, "/inherent/memory/places/p-home")
        XCTAssertEqual(Stub.seen[0].auth, "Bearer tok")
        XCTAssertEqual(Set(Stub.seen[0].body.keys), ["name"], "no role, no forget")
        XCTAssertEqual(Stub.seen[0].body["name"] as? String, "我的家")
    }

    func testAPlaceIdWithAColonOrADotStaysWholeInThePath() async throws {
        Stub.handler = { _ in (200, #"{"ok": true, "change": "c"}"#) }
        _ = try await BrainClient.renamePlace(id: "place:cafe.1", name: "Cafe", bases: [pi], token: "tok")
        XCTAssertEqual(Stub.seen.map(\.path), ["/inherent/memory/places/place:cafe.1"])
    }

    func testANameTheSameAsBeforeChangedNothingAndHasNothingToTakeBack() async throws {
        Stub.handler = { _ in (200, #"{"ok": true, "place": {"id": "p"}, "change": null}"#) }
        let a = try await BrainClient.renamePlace(id: "p", name: "家", bases: [pi], token: "tok")
        XCTAssertEqual(a, .done(change: nil))
        Stub.handler = { _ in (200, #"{"ok": true}"#) }
        let b = try await BrainClient.renamePlace(id: "p", name: "家", bases: [pi], token: "tok")
        XCTAssertEqual(b, .done(change: nil))
        Stub.handler = { _ in (200, "not json") }
        let c = try await BrainClient.renamePlace(id: "p", name: "家", bases: [pi], token: "tok")
        XCTAssertEqual(c, .done(change: nil), "taken is taken: what it says of the change is not needed")
    }

    func testARefusalIsTheRoutesOwnAnswerAndTheNextAddressIsNotAsked() async throws {
        for (status, words) in [(400, "这个名字用不了，可能已经有别的地方叫它"), (404, "这个地方已经不在了"), (409, "那之后又改过这个地方，撤销不了"), (422, "这个名字用不了，可能已经有别的地方叫它")] {
            Stub.seen = []
            Stub.handler = { _ in (status, #"{"detail": "x"}"#) }
            let r = try await BrainClient.renamePlace(id: "p", name: "X", bases: [pi, mac], token: "tok")
            XCTAssertEqual(r, .refused(words), "\(status)")
            XCTAssertEqual(Stub.seen.count, 1, "\(status): one try, not one on each address")
        }
    }

    func testAnUnexpectedStatusIsTheBrainNotAnsweringAndItMovesOnToTheNextAddress() async {
        Stub.handler = { _ in (500, "") }
        do {
            _ = try await BrainClient.renamePlace(id: "p", name: "X", bases: [pi, mac], token: "tok")
            XCTFail("a 500 is no answer")
        } catch {
            XCTAssertEqual(error as? BrainError, .refused)
        }
        XCTAssertEqual(Stub.seen.count, 2, "both addresses were tried")
    }

    func testTakingItBackIsAPostOfTheChangeAndItsRefusalsHaveTheirOwnWords() async throws {
        Stub.handler = { _ in (200, #"{"ok": true, "place": {"id": "p"}, "change": "chg-8"}"#) }
        let back = try await BrainClient.undoPlaceChange("chg-7", bases: [pi], token: "tok")
        XCTAssertEqual(back, .done(change: "chg-8"))
        XCTAssertEqual(Stub.seen.map(\.path), ["/inherent/memory/places/undo"])
        XCTAssertEqual(Set(Stub.seen[0].body.keys), ["change_id"])
        XCTAssertEqual(Stub.seen[0].body["change_id"] as? String, "chg-7")
        for (status, words) in [(404, "找不到那次改动"), (409, "那之后又改过这个地方，撤销不了"), (400, "撤销不了")] {
            Stub.handler = { _ in (status, "{}") }
            let r = try await BrainClient.undoPlaceChange("chg-7", bases: [pi], token: "tok")
            XCTAssertEqual(r, .refused(words), "\(status)")
        }
    }
}
