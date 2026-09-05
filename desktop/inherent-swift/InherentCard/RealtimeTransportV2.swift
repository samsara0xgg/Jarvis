import Foundation

/// State of the v2 handshake, from `connect()` to a validated `server.hello`.
enum RealtimeTransportV2State: Equatable, Sendable {
  case idle
  case connecting
  case helloSent
  case ready(ServerHelloPayload)
  case failed(String)
}

/// The dormant ADR-0014 D7 client transport.
///
/// It opens `/inherent/ws/v2` with the Bearer token the launcher points at,
/// sends `client.hello`, and validates the single `server.hello` that answers.
/// Nothing in the card references it yet: `enabled` is false and snapshot,
/// deltas and reconnect belong to later cards.
///
/// The token is read from a file and put in one place only — the `Authorization`
/// header.  It is never logged, never put in the URL, and never included in a
/// failure string.
actor RealtimeTransportV2 {
  /// The feature switch.  Stays false until the v2 socket carries real state.
  static let enabled = false

  static let url = URL(string: "ws://127.0.0.1:8006/inherent/ws/v2")!
  static let tokenPathEnvironmentKey = "JARVIS_INHERENT_V2_TOKEN_PATH"

  /// The four D7 client capabilities this build implements.
  static let clientCapabilities = [
    "paged_snapshot",
    "transport_ack",
    "response_control",
    "confirmation_control",
  ]
  static let clientBuild = "inherent-card-swift"

  private(set) var state: RealtimeTransportV2State = .idle

  // MARK: - Token

  /// The token file path the launcher passes in the environment.
  static func tokenPath(
    environment: [String: String] = ProcessInfo.processInfo.environment
  ) -> String? {
    environment[tokenPathEnvironmentKey]
  }

  /// Read the token, trimming the trailing newline the writer appends.
  static func loadToken(atPath path: String) throws -> String {
    let raw = try String(contentsOfFile: path, encoding: .utf8)
    let token = raw.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !token.isEmpty else {
      throw RealtimeProtocolError.invalidField(field: "token", reason: "token file is empty")
    }
    return token
  }

  // MARK: - Handshake

  static func makeHello(
    clientInstanceId: String,
    messageId: String,
    sentAtMs: Int,
    clientBuild: String = RealtimeTransportV2.clientBuild
  ) -> ClientHello {
    ClientHello(
      protocolVersion: RealtimeProtocol.protocolVersion,
      messageType: RealtimeProtocol.clientHelloMessageType,
      messageId: messageId,
      clientInstanceId: clientInstanceId,
      connectionId: nil,
      sentAtMs: sentAtMs,
      payload: ClientHelloPayload(
        supportedVersions: [RealtimeProtocol.protocolVersion],
        clientBuild: clientBuild,
        viewSchemaVersions: [RealtimeProtocol.viewSchemaVersion],
        capabilities: clientCapabilities,
        lastLogEpoch: nil,
        lastAppliedCursor: nil,
        hasCompleteLocalState: false
      )
    )
  }

  /// Accept the server's answer, or say which D7 rule it broke.
  @discardableResult
  static func validate(
    serverHello: ServerHello,
    expectedMessageId: String
  ) throws -> ServerHelloPayload {
    guard serverHello.messageType == RealtimeProtocol.serverHelloMessageType else {
      throw RealtimeProtocolError.invalidField(
        field: "message_type", reason: "must be \(RealtimeProtocol.serverHelloMessageType)"
      )
    }
    guard serverHello.deliveryClass == .protocol else {
      throw RealtimeProtocolError.invalidField(
        field: "delivery_class", reason: "server.hello is a protocol frame"
      )
    }
    guard serverHello.messageId == expectedMessageId else {
      throw RealtimeProtocolError.invalidField(
        field: "message_id", reason: "must echo the client.hello message_id"
      )
    }
    let payload = serverHello.payload
    guard payload.selectedVersion == RealtimeProtocol.protocolVersion else {
      throw RealtimeProtocolError.invalidField(
        field: "selected_version", reason: "must be \(RealtimeProtocol.protocolVersion)"
      )
    }
    guard payload.viewSchemaVersion == RealtimeProtocol.viewSchemaVersion else {
      throw RealtimeProtocolError.invalidField(
        field: "view_schema_version", reason: "must be \(RealtimeProtocol.viewSchemaVersion)"
      )
    }
    guard payload.resumeMode == .snapshot else {
      throw RealtimeProtocolError.invalidField(
        field: "resume_mode", reason: "this build only resumes by snapshot"
      )
    }
    let missing = payload.requiredClientCapabilities.filter {
      !clientCapabilities.contains($0)
    }
    guard missing.isEmpty else {
      throw RealtimeProtocolError.invalidField(
        field: "required_client_capabilities", reason: "unsupported: \(missing.joined(separator: ","))"
      )
    }
    return payload
  }

  // MARK: - Connect

  /// Open the socket, say hello, and stop at the validated answer.
  func connect(tokenPath: String? = RealtimeTransportV2.tokenPath()) async {
    state = .connecting
    do {
      guard let tokenPath else {
        throw RealtimeProtocolError.invalidField(
          field: RealtimeTransportV2.tokenPathEnvironmentKey, reason: "not set"
        )
      }
      let token = try RealtimeTransportV2.loadToken(atPath: tokenPath)
      var request = URLRequest(url: RealtimeTransportV2.url)
      request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
      let task = URLSession(configuration: .default).webSocketTask(with: request)
      task.resume()
      defer { task.cancel(with: .goingAway, reason: nil) }

      let hello = RealtimeTransportV2.makeHello(
        clientInstanceId: RealtimeTransportV2.mintIdentity(prefix: "I"),
        messageId: RealtimeTransportV2.mintIdentity(prefix: "R"),
        sentAtMs: Int(Date().timeIntervalSince1970 * 1000)
      )
      let frame = try RealtimeProtocol.encode(hello)
      try await task.send(.string(String(decoding: frame, as: UTF8.self)))
      state = .helloSent

      let answer = try await task.receive()
      let data: Data
      switch answer {
      case .string(let text): data = Data(text.utf8)
      case .data(let bytes): data = bytes
      @unknown default:
        throw RealtimeProtocolError.invalidField(field: "frame", reason: "unknown frame kind")
      }
      let payload = try RealtimeTransportV2.validate(
        serverHello: RealtimeProtocol.decodeServerHello(data),
        expectedMessageId: hello.messageId
      )
      state = .ready(payload)
    } catch {
      state = .failed(String(describing: error))
    }
  }

  private static func mintIdentity(prefix: String) -> String {
    prefix + UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
  }
}
