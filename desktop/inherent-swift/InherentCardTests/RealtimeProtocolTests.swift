import XCTest
@testable import InherentCard

/// Swift half of the ADR-0014 D6/D7 wire contract, checked against the same
/// golden fixtures the Python DTO tests decode.
final class RealtimeProtocolTests: XCTestCase {
  /// `tests/fixtures/inherent_v2/` relative to this source file:
  /// InherentCardTests -> inherent-swift -> desktop -> repo root.
  private static let fixturesDirectory: URL = {
    var url = URL(fileURLWithPath: #filePath)
    for _ in 0..<4 { url.deleteLastPathComponent() }
    return url.appendingPathComponent("tests/fixtures/inherent_v2")
  }()

  private func fixture(_ name: String) throws -> Data {
    try Data(contentsOf: Self.fixturesDirectory.appendingPathComponent(name))
  }

  // MARK: - Fixture decoding

  func test_clientHelloFixtureDecodes() throws {
    let hello = try RealtimeProtocol.decodeClientHello(fixture("client.hello.json"))
    XCTAssertEqual(hello.protocolVersion, 2)
    XCTAssertEqual(hello.messageType, "client.hello")
    XCTAssertEqual(hello.messageId, "Rfixture0001")
    XCTAssertEqual(hello.clientInstanceId, "Ifixture0001")
    XCTAssertNil(hello.connectionId)
    XCTAssertEqual(hello.sentAtMs, 1_788_200_000_000)
    XCTAssertEqual(hello.payload.supportedVersions, [2])
    XCTAssertEqual(hello.payload.viewSchemaVersions, [1])
    XCTAssertEqual(hello.payload.clientBuild, "fixture")
    XCTAssertEqual(
      hello.payload.capabilities,
      ["paged_snapshot", "transport_ack", "response_control", "confirmation_control"]
    )
    XCTAssertNil(hello.payload.lastLogEpoch)
    XCTAssertEqual(hello.payload.lastAppliedCursor, 0)
    XCTAssertFalse(hello.payload.hasCompleteLocalState)
  }

  func test_clientHelloUnknownFieldsAreIgnored() throws {
    let base = try RealtimeProtocol.decodeClientHello(fixture("client.hello.json"))
    let extended = try RealtimeProtocol.decodeClientHello(
      fixture("client.hello.unknown-field.json")
    )
    XCTAssertEqual(base, extended)
  }

  func test_serverHelloFixtureDecodes() throws {
    let hello = try RealtimeProtocol.decodeServerHello(fixture("server.hello.json"))
    XCTAssertEqual(hello.deliveryClass, .protocol)
    XCTAssertEqual(hello.connectionId, "Cfixture0001")
    XCTAssertEqual(hello.logEpoch, "Lfixture0001")
    XCTAssertEqual(hello.bootId, "Bfixture0001")
    XCTAssertNil(hello.eventCursor)
    XCTAssertNil(hello.ephemeralSequence)
    XCTAssertEqual(hello.sentAtMs, 1_788_200_000_001)
    XCTAssertEqual(hello.payload.selectedVersion, 2)
    XCTAssertEqual(hello.payload.viewSchemaVersion, 1)
    XCTAssertEqual(hello.payload.resumeMode, .snapshot)
    XCTAssertEqual(hello.payload.serverHighWaterCursor, 1840)
    XCTAssertEqual(hello.payload.requiredClientCapabilities, ["paged_snapshot", "transport_ack"])
    XCTAssertEqual(
      hello.payload.runtimeCapabilities,
      RuntimeCapabilities(
        textInput: true,
        imageInput: false,
        voiceInput: true,
        responseInterrupt: true,
        actionCancel: true,
        confirmationActions: true,
        naturalBargeIn: false,
        aecProfile: "headphones_only"
      )
    )
  }

  func test_serverHelloUnknownFieldsAreIgnored() throws {
    let base = try RealtimeProtocol.decodeServerHello(fixture("server.hello.json"))
    let extended = try RealtimeProtocol.decodeServerHello(
      fixture("server.hello.unknown-field.json")
    )
    XCTAssertEqual(base, extended)
  }

  func test_durableFixtureDecodesWithRawPayloadKeys() throws {
    let frame = try RealtimeProtocol.decodeServerEnvelope(fixture("server.durable.json"))
    XCTAssertEqual(frame.messageType, "view.delta")
    XCTAssertEqual(frame.deliveryClass, .durable)
    XCTAssertEqual(frame.eventCursor, 1842)
    XCTAssertNil(frame.ephemeralSequence)
    // Payload keys stay exactly as Python's dict[str, Any] would hold them.
    XCTAssertEqual(frame.payload["source_event_uid"], .string("Rfixture0001"))
    XCTAssertEqual(frame.payload["changes"], .array([]))
  }

  func test_ephemeralFixtureDecodes() throws {
    let frame = try RealtimeProtocol.decodeServerEnvelope(fixture("server.ephemeral.json"))
    XCTAssertEqual(frame.messageType, "input.partial")
    XCTAssertEqual(frame.deliveryClass, .ephemeral)
    XCTAssertEqual(frame.ephemeralSequence, 7)
    XCTAssertNil(frame.eventCursor)
    XCTAssertEqual(frame.payload["text"], .string("你好"))
  }

  func test_protocolFixtureDecodes() throws {
    let frame = try RealtimeProtocol.decodeServerEnvelope(fixture("server.protocol.json"))
    XCTAssertEqual(frame.messageType, "connection.notice")
    XCTAssertEqual(frame.deliveryClass, .protocol)
    XCTAssertNil(frame.eventCursor)
    XCTAssertNil(frame.ephemeralSequence)
    XCTAssertEqual(frame.payload["notice"], .string("hold"))
  }

  // MARK: - Rejection

  func test_everyMalformedFixtureIsRejected() throws {
    let raw = try JSONSerialization.jsonObject(with: fixture("malformed.json"))
    let entries = try XCTUnwrap(raw as? [[String: Any]])
    XCTAssertGreaterThanOrEqual(entries.count, 15, "the malformed corpus lost entries")

    for entry in entries {
      let name = try XCTUnwrap(entry["name"] as? String)
      let kind = try XCTUnwrap(entry["kind"] as? String)
      let frame = try XCTUnwrap(entry["frame"])
      let data = try JSONSerialization.data(withJSONObject: frame)
      XCTAssertThrowsError(try Self.decode(kind: kind, data: data), "\(kind)/\(name) must fail")
    }
  }

  private static func decode(kind: String, data: Data) throws {
    switch kind {
    case "client_hello": _ = try RealtimeProtocol.decodeClientHello(data)
    case "server": _ = try RealtimeProtocol.decodeServerEnvelope(data)
    case "server_hello": _ = try RealtimeProtocol.decodeServerHello(data)
    default: XCTFail("unknown malformed kind \(kind)")
    }
  }

  // MARK: - Hello builder

  func test_helloBuilderEmitsTheD7Keys() throws {
    let hello = RealtimeTransportV2.makeHello(
      clientInstanceId: "Ifixture0001",
      messageId: "Rfixture0001",
      sentAtMs: 1_788_200_000_000,
      clientBuild: "fixture"
    )
    let data = try RealtimeProtocol.encode(hello)
    let json = try XCTUnwrap(
      try JSONSerialization.jsonObject(with: data) as? [String: Any]
    )
    XCTAssertEqual(
      Set(json.keys),
      [
        "protocol_version", "message_type", "message_id", "client_instance_id",
        "connection_id", "sent_at_ms", "payload",
      ]
    )
    // Python requires the key, so nil must serialize as null rather than vanish.
    XCTAssertTrue(json["connection_id"] is NSNull)

    let payload = try XCTUnwrap(json["payload"] as? [String: Any])
    XCTAssertEqual(
      Set(payload.keys),
      [
        "supported_versions", "client_build", "view_schema_versions", "capabilities",
        "last_log_epoch", "last_applied_cursor", "has_complete_local_state",
      ]
    )
    XCTAssertEqual(payload["supported_versions"] as? [Int], [2])
    XCTAssertEqual(payload["view_schema_versions"] as? [Int], [1])
    XCTAssertEqual(
      payload["capabilities"] as? [String],
      ["paged_snapshot", "transport_ack", "response_control", "confirmation_control"]
    )
    XCTAssertTrue(payload["last_log_epoch"] is NSNull)
    XCTAssertTrue(payload["last_applied_cursor"] is NSNull)

    // The bytes survive the strict decoder unchanged, so Python accepts them too.
    XCTAssertEqual(try RealtimeProtocol.decodeClientHello(data), hello)
  }

  // MARK: - server.hello validation

  private func serverHelloFixture() throws -> ServerHello {
    try RealtimeProtocol.decodeServerHello(fixture("server.hello.json"))
  }

  func test_validateAcceptsTheServerHelloFixture() throws {
    let hello = try serverHelloFixture()
    let payload = try RealtimeTransportV2.validate(
      serverHello: hello, expectedMessageId: "Rfixture0001"
    )
    XCTAssertEqual(payload.resumeMode, .snapshot)
    XCTAssertEqual(payload.serverHighWaterCursor, 1840)
  }

  func test_validateRejectsWrongSelectedVersion() throws {
    var hello = try serverHelloFixture()
    hello.payload.selectedVersion = 3
    XCTAssertThrowsError(
      try RealtimeTransportV2.validate(serverHello: hello, expectedMessageId: "Rfixture0001")
    )
  }

  func test_validateRejectsUnsupportedRequiredCapability() throws {
    var hello = try serverHelloFixture()
    hello.payload.requiredClientCapabilities = ["paged_snapshot", "time_travel"]
    XCTAssertThrowsError(
      try RealtimeTransportV2.validate(serverHello: hello, expectedMessageId: "Rfixture0001")
    )
  }

  func test_validateRejectsWrongMessageId() throws {
    let hello = try serverHelloFixture()
    XCTAssertThrowsError(
      try RealtimeTransportV2.validate(serverHello: hello, expectedMessageId: "Rother0001")
    )
  }

  func test_validateRejectsNonProtocolDeliveryClass() throws {
    var hello = try serverHelloFixture()
    hello.deliveryClass = .durable
    XCTAssertThrowsError(
      try RealtimeTransportV2.validate(serverHello: hello, expectedMessageId: "Rfixture0001")
    )
  }

  // MARK: - Token file

  func test_loadTokenTrimsTrailingNewline() throws {
    let path = FileManager.default.temporaryDirectory
      .appendingPathComponent("jarvis-v2-token-\(UUID().uuidString)")
    try "abc123\n".write(to: path, atomically: true, encoding: .utf8)
    defer { try? FileManager.default.removeItem(at: path) }
    XCTAssertEqual(try RealtimeTransportV2.loadToken(atPath: path.path), "abc123")
  }

  func test_loadTokenRefusesEmptyFile() throws {
    let path = FileManager.default.temporaryDirectory
      .appendingPathComponent("jarvis-v2-token-\(UUID().uuidString)")
    try "\n".write(to: path, atomically: true, encoding: .utf8)
    defer { try? FileManager.default.removeItem(at: path) }
    XCTAssertThrowsError(try RealtimeTransportV2.loadToken(atPath: path.path))
  }

  func test_transportStaysDisabled() {
    XCTAssertFalse(RealtimeTransportV2.enabled)
  }
}
