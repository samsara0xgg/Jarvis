import XCTest
@testable import Jarvis

/// What he can do to a row of the day on the brain (Brain/DayActions.swift): the route, the body each carries, and what each status of the brain's answer comes to.
final class DayActionsTests: XCTestCase {
    final class Stub: URLProtocol {
        nonisolated(unsafe) static var handler: ((URLRequest) -> (Int, String))?
        nonisolated(unsafe) static var seen: [(method: String, path: String, auth: String?, raw: String)] = []
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
            Stub.seen.append((request.httpMethod ?? "", request.url!.path, request.value(forHTTPHeaderField: "Authorization"), String(decoding: body, as: UTF8.self)))
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

    private func body(_ i: Int = 0) throws -> [String: Any] { try XCTUnwrap(try JSONSerialization.jsonObject(with: Data(Stub.seen[i].raw.utf8)) as? [String: Any]) }

    // MARK: a mark on an item

    func testAMarkIsAPostOfTheDayTheItemAndAPlainTrueOrFalseAndNothingElse() async throws {
        Stub.handler = { _ in (200, #"{"ok": true, "highlights": [{"id": "talk-1", "label": "明天的安排", "by": "him"}, {"id": "call-1", "label": "和 Mia 聊了很久", "by": "her"}]}"#) }
        let on = try await BrainClient.markItem(day: "2026-10-08", id: "talk-1", on: true, bases: [pi], token: "tok")
        XCTAssertEqual(on, .done([Highlight(id: "talk-1", label: "明天的安排", by: "him"), Highlight(id: "call-1", label: "和 Mia 聊了很久", by: "her")]))
        _ = try await BrainClient.markItem(day: "2026-10-08", id: "talk-1", on: false, bases: [pi], token: "tok")
        XCTAssertEqual(Stub.seen.map(\.method), ["POST", "POST"])
        XCTAssertEqual(Stub.seen.map(\.path), ["/inherent/day/highlight", "/inherent/day/highlight"])
        XCTAssertEqual(Stub.seen.map(\.auth), ["Bearer tok", "Bearer tok"])
        XCTAssertEqual(Set(try body(0).keys), ["day", "id", "on"], "no label: the item keeps the name the brain gives it")
        XCTAssertEqual(try body(0)["day"] as? String, "2026-10-08")
        XCTAssertEqual(try body(0)["id"] as? String, "talk-1")
        XCTAssertTrue(Stub.seen[0].raw.contains(#""on":true"#), "the brain's StrictBool takes true and false, not 1 and 0: \(Stub.seen[0].raw)")
        XCTAssertTrue(Stub.seen[1].raw.contains(#""on":false"#), Stub.seen[1].raw)
    }

    func testAnItemIdGoesWholeInTheBody() async throws {
        Stub.handler = { _ in (200, #"{"ok": true, "highlights": []}"#) }
        let id = "commitment-5f2c9d1e4b7a4c3f8a6d2e9b1c0d7a53@2026-10-08"
        _ = try await BrainClient.markItem(day: "2026-10-08", id: id, on: true, bases: [pi], token: "tok")
        XCTAssertEqual(try body()["id"] as? String, id)
    }

    func testTheBrainsAnswerToAMarkByItsStatus() async throws {
        let list = #"{"ok": true, "highlights": [{"id": "a", "label": "", "by": "him"}, {"id": 5}, {"id": "b", "label": "x"}]}"#
        let cases: [(Int, String, HighlightAnswer)] = [
            (200, list, .done([Highlight(id: "a", label: "", by: "him"), Highlight(id: "b", label: "x", by: nil)])),         // one that cannot be read costs only itself
            (200, #"{"ok": true, "highlights": []}"#, .done([])),
            (200, #"{"ok": true}"#, .done(nil)),
            (200, "not json", .done(nil)),
            (404, #"{"detail": "no item a on 2026-10-08"}"#, .gone),
            (404, #"{"detail": "Not Found"}"#, .failed),                  // a brain that has no such route
            (409, #"{"detail": "five of his marks stand"}"#, .full),
            (422, #"{"detail": "bad"}"#, .failed),
        ]
        for (status, text, want) in cases {
            Stub.seen = []
            Stub.handler = { _ in (status, text) }
            let r = try await BrainClient.markItem(day: "2026-10-08", id: "a", on: true, bases: [pi, mac], token: "tok")
            XCTAssertEqual(r, want, "\(status) \(text)")
            XCTAssertEqual(Stub.seen.count, 1, "\(status): the route's own answer, so one try and not one on each address")
        }
    }

    func testAMarkTheBrainDidNotAnswerMovesOnToTheNextAddressAndThenFails() async {
        for status in [500, 502, 503] {
            Stub.seen = []
            Stub.handler = { _ in (status, "") }
            do {
                _ = try await BrainClient.markItem(day: "2026-10-08", id: "a", on: true, bases: [pi, mac], token: "tok")
                XCTFail("\(status) is no answer")
            } catch {
                XCTAssertEqual(error as? BrainError, .refused, "\(status)")
            }
            XCTAssertEqual(Stub.seen.count, 2, "\(status): both addresses were tried")
        }
    }

    // MARK: a commitment

    func testAConfirmOrACancelIsAPostToTheCommitmentWithOnlyTheAction() async throws {
        Stub.handler = { _ in (200, #"{"ok": true}"#) }
        let hex = "5f2c9d1e4b7a4c3f8a6d2e9b1c0d7a53"
        let a = try await BrainClient.actOnCommitment(hex: hex, action: .confirm, bases: [pi], token: "tok")
        let b = try await BrainClient.actOnCommitment(hex: hex, action: .cancel, bases: [pi], token: "tok")
        XCTAssertEqual([a, b], [.done, .done])
        XCTAssertEqual(Stub.seen.map(\.method), ["POST", "POST"])
        XCTAssertEqual(Stub.seen.map(\.path), ["/inherent/commitments/" + hex, "/inherent/commitments/" + hex])
        XCTAssertEqual(Stub.seen.map(\.auth), ["Bearer tok", "Bearer tok"])
        XCTAssertEqual(Set(try body(0).keys), ["action"])
        XCTAssertEqual(try body(0)["action"] as? String, "confirm")
        XCTAssertEqual(try body(1)["action"] as? String, "cancel")
    }

    func testTheBrainsAnswerToACommitmentByItsStatus() async throws {
        let cases: [(Int, String, CommitAnswer)] = [
            (200, #"{"ok": true}"#, .done),
            (200, "", .done),
            (404, #"{"detail": "no live commitment has that id"}"#, .gone),
            (404, #"{"detail": "Not Found"}"#, .failed),
            (409, #"{"detail": "it is confirmed already"}"#, .already),
            (422, #"{"detail": "bad"}"#, .failed),
        ]
        for (status, text, want) in cases {
            Stub.seen = []
            Stub.handler = { _ in (status, text) }
            let r = try await BrainClient.actOnCommitment(hex: "ab12", action: .confirm, bases: [pi, mac], token: "tok")
            XCTAssertEqual(r, want, "\(status) \(text)")
            XCTAssertEqual(Stub.seen.count, 1, "\(status)")
        }
        Stub.seen = []
        Stub.handler = { _ in (502, #"{"detail": "the log cannot be written"}"#) }
        do {
            _ = try await BrainClient.actOnCommitment(hex: "ab12", action: .cancel, bases: [pi, mac], token: "tok")
            XCTFail("a 502 is no answer")
        } catch {
            XCTAssertEqual(error as? BrainError, .refused)
        }
        XCTAssertEqual(Stub.seen.count, 2)
    }

    // MARK: a place he names

    func testANamedPlaceIsAPostOfItsNameItsPointAndItsRole() async throws {
        Stub.handler = { _ in (200, #"{"ok": true, "place": {"id": "place:0a1b2c3d4e5f", "name": "老地方", "role": "other", "radius_m": 120, "has_centre": true, "address": null, "origin": "page"}, "change": "place-change:9f"}"#) }
        let r = try await BrainClient.addPlace(name: "老地方", lat: 48.4538, lng: -123.3268, role: "other", bases: [pi], token: "tok")
        XCTAssertEqual(r, .done(KeptPlace(id: "place:0a1b2c3d4e5f", name: "老地方", role: "other"), change: "place-change:9f"))
        XCTAssertEqual(Stub.seen.map(\.method), ["POST"])
        XCTAssertEqual(Stub.seen.map(\.path), ["/inherent/memory/places"], "the collection: a place with an id is the rename's route")
        XCTAssertEqual(Stub.seen.map(\.auth), ["Bearer tok"])
        let b = try body()
        XCTAssertEqual(Set(b.keys), ["name", "lat", "lng", "role"])
        XCTAssertEqual(b["name"] as? String, "老地方")
        XCTAssertEqual(b["lat"] as? Double, 48.4538)
        XCTAssertEqual(b["lng"] as? Double, -123.3268)
        XCTAssertEqual(b["role"] as? String, "other")
    }

    func testAPlaceKeptAlreadyUnderThatNameChangedNothingAndTheAnswerStillNamesIt() async throws {
        Stub.handler = { _ in (200, #"{"ok": true, "place": {"id": "p1", "name": "家", "role": "home"}, "change": null}"#) }
        let a = try await BrainClient.addPlace(name: "家", lat: 1, lng: 2, role: "home", bases: [pi], token: "tok")
        XCTAssertEqual(a, .done(KeptPlace(id: "p1", name: "家", role: "home"), change: nil))
        Stub.handler = { _ in (200, #"{"ok": true}"#) }
        let b = try await BrainClient.addPlace(name: "家", lat: 1, lng: 2, role: "home", bases: [pi], token: "tok")
        XCTAssertEqual(b, .done(nil, change: nil), "taken is taken: what it says of the place is not needed")
    }

    func testTheBrainsAnswerToANamedPlaceByItsStatus() async throws {
        let taken = "这个名字或这个地方已经记过了"
        let cases: [(Int, String, PlaceAdd)] = [
            (400, #"{"detail": "that spot is already kept as “家” (p-home); to rename it, pass its place id"}"#, .refused(taken)),
            (409, #"{"detail": "x"}"#, .refused(taken)),
            (422, #"{"detail": "x"}"#, .refused("这个名字用不了")),
            (404, #"{"detail": "Not Found"}"#, .refused(BrainError.notOpen.message)),
        ]
        for (status, text, want) in cases {
            Stub.seen = []
            Stub.handler = { _ in (status, text) }
            let r = try await BrainClient.addPlace(name: "X", lat: 1, lng: 2, role: "other", bases: [pi, mac], token: "tok")
            XCTAssertEqual(r, want, "\(status)")
            XCTAssertEqual(Stub.seen.count, 1, "\(status): one try, not one on each address")
        }
        Stub.seen = []
        Stub.handler = { _ in (500, "") }
        do {
            _ = try await BrainClient.addPlace(name: "X", lat: 1, lng: 2, role: "other", bases: [pi, mac], token: "tok")
            XCTFail("a 500 is no answer")
        } catch {
            XCTAssertEqual(error as? BrainError, .refused)
        }
        XCTAssertEqual(Stub.seen.count, 2)
    }

    // MARK: a brain without these routes

    func testOnlyTheFrameworksOwnNotFoundIsARouteThatIsNotThere() {
        XCTAssertTrue(BrainRoute.missing(Data(#"{"detail": "Not Found"}"#.utf8)))
        XCTAssertFalse(BrainRoute.missing(Data(#"{"detail": "no item a on 2026-10-08"}"#.utf8)))
        XCTAssertFalse(BrainRoute.missing(Data()))
        XCTAssertFalse(BrainRoute.missing(Data("<html>".utf8)))
    }
}
