import CryptoKit
import XCTest
@testable import InherentRealtime

/// ADR-0014 D21, client half: the retry that cannot duplicate a turn, the
/// refusal that must not be retried, and the pending entry a matching
/// `response.opened` clears.
final class InputSubmissionClientTests: XCTestCase {
  /// A transport that answers from a script and records what it was asked to send.
  private actor FakeTransport: InputSubmissionTransport {
    enum Step: Sendable {
      case lost
      case answer(status: Int, body: String)
    }

    private var script: [Step]
    private(set) var sent: [InputSubmissionHTTPRequest] = []

    init(_ script: [Step]) { self.script = script }

    func perform(
      _ request: InputSubmissionHTTPRequest
    ) async throws -> InputSubmissionHTTPResponse {
      sent.append(request)
      guard !script.isEmpty else {
        XCTFail("the client sent more requests than the script answers")
        return InputSubmissionHTTPResponse(statusCode: 500, body: Data())
      }
      switch script.removeFirst() {
      case .lost:
        throw URLError(.networkConnectionLost)
      case .answer(let status, let body):
        return InputSubmissionHTTPResponse(statusCode: status, body: Data(body.utf8))
      }
    }

    func recorded() -> [InputSubmissionHTTPRequest] { sent }
  }

  private static let receiptJSON = """
    {"status":"accepted","request_id":"R-1","input_event_uid":"E-9","turn_id":"T-9"}
    """

  private func client(_ transport: FakeTransport) -> InputSubmissionClient {
    InputSubmissionClient(
      transport: transport,
      clientInstanceID: "I-1",
      bearerToken: "tok",
      newRequestID: { "R-1" },
      nowMs: { 1_788_200_000_000 }
    )
  }

  /// A lost answer is retried with the identical bytes, so the server resolves
  /// the original receipt instead of starting a second turn.
  func testALostAnswerIsRetriedByteIdentically() async throws {
    let transport = FakeTransport([.lost, .answer(status: 200, body: Self.receiptJSON)])
    let (pending, receipt) = try await client(transport).submitText("明天下雨吗")

    let sent = await transport.recorded()
    XCTAssertEqual(sent.count, 2)
    XCTAssertEqual(sent[0], sent[1])
    XCTAssertEqual(pending.requestID, "R-1")
    XCTAssertEqual(receipt.turnID, "T-9")
    XCTAssertEqual(receipt.inputEventUID, "E-9")
  }

  /// The 409 is the client's own bookkeeping being wrong: surface it, never
  /// resend, and never mint a fresh id that would submit the turn twice.
  func testAPayloadConflictSurfacesAsAnErrorAndIsNotRetried() async throws {
    let transport = FakeTransport([.answer(status: 409, body: "{}")])
    do {
      _ = try await client(transport).submitText("hello")
      XCTFail("a 409 must not be reported as an accepted receipt")
    } catch let error as InputSubmissionError {
      XCTAssertEqual(error, .payloadConflict)
    }
    let sent = await transport.recorded()
    XCTAssertEqual(sent.count, 1)
  }

  func testAnUnauthorizedSubmissionSurfacesAsSuchAndIsNotRetried() async throws {
    let transport = FakeTransport([.answer(status: 403, body: "{}")])
    do {
      _ = try await client(transport).submitText("hello")
      XCTFail("a 403 must not be reported as an accepted receipt")
    } catch let error as InputSubmissionError {
      XCTAssertEqual(error, .unauthorized)
    }
    let sent = await transport.recorded()
    XCTAssertEqual(sent.count, 1)
  }

  /// Two lost answers in a row is a fact the caller needs, not something to
  /// keep hiding behind further retries.
  func testTwoLostAnswersReportUnreachable() async throws {
    let transport = FakeTransport([.lost, .lost])
    do {
      _ = try await client(transport).submitText("hello")
      XCTFail("an unreachable daemon must not look accepted")
    } catch let error as InputSubmissionError {
      XCTAssertEqual(error, .unreachable)
    }
  }

  /// The multipart request carries the digest of the exact uploaded bytes, and
  /// a retry of it is byte-identical too.
  func testAnAudioSubmissionCarriesItsDigestAndRetriesIdentically() async throws {
    let wav = Data("RIFFfake-wav-bytes".utf8)
    let transport = FakeTransport([.lost, .answer(status: 200, body: Self.receiptJSON)])
    _ = try await client(transport).submitAudio(wav: wav)

    let sent = await transport.recorded()
    XCTAssertEqual(sent.count, 2)
    XCTAssertEqual(sent[0], sent[1])
    XCTAssertEqual(sent[0].path, InputSubmissionClient.asrPath)
    let body = String(decoding: sent[0].body, as: UTF8.self)
    // The digest VALUE, not just the field name: hashing the wrong bytes has
    // to fail here, since the server refuses a mismatch with 400.
    let expected = SHA256.hash(data: wav).map { String(format: "%02x", $0) }.joined()
    XCTAssertTrue(body.contains("name=\"audio_sha256\"\r\n\r\n\(expected)\r\n"))
    XCTAssertTrue(body.contains("name=\"audio\"; filename=\"u.wav\""))
    XCTAssertTrue(body.contains("R-1"))
  }

  // MARK: - The pending entry

  private func pending(_ requestID: String, at submittedAtMs: Int = 1) -> PendingInputState {
    PendingInputState(requestID: requestID, text: "hi", submittedAtMs: submittedAtMs)
  }

  /// The correlation the panel depends on: the group the server opened for this
  /// submission clears the local pending entry.
  func testAMatchingOpenedResolvesThePendingInput() throws {
    var run = Reducing()
    try run.goLive()
    run.apply(.local(.inputSubmitted(pending("R-1"))))
    XCTAssertEqual(Set(run.state.pendingInputs.keys), ["R-1"])

    run.apply(
      .durable(
        socketEpoch: 1,
        try Fx.durable(cursor: 1, [Fx.opened("RESP-1", group: "G-1", sourceRequest: "R-1")])
      )
    )

    XCTAssertTrue(run.state.pendingInputs.isEmpty)
    XCTAssertNotNil(run.state.responseGroups[ResponseGroupID("G-1")])
  }

  /// Another window's submission, or a turn that never came from this inbox at
  /// all: the group is still created and nothing local is disturbed.
  func testAnUnknownOrAbsentRequestIDLeavesThePendingInputAlone() throws {
    var run = Reducing()
    try run.goLive()
    run.apply(.local(.inputSubmitted(pending("R-1"))))

    run.apply(
      .durable(
        socketEpoch: 1,
        try Fx.durable(cursor: 1, [Fx.opened("RESP-2", group: "G-2", sourceRequest: "R-other")])
      )
    )
    run.apply(
      .durable(socketEpoch: 2, try Fx.durable(cursor: 2, [Fx.opened("RESP-3", group: "G-3")]))
    )

    XCTAssertEqual(Set(run.state.pendingInputs.keys), ["R-1"])
    XCTAssertNotNil(run.state.responseGroups[ResponseGroupID("G-2")])
    XCTAssertNotNil(run.state.responseGroups[ResponseGroupID("G-3")])
  }

  /// A submission whose turn never opens a response cannot grow the map for the
  /// life of the process.
  func testThePendingMapIsBounded() throws {
    var run = Reducing()
    try run.goLive()
    for index in 0..<(pendingInputLimit + 5) {
      run.apply(.local(.inputSubmitted(pending("R-\(index)", at: index))))
    }

    XCTAssertEqual(run.state.pendingInputs.count, pendingInputLimit)
    XCTAssertNil(run.state.pendingInputs["R-0"])
    XCTAssertNotNil(run.state.pendingInputs["R-\(pendingInputLimit + 4)"])
  }

  /// The added wire field is optional: a frame without it still decodes.
  func testTheOpenedDTODecodesWithAndWithoutTheField() throws {
    let without = try Fx.decode(ResponseOpened.self, Fx.opened("RESP-1", group: "G-1"))
    XCTAssertNil(without.sourceClientRequestId)
    let with = try Fx.decode(
      ResponseOpened.self, Fx.opened("RESP-1", group: "G-1", sourceRequest: "R-1")
    )
    XCTAssertEqual(with.sourceClientRequestId, "R-1")
  }
}
