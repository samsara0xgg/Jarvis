import XCTest
@testable import InherentCard

final class BridgeDispatchTests: XCTestCase {
  final class StubDispatcher: BridgeDispatcher {
    var calls: [(String, [String: Any]?)] = []
    func siriOpen(payload: [String: Any]?)   { calls.append(("open", payload)) }
    func siriAppend(payload: [String: Any]?) { calls.append(("append", payload)) }
    func siriDone(payload: [String: Any]?)   { calls.append(("done", payload)) }
    func siriReset()                         { calls.append(("reset", nil)) }
    func voiceState(payload: [String: Any]?) { calls.append(("voice", payload)) }
  }

  func test_routeOpen() {
    let stub = StubDispatcher()
    BridgeMessageRouter.dispatch(json: ["op": "open", "payload": ["content": "hi"]], to: stub)
    XCTAssertEqual(stub.calls.count, 1)
    XCTAssertEqual(stub.calls[0].0, "open")
  }

  func test_routeAppend() {
    let stub = StubDispatcher()
    BridgeMessageRouter.dispatch(json: ["op": "append", "payload": ["token": "x"]], to: stub)
    XCTAssertEqual(stub.calls[0].0, "append")
  }

  func test_routeDone() {
    let stub = StubDispatcher()
    BridgeMessageRouter.dispatch(json: ["op": "done"], to: stub)
    XCTAssertEqual(stub.calls[0].0, "done")
  }

  func test_routeReset() {
    let stub = StubDispatcher()
    BridgeMessageRouter.dispatch(json: ["op": "reset"], to: stub)
    XCTAssertEqual(stub.calls[0].0, "reset")
  }

  func test_routeVoiceState() {
    let stub = StubDispatcher()
    BridgeMessageRouter.dispatch(json: ["op": "voice", "payload": ["phase": "listening"]], to: stub)
    XCTAssertEqual(stub.calls[0].0, "voice")
    XCTAssertEqual(stub.calls[0].1?["phase"] as? String, "listening")
  }

  func test_unknownOpIgnored() {
    let stub = StubDispatcher()
    BridgeMessageRouter.dispatch(json: ["op": "garbage"], to: stub)
    XCTAssertTrue(stub.calls.isEmpty)
  }

  func test_malformedIgnored() {
    let stub = StubDispatcher()
    BridgeMessageRouter.dispatch(json: ["nope": "wrong"], to: stub)
    XCTAssertTrue(stub.calls.isEmpty)
  }
}
