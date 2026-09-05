import Foundation

/// Codable mirrors of the Inherent realtime v2 wire DTOs (ADR-0014 §5 D6/D7).
///
/// These types are the Swift half of `jarvis/surface/inherent_protocol.py` and
/// are checked against the same golden fixtures under
/// `tests/fixtures/inherent_v2/`.  Decoding is strict for the same reason it is
/// strict in Python: a transport that silently accepts `"2"` for a version or a
/// negative cursor cannot fail closed, and the damage only surfaces later as a
/// client applying deltas in the wrong order.  The single deliberate laxity is
/// forward compatibility — unknown keys are ignored (D6).
///
/// Wire keys are spelled out in each `CodingKeys` rather than produced by
/// `JSONDecoder.keyDecodingStrategy = .convertFromSnakeCase`: that strategy also
/// rewrites the keys of untyped `payload` objects (`source_event_uid` would
/// arrive as `sourceEventUid`), which Python's `payload: dict[str, Any]` never
/// does.

// MARK: - Errors

/// A rule from the Python DTOs that this frame broke.
public enum RealtimeProtocolError: Error, Equatable, Sendable {
  case invalidField(field: String, reason: String)
}

// MARK: - Shared vocabulary

/// Which ordering key a server frame carries, per D6.
public enum DeliveryClass: String, Codable, Equatable, Sendable {
  case durable
  case ephemeral
  case `protocol`
}

/// How the server decided the client should catch up, per D7.
public enum ResumeMode: String, Codable, Equatable, Sendable {
  case snapshot
  case incremental
}

/// An untyped JSON value, for payloads this card does not yet interpret.
///
/// Object keys are preserved exactly as they arrive on the wire.
public enum JSONValue: Decodable, Equatable, Sendable {
  case null
  case bool(Bool)
  case int(Int)
  case double(Double)
  case string(String)
  case array([JSONValue])
  case object([String: JSONValue])

  public init(from decoder: Decoder) throws {
    let container = try decoder.singleValueContainer()
    if container.decodeNil() {
      self = .null
    } else if let value = try? container.decode(Bool.self) {
      self = .bool(value)
    } else if let value = try? container.decode(Int.self) {
      self = .int(value)
    } else if let value = try? container.decode(Double.self) {
      self = .double(value)
    } else if let value = try? container.decode(String.self) {
      self = .string(value)
    } else if let value = try? container.decode([JSONValue].self) {
      self = .array(value)
    } else {
      self = .object(try container.decode([String: JSONValue].self))
    }
  }
}

/// The shape of an uninterpreted `payload`, matching Python's `dict[str, Any]`.
public typealias JSONObject = [String: JSONValue]

// MARK: - Field rules (mirroring the Python annotated types)

/// `Identity` — 1...128 characters.
///
/// Length is counted in Unicode scalars so the limit lands on the same strings
/// Python's `len()` rejects.
private func checkedIdentity(_ value: String, _ field: String) throws -> String {
  let length = value.unicodeScalars.count
  guard length >= 1, length <= RealtimeProtocol.identityMaxLength else {
    throw RealtimeProtocolError.invalidField(
      field: field,
      reason: "identity must be 1...\(RealtimeProtocol.identityMaxLength) characters"
    )
  }
  return value
}

/// `ShortText` — at most 256 characters.
private func checkedShortText(_ value: String, _ field: String) throws -> String {
  guard value.unicodeScalars.count <= RealtimeProtocol.shortTextMaxLength else {
    throw RealtimeProtocolError.invalidField(
      field: field,
      reason: "text must be at most \(RealtimeProtocol.shortTextMaxLength) characters"
    )
  }
  return value
}

/// `Cursor` / `EpochMs` — non-negative.
private func checkedNonNegative(_ value: Int, _ field: String) throws -> Int {
  guard value >= 0 else {
    throw RealtimeProtocolError.invalidField(field: field, reason: "must be >= 0")
  }
  return value
}

private func checkedVersionList(_ values: [Int], _ field: String) throws -> [Int] {
  guard !values.isEmpty else {
    throw RealtimeProtocolError.invalidField(field: field, reason: "must not be empty")
  }
  for value in values where value < 1 {
    throw RealtimeProtocolError.invalidField(field: field, reason: "versions must be >= 1")
  }
  return values
}

/// Decode a key that must be present but may be JSON `null`.
private func decodeRequiredNullable<Key: CodingKey, Value: Decodable>(
  _ type: Value.Type,
  forKey key: Key,
  in container: KeyedDecodingContainer<Key>
) throws -> Value? {
  guard container.contains(key) else {
    throw RealtimeProtocolError.invalidField(field: key.stringValue, reason: "required")
  }
  return try container.decodeIfPresent(type, forKey: key)
}

// MARK: - Client frames

/// Any frame the client sends over the v2 socket (D6).
///
/// `protocolVersion` is a plain positive integer rather than a hard 2: an
/// unsupported client must still decode far enough for hello to answer
/// `upgrade_required`.
public struct ClientEnvelope<Payload: Codable & Equatable & Sendable>: Codable, Equatable, Sendable {
  public var protocolVersion: Int
  public var messageType: String
  public var messageId: String
  public var clientInstanceId: String
  public var connectionId: String?
  public var sentAtMs: Int
  public var payload: Payload

  public init(
    protocolVersion: Int,
    messageType: String,
    messageId: String,
    clientInstanceId: String,
    connectionId: String?,
    sentAtMs: Int,
    payload: Payload
  ) {
    self.protocolVersion = protocolVersion
    self.messageType = messageType
    self.messageId = messageId
    self.clientInstanceId = clientInstanceId
    self.connectionId = connectionId
    self.sentAtMs = sentAtMs
    self.payload = payload
  }

  enum CodingKeys: String, CodingKey {
    case protocolVersion = "protocol_version"
    case messageType = "message_type"
    case messageId = "message_id"
    case clientInstanceId = "client_instance_id"
    case connectionId = "connection_id"
    case sentAtMs = "sent_at_ms"
    case payload
  }
}

extension ClientEnvelope {
  public init(from decoder: Decoder) throws {
    let container = try decoder.container(keyedBy: CodingKeys.self)
    let protocolVersion = try container.decode(Int.self, forKey: .protocolVersion)
    guard protocolVersion >= 1 else {
      throw RealtimeProtocolError.invalidField(field: "protocol_version", reason: "must be >= 1")
    }
    self.protocolVersion = protocolVersion
    messageType = try checkedIdentity(
      container.decode(String.self, forKey: .messageType), "message_type"
    )
    messageId = try checkedIdentity(container.decode(String.self, forKey: .messageId), "message_id")
    clientInstanceId = try checkedIdentity(
      container.decode(String.self, forKey: .clientInstanceId), "client_instance_id"
    )
    let rawConnectionId = try decodeRequiredNullable(
      String.self, forKey: CodingKeys.connectionId, in: container
    )
    connectionId = try rawConnectionId.map { try checkedIdentity($0, "connection_id") }
    sentAtMs = try checkedNonNegative(container.decode(Int.self, forKey: .sentAtMs), "sent_at_ms")
    payload = try container.decode(Payload.self, forKey: .payload)

    // Only the hello may omit connection_id, and it must omit it.
    let isHello = messageType == RealtimeProtocol.clientHelloMessageType
    if isHello, connectionId != nil {
      throw RealtimeProtocolError.invalidField(
        field: "connection_id", reason: "client.hello must not carry a connection_id"
      )
    }
    if !isHello, connectionId == nil {
      throw RealtimeProtocolError.invalidField(
        field: "connection_id", reason: "a post-hello client frame must carry a connection_id"
      )
    }
  }

  /// Nullable keys are written as `null` rather than dropped: the Python models
  /// require `connection_id` to be present.
  public func encode(to encoder: Encoder) throws {
    var container = encoder.container(keyedBy: CodingKeys.self)
    try container.encode(protocolVersion, forKey: .protocolVersion)
    try container.encode(messageType, forKey: .messageType)
    try container.encode(messageId, forKey: .messageId)
    try container.encode(clientInstanceId, forKey: .clientInstanceId)
    try container.encode(connectionId, forKey: .connectionId)
    try container.encode(sentAtMs, forKey: .sentAtMs)
    try container.encode(payload, forKey: .payload)
  }
}

/// The D7 hello payload: what the client is and what it already holds.
public struct ClientHelloPayload: Codable, Equatable, Sendable {
  public var supportedVersions: [Int]
  public var clientBuild: String
  public var viewSchemaVersions: [Int]
  public var capabilities: [String]
  public var lastLogEpoch: String?
  public var lastAppliedCursor: Int?
  public var hasCompleteLocalState: Bool

  public init(
    supportedVersions: [Int],
    clientBuild: String,
    viewSchemaVersions: [Int],
    capabilities: [String],
    lastLogEpoch: String?,
    lastAppliedCursor: Int?,
    hasCompleteLocalState: Bool
  ) {
    self.supportedVersions = supportedVersions
    self.clientBuild = clientBuild
    self.viewSchemaVersions = viewSchemaVersions
    self.capabilities = capabilities
    self.lastLogEpoch = lastLogEpoch
    self.lastAppliedCursor = lastAppliedCursor
    self.hasCompleteLocalState = hasCompleteLocalState
  }

  enum CodingKeys: String, CodingKey {
    case supportedVersions = "supported_versions"
    case clientBuild = "client_build"
    case viewSchemaVersions = "view_schema_versions"
    case capabilities
    case lastLogEpoch = "last_log_epoch"
    case lastAppliedCursor = "last_applied_cursor"
    case hasCompleteLocalState = "has_complete_local_state"
  }
}

extension ClientHelloPayload {
  public init(from decoder: Decoder) throws {
    let container = try decoder.container(keyedBy: CodingKeys.self)
    supportedVersions = try checkedVersionList(
      container.decode([Int].self, forKey: .supportedVersions), "supported_versions"
    )
    clientBuild = try checkedShortText(
      container.decode(String.self, forKey: .clientBuild), "client_build"
    )
    viewSchemaVersions = try checkedVersionList(
      container.decode([Int].self, forKey: .viewSchemaVersions), "view_schema_versions"
    )
    capabilities = try container.decode([String].self, forKey: .capabilities)
      .map { try checkedShortText($0, "capabilities") }
    let rawEpoch = try decodeRequiredNullable(
      String.self, forKey: CodingKeys.lastLogEpoch, in: container
    )
    lastLogEpoch = try rawEpoch.map { try checkedIdentity($0, "last_log_epoch") }
    let rawCursor = try decodeRequiredNullable(
      Int.self, forKey: CodingKeys.lastAppliedCursor, in: container
    )
    lastAppliedCursor = try rawCursor.map { try checkedNonNegative($0, "last_applied_cursor") }
    hasCompleteLocalState = try container.decode(Bool.self, forKey: .hasCompleteLocalState)
  }

  public func encode(to encoder: Encoder) throws {
    var container = encoder.container(keyedBy: CodingKeys.self)
    try container.encode(supportedVersions, forKey: .supportedVersions)
    try container.encode(clientBuild, forKey: .clientBuild)
    try container.encode(viewSchemaVersions, forKey: .viewSchemaVersions)
    try container.encode(capabilities, forKey: .capabilities)
    try container.encode(lastLogEpoch, forKey: .lastLogEpoch)
    try container.encode(lastAppliedCursor, forKey: .lastAppliedCursor)
    try container.encode(hasCompleteLocalState, forKey: .hasCompleteLocalState)
  }
}

/// The first frame on every accepted v2 socket (D7).
public typealias ClientHello = ClientEnvelope<ClientHelloPayload>

// MARK: - Server frames

/// Any frame the daemon sends over the v2 socket (D6).
///
/// The cursor rules are why this type exists: durable deltas are ordered by
/// `event_cursor`, ephemeral updates by a connection-scoped
/// `ephemeral_sequence`, and mixing the two would let a client ACK a cursor it
/// never received.
public struct ServerEnvelope<Payload: Decodable & Equatable & Sendable>: Decodable, Equatable, Sendable {
  public var protocolVersion: Int
  public var messageType: String
  public var messageId: String
  public var deliveryClass: DeliveryClass
  public var connectionId: String
  public var logEpoch: String
  public var bootId: String
  public var eventCursor: Int?
  public var ephemeralSequence: Int?
  public var sentAtMs: Int
  public var payload: Payload

  enum CodingKeys: String, CodingKey {
    case protocolVersion = "protocol_version"
    case messageType = "message_type"
    case messageId = "message_id"
    case deliveryClass = "delivery_class"
    case connectionId = "connection_id"
    case logEpoch = "log_epoch"
    case bootId = "boot_id"
    case eventCursor = "event_cursor"
    case ephemeralSequence = "ephemeral_sequence"
    case sentAtMs = "sent_at_ms"
    case payload
  }
}

extension ServerEnvelope {
  public init(from decoder: Decoder) throws {
    let container = try decoder.container(keyedBy: CodingKeys.self)
    protocolVersion = try container.decode(Int.self, forKey: .protocolVersion)
    guard protocolVersion == RealtimeProtocol.protocolVersion else {
      throw RealtimeProtocolError.invalidField(
        field: "protocol_version",
        reason: "server frames are version \(RealtimeProtocol.protocolVersion)"
      )
    }
    messageType = try checkedIdentity(
      container.decode(String.self, forKey: .messageType), "message_type"
    )
    messageId = try checkedIdentity(container.decode(String.self, forKey: .messageId), "message_id")
    deliveryClass = try container.decode(DeliveryClass.self, forKey: .deliveryClass)
    connectionId = try checkedIdentity(
      container.decode(String.self, forKey: .connectionId), "connection_id"
    )
    logEpoch = try checkedIdentity(container.decode(String.self, forKey: .logEpoch), "log_epoch")
    bootId = try checkedIdentity(container.decode(String.self, forKey: .bootId), "boot_id")
    eventCursor = try container.decodeIfPresent(Int.self, forKey: .eventCursor)
      .map { try checkedNonNegative($0, "event_cursor") }
    ephemeralSequence = try container.decodeIfPresent(Int.self, forKey: .ephemeralSequence)
      .map { try checkedNonNegative($0, "ephemeral_sequence") }
    sentAtMs = try checkedNonNegative(container.decode(Int.self, forKey: .sentAtMs), "sent_at_ms")
    payload = try container.decode(Payload.self, forKey: .payload)

    // Each delivery class carries exactly the ordering key it owns.
    switch deliveryClass {
    case .durable:
      guard eventCursor != nil, ephemeralSequence == nil else {
        throw RealtimeProtocolError.invalidField(
          field: "event_cursor",
          reason: "a durable frame carries event_cursor and no ephemeral_sequence"
        )
      }
    case .ephemeral:
      guard ephemeralSequence != nil, eventCursor == nil else {
        throw RealtimeProtocolError.invalidField(
          field: "ephemeral_sequence",
          reason: "an ephemeral frame carries ephemeral_sequence and no event_cursor"
        )
      }
    case .protocol:
      guard eventCursor == nil, ephemeralSequence == nil else {
        throw RealtimeProtocolError.invalidField(
          field: "delivery_class", reason: "a protocol frame carries neither cursor"
        )
      }
    }
  }
}

/// What this daemon can actually do right now, per D7.
public struct RuntimeCapabilities: Decodable, Equatable, Sendable {
  public var textInput: Bool
  public var imageInput: Bool
  public var voiceInput: Bool
  public var responseInterrupt: Bool
  public var actionCancel: Bool
  public var confirmationActions: Bool
  public var naturalBargeIn: Bool
  public var aecProfile: String

  enum CodingKeys: String, CodingKey {
    case textInput = "text_input"
    case imageInput = "image_input"
    case voiceInput = "voice_input"
    case responseInterrupt = "response_interrupt"
    case actionCancel = "action_cancel"
    case confirmationActions = "confirmation_actions"
    case naturalBargeIn = "natural_barge_in"
    case aecProfile = "aec_profile"
  }
}

extension RuntimeCapabilities {
  public init(from decoder: Decoder) throws {
    let container = try decoder.container(keyedBy: CodingKeys.self)
    textInput = try container.decode(Bool.self, forKey: .textInput)
    imageInput = try container.decode(Bool.self, forKey: .imageInput)
    voiceInput = try container.decode(Bool.self, forKey: .voiceInput)
    responseInterrupt = try container.decode(Bool.self, forKey: .responseInterrupt)
    actionCancel = try container.decode(Bool.self, forKey: .actionCancel)
    confirmationActions = try container.decode(Bool.self, forKey: .confirmationActions)
    naturalBargeIn = try container.decode(Bool.self, forKey: .naturalBargeIn)
    aecProfile = try checkedShortText(
      container.decode(String.self, forKey: .aecProfile), "aec_profile"
    )
  }
}

/// The D7 server answer: version, resume decision, and capabilities.
public struct ServerHelloPayload: Decodable, Equatable, Sendable {
  public var selectedVersion: Int
  public var viewSchemaVersion: Int
  public var resumeMode: ResumeMode
  public var serverHighWaterCursor: Int
  public var requiredClientCapabilities: [String]
  public var runtimeCapabilities: RuntimeCapabilities

  enum CodingKeys: String, CodingKey {
    case selectedVersion = "selected_version"
    case viewSchemaVersion = "view_schema_version"
    case resumeMode = "resume_mode"
    case serverHighWaterCursor = "server_high_water_cursor"
    case requiredClientCapabilities = "required_client_capabilities"
    case runtimeCapabilities = "runtime_capabilities"
  }
}

extension ServerHelloPayload {
  public init(from decoder: Decoder) throws {
    let container = try decoder.container(keyedBy: CodingKeys.self)
    selectedVersion = try container.decode(Int.self, forKey: .selectedVersion)
    guard selectedVersion == RealtimeProtocol.protocolVersion else {
      throw RealtimeProtocolError.invalidField(
        field: "selected_version",
        reason: "must be \(RealtimeProtocol.protocolVersion)"
      )
    }
    viewSchemaVersion = try container.decode(Int.self, forKey: .viewSchemaVersion)
    guard viewSchemaVersion == RealtimeProtocol.viewSchemaVersion else {
      throw RealtimeProtocolError.invalidField(
        field: "view_schema_version",
        reason: "must be \(RealtimeProtocol.viewSchemaVersion)"
      )
    }
    resumeMode = try container.decode(ResumeMode.self, forKey: .resumeMode)
    serverHighWaterCursor = try checkedNonNegative(
      container.decode(Int.self, forKey: .serverHighWaterCursor), "server_high_water_cursor"
    )
    requiredClientCapabilities = try container
      .decode([String].self, forKey: .requiredClientCapabilities)
      .map { try checkedShortText($0, "required_client_capabilities") }
    runtimeCapabilities = try container.decode(
      RuntimeCapabilities.self, forKey: .runtimeCapabilities
    )
  }
}

/// The daemon's reply to a supported `client.hello` (D7).
public typealias ServerHello = ServerEnvelope<ServerHelloPayload>

// MARK: - Constants and decoders

/// The wire constants and the entry points that decode a frame.
public enum RealtimeProtocol {
  public static let protocolVersion = 2
  public static let viewSchemaVersion = 1
  public static let maxClientFrameBytes = 64 * 1024
  public static let requiredClientCapabilities = ["paged_snapshot", "transport_ack"]
  public static let helloTimeoutSeconds: TimeInterval = 2.0
  public static let initialMaxFramesPerSecond = 50

  public static let clientHelloMessageType = "client.hello"
  public static let serverHelloMessageType = "server.hello"

  public static let identityMaxLength = 128
  public static let shortTextMaxLength = 256

  public static func decodeClientHello(_ data: Data) throws -> ClientHello {
    let hello = try JSONDecoder().decode(ClientHello.self, from: data)
    guard hello.messageType == clientHelloMessageType else {
      throw RealtimeProtocolError.invalidField(
        field: "message_type", reason: "must be \(clientHelloMessageType)"
      )
    }
    return hello
  }

  public static func decodeServerEnvelope(_ data: Data) throws -> ServerEnvelope<JSONObject> {
    try JSONDecoder().decode(ServerEnvelope<JSONObject>.self, from: data)
  }

  public static func decodeServerHello(_ data: Data) throws -> ServerHello {
    let hello = try JSONDecoder().decode(ServerHello.self, from: data)
    guard hello.messageType == serverHelloMessageType else {
      throw RealtimeProtocolError.invalidField(
        field: "message_type", reason: "must be \(serverHelloMessageType)"
      )
    }
    guard hello.deliveryClass == .protocol else {
      throw RealtimeProtocolError.invalidField(
        field: "delivery_class", reason: "server.hello is a protocol frame"
      )
    }
    return hello
  }

  public static func encode(_ value: some Encodable) throws -> Data {
    let encoder = JSONEncoder()
    encoder.outputFormatting = .sortedKeys
    return try encoder.encode(value)
  }
}
