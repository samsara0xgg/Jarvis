import XCTest
@testable import Jarvis

/// A 401 unpairs the phone only when the daemon that issued its token refuses the token at its guard, twice.
final class PairingKeepTests: XCTestCase {
    final class Stub: URLProtocol {
        nonisolated(unsafe) static var handler: ((URLRequest) -> (Int, String))?
        nonisolated(unsafe) static var seen: [String] = []
        override class func canInit(with request: URLRequest) -> Bool { true }
        override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
        override func startLoading() {
            Stub.seen.append("\(request.url!.host!)\(request.url!.path)")
            let (status, body) = Stub.handler!(request)
            client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: Data(body.utf8))
            client?.urlProtocolDidFinishLoading(self)
        }
        override func stopLoading() {}
    }

    private let pi = URL(string: "http://pi.test:8765")!, mac = URL(string: "http://mac.test:8765")!
    private let guardBody = #"{"detail":"unauthorized"}"#, routeBody = #"{"detail":"desktop authorization required"}"#
    private var savedSession: URLSession!, savedSlow: URLSession!

    override func setUp() {
        let c = URLSessionConfiguration.ephemeral
        c.protocolClasses = [Stub.self]
        savedSession = BrainClient.session; savedSlow = BrainClient.slowSession
        BrainClient.session = URLSession(configuration: c); BrainClient.slowSession = BrainClient.session
        Stub.seen = []
    }
    override func tearDown() { BrainClient.session = savedSession; BrainClient.slowSession = savedSlow; Stub.handler = nil }

    private func paired(bases: [URL]? = nil, issuer: URL? = nil) -> BrainLink {
        BrainLink(defaults: UserDefaults(suiteName: "pairing-keep-tests")!, pairing: ("tok", bases ?? [pi], issuer ?? pi))
    }
    /// the route under test (`/inherent/plugins/action`) answers `route`; the probe route (`/inherent/waiting`) answers `probe`
    private func serve(route: (Int, String), probe: (Int, String)) {
        Stub.handler = { $0.url!.path == "/inherent/waiting" ? probe : route }
    }
    private func failure(_ link: BrainLink) async -> BrainError? {
        do { _ = try await link.call { try await BrainClient.send("POST", "/inherent/plugins/action", body: Data("{}".utf8), bases: $0, token: $1, unauthorized: .revoked) }; return nil }
        catch { return error as? BrainError }
    }

    func testARouteRefusalIsNotAnUnpairing() async {
        serve(route: (401, routeBody), probe: (401, guardBody))
        let link = paired()
        let e = await failure(link)
        XCTAssertEqual(e, .refused)
        XCTAssertTrue(link.hasToken)
        XCTAssertEqual(Stub.seen, ["pi.test/inherent/plugins/action"], "no probe: the guard did not refuse the token")
    }

    func testAGuardRefusalConfirmedByTheProbeUnpairs() async {
        serve(route: (401, guardBody), probe: (401, guardBody))
        let link = paired()
        let e = await failure(link)
        XCTAssertEqual(e, .revoked)
        XCTAssertFalse(link.hasToken)
        XCTAssertEqual(Stub.seen, ["pi.test/inherent/plugins/action", "pi.test/inherent/waiting"])
    }

    func testAGuardRefusalTheProbeContradictsKeepsThePairing() async {
        serve(route: (401, guardBody), probe: (200, "{}"))
        let link = paired()
        let e = await failure(link)
        XCTAssertEqual(e, .refused)
        XCTAssertTrue(link.hasToken)
    }

    func testAnEmptyOrUnreadable401CountsAsTheGuards() {
        XCTAssertTrue(BrainClient.isGuardRefusal(Data()))
        XCTAssertTrue(BrainClient.isGuardRefusal(Data("<html>".utf8)))
        XCTAssertTrue(BrainClient.isGuardRefusal(Data(guardBody.utf8)))
        XCTAssertFalse(BrainClient.isGuardRefusal(Data(routeBody.utf8)))
    }

    func testTheProbeAsksOnlyTheDaemonThatIssuedTheToken() async {
        // the Mac's daemon is listed first and does not know the token; the Pi's does
        Stub.handler = { $0.url!.host == "mac.test" ? (401, self.guardBody) : (200, "{}") }
        let link = paired(bases: [mac, pi], issuer: pi)
        let e = await failure(link)
        XCTAssertEqual(e, .refused)
        XCTAssertTrue(link.hasToken, "the Mac's guard refusing the token does not unpair the phone")
        XCTAssertEqual(Stub.seen, ["mac.test/inherent/plugins/action", "pi.test/inherent/waiting"])
    }
}
