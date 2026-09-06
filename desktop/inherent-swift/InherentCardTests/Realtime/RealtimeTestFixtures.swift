import Foundation
@testable import InherentRealtime

/// Builders for the reducer tests.
///
/// Every fixture is built as the JSON the server actually sends and then
/// decoded through the real DTOs: the wire types are `Decodable` only, and a
/// test that hand-assembled Swift values would be testing a shape no socket
/// ever produces.
enum Fx {
  static let connectionID = "C-1"
  static let logEpoch = "L-1"
  static let bootID = "B-1"

  /// The reducer ignores it; the injected clock exists so it can never grow a
  /// wall clock of its own (D4).
  static let now = ContinuousClock().now

  static func decode<T: Decodable>(_ type: T.Type, _ object: Any) throws -> T {
    try JSONDecoder().decode(type, from: JSONSerialization.data(withJSONObject: object))
  }

  // MARK: - Durable mutations (D10)

  static func opened(
    _ responseID: String,
    group: String,
    turn: String = "T-1",
    phase: String = "final",
    channel: String = "document",
    lifecycle: String = "generating",
    question: String? = nil,
    revision: Int = 1,
    sourceRequest: String? = nil
  ) -> [String: Any] {
    var mutation: [String: Any] = [
      "kind": "response.opened", "response_id": responseID, "response_group_id": group,
      "turn_id": turn, "phase": phase, "channel": channel, "lifecycle": lifecycle,
      "created_at_ms": 1_788_200_000_000, "revision": revision,
    ]
    if let question { mutation["question"] = question }
    if let sourceRequest { mutation["source_client_request_id"] = sourceRequest }
    return mutation
  }

  static func segment(
    _ responseID: String,
    _ sequence: Int,
    text: String? = nil,
    hash: String? = nil,
    phase: String = "final",
    channel: String = "document"
  ) -> [String: Any] {
    [
      "kind": "response.segment", "response_id": responseID, "sequence": sequence,
      "phase": phase, "channel": channel, "text": text ?? "s\(sequence)",
      "segment_hash": hash ?? "h\(responseID)-\(sequence)",
    ]
  }

  static func delivery(
    _ responseID: String, _ panelStream: String, reason: String? = nil
  ) -> [String: Any] {
    var mutation: [String: Any] = [
      "kind": "response.delivery", "response_id": responseID, "panel_stream": panelStream,
    ]
    if let reason { mutation["reason"] = reason }
    return mutation
  }

  static func lifecycle(
    _ responseID: String, _ value: String, revision: Int, reason: String? = nil
  ) -> [String: Any] {
    var mutation: [String: Any] = [
      "kind": "response.lifecycle", "response_id": responseID, "lifecycle": value,
      "revision": revision,
    ]
    if let reason { mutation["terminal_reason"] = reason }
    return mutation
  }

  static func playback(
    _ responseID: String, _ generation: String, _ phase: String, revision: Int, heard: Int? = nil
  ) -> [String: Any] {
    var mutation: [String: Any] = [
      "kind": "playback.state", "response_id": responseID,
      "playback_generation_id": generation, "phase": phase, "revision": revision,
    ]
    if let heard { mutation["heard_through_sequence"] = heard }
    return mutation
  }

  static func action(
    _ actionID: String,
    group: String? = nil,
    state: String,
    label: String = "run the task",
    revision: Int,
    cancellable: Bool = true,
    cancel: [String: Any]? = nil
  ) -> [String: Any] {
    var mutation: [String: Any] = [
      "kind": "action.upsert", "action_id": actionID, "state": state, "label": label,
      "revision": revision, "cancellable": cancellable,
      "cancel_request": cancel ?? NSNull(),
    ]
    if let group { mutation["response_group_id"] = group }
    return mutation
  }

  static func confirmation(
    _ confirmationID: String,
    group: String? = nil,
    action: String? = nil,
    summary: String = "unlock the door",
    revision: Int
  ) -> [String: Any] {
    var mutation: [String: Any] = [
      "kind": "confirmation.upsert", "confirmation_id": confirmationID, "summary": summary,
      "risk": "high", "options": ["accept", "reject"], "expires_at_ms": 1_788_200_030_000,
      "revision": revision,
    ]
    if let group { mutation["response_group_id"] = group }
    if let action { mutation["action_id"] = action }
    return mutation
  }

  static func confirmationCleared(
    _ confirmationID: String, reason: String = "accepted", revision: Int
  ) -> [String: Any] {
    [
      "kind": "confirmation.cleared", "confirmation_id": confirmationID, "reason": reason,
      "revision": revision,
    ]
  }

  static func inputCommitted(
    utterance: String, turn: String, group: String, text: String? = nil, source: String = "text"
  ) -> [String: Any] {
    var mutation: [String: Any] = [
      "kind": "input.committed", "utterance_id": utterance, "turn_id": turn,
      "response_group_id": group, "source": source,
    ]
    if let text { mutation["text"] = text }
    return mutation
  }

  static func notice(
    _ noticeID: String, subject: String? = nil, severity: String = "warning",
    code: String = "tts_unavailable"
  ) -> [String: Any] {
    var mutation: [String: Any] = [
      "kind": "surface.notice", "notice_id": noticeID, "severity": severity, "code": code,
    ]
    if let subject { mutation["subject_ref"] = subject }
    return mutation
  }

  // MARK: - Envelopes

  /// Message ids follow the cursor, so every durable frame in a run is unique
  /// and a deliberate duplicate is byte-identical to its original.
  static func durable(
    cursor: Int,
    _ changes: [[String: Any]],
    messageID: String? = nil,
    connection: String = connectionID,
    logEpoch: String = logEpoch,
    boot: String = bootID
  ) throws -> ServerEnvelope<ViewDeltaPayload> {
    try decode(
      ServerEnvelope<ViewDeltaPayload>.self,
      [
        "protocol_version": 2, "message_type": "view.delta",
        "message_id": messageID ?? "M-\(cursor)", "delivery_class": "durable",
        "connection_id": connection, "log_epoch": logEpoch, "boot_id": boot,
        "event_cursor": cursor, "sent_at_ms": 1_788_200_000_000,
        "payload": ["source_event_uid": "E-\(cursor)", "changes": changes],
      ]
    )
  }

  static func ephemeral(
    sequence: Int,
    _ payload: [String: Any],
    messageID: String? = nil,
    connection: String = connectionID,
    logEpoch: String = logEpoch,
    boot: String = bootID
  ) throws -> ServerEnvelope<EphemeralUpdate> {
    try decode(
      ServerEnvelope<EphemeralUpdate>.self,
      [
        "protocol_version": 2, "message_type": "ephemeral",
        "message_id": messageID ?? "EM-\(sequence)", "delivery_class": "ephemeral",
        "connection_id": connection, "log_epoch": logEpoch, "boot_id": boot,
        "ephemeral_sequence": sequence, "sent_at_ms": 1_788_200_000_000,
        "payload": payload,
      ]
    )
  }

  // MARK: - Ephemeral payloads (D10)

  static func inputState(_ utterance: String, revision: Int, state: String) -> [String: Any] {
    ["kind": "input.state", "utterance_id": utterance, "revision": revision, "state": state]
  }

  static func inputPartial(_ utterance: String, revision: Int, text: String) -> [String: Any] {
    ["kind": "input.partial", "utterance_id": utterance, "revision": revision, "text": text]
  }

  static func playbackProgress(
    _ responseID: String, _ generation: String, _ state: String, heard: Int? = nil
  ) -> [String: Any] {
    var payload: [String: Any] = [
      "kind": "playback.progress", "response_id": responseID,
      "playback_generation_id": generation, "state": state,
    ]
    if let heard { payload["heard_through_sequence"] = heard }
    return payload
  }

  static func progressHint(_ actionID: String, stage: String, text: String? = nil) -> [String: Any]
  {
    var payload: [String: Any] = [
      "kind": "action.progress_hint", "action_id": actionID, "stage": stage,
    ]
    if let text { payload["text"] = text }
    return payload
  }

  static func ephemeralClear(_ key: String) -> [String: Any] {
    ["kind": "ephemeral.clear", "key": key]
  }

  static func baseline(
    watermark: Int, items: [(key: String, type: String, sequence: Int, payload: [String: Any])]
  ) -> [String: Any] {
    [
      "kind": "ephemeral.baseline", "watermark_sequence": watermark,
      "items": items.map {
        [
          "key": $0.key, "item_message_type": $0.type, "item_sequence": $0.sequence,
          "typed_payload": $0.payload,
        ]
      },
    ]
  }

  // MARK: - Hello and snapshot

  static func capabilities(voiceInput: Bool = true) -> [String: Any] {
    [
      "text_input": true, "image_input": true, "voice_input": voiceInput,
      "response_interrupt": true, "action_cancel": true, "confirmation_actions": true,
      "natural_barge_in": false, "aec_profile": "mac_builtin",
    ]
  }

  static func hello(voiceInput: Bool = true) throws -> ServerHelloPayload {
    try decode(
      ServerHelloPayload.self,
      [
        "selected_version": 2, "view_schema_version": 1, "resume_mode": "snapshot",
        "server_high_water_cursor": 0,
        "required_client_capabilities": ["paged_snapshot", "transport_ack"],
        "runtime_capabilities": capabilities(voiceInput: voiceInput),
      ]
    )
  }

  static func socketOpened(
    epoch: Int = 1,
    connection: String = connectionID,
    logEpoch: String = logEpoch,
    boot: String = bootID,
    voiceInput: Bool = true
  ) throws -> InherentClientEvent {
    .socketOpened(
      socketEpoch: epoch, connectionID: connection, hello: try hello(voiceInput: voiceInput),
      logEpoch: logEpoch, bootID: boot
    )
  }

  static func snapshotPlayback(
    _ generation: String, phase: String, revision: Int = 1, heard: Int? = nil
  ) -> [String: Any] {
    var item: [String: Any] = [
      "playback_generation_id": generation, "phase": phase, "revision": revision,
    ]
    if let heard { item["heard_through_sequence"] = heard }
    return item
  }

  static func snapshotResponse(
    _ responseID: String,
    phase: String = "final",
    channel: String = "document",
    lifecycle: String = "generating",
    revision: Int = 1,
    panel: String = "open",
    segments: [[String: Any]] = [],
    playbacks: [[String: Any]] = []
  ) -> [String: Any] {
    [
      "response_id": responseID, "phase": phase, "channel": channel, "lifecycle": lifecycle,
      "revision": revision, "panel_stream": panel, "segments": segments, "playbacks": playbacks,
    ]
  }

  static func snapshotGroup(
    _ groupID: String,
    turn: String? = nil,
    question: String? = nil,
    createdAtMs: Int = 1_788_200_000_000,
    responses: [[String: Any]] = [],
    sourceRequest: String? = nil
  ) -> [String: Any] {
    var group: [String: Any] = [
      "response_group_id": groupID, "created_at_ms": createdAtMs, "responses": responses,
    ]
    if let turn { group["turn_id"] = turn }
    if let question { group["question"] = question }
    if let sourceRequest { group["source_client_request_id"] = sourceRequest }
    return group
  }

  static func snapshot(
    id: String = "SNAP-1",
    through: Int,
    groups: [[String: Any]] = [],
    actions: [[String: Any]] = [],
    pendingConfirmation: [String: Any]? = nil,
    capabilities: [String: Any]? = nil,
    notices: [[String: Any]] = [],
    logEpoch: String = logEpoch,
    boot: String = bootID,
    connection: String = connectionID
  ) throws -> VerifiedSnapshot {
    VerifiedSnapshot(
      snapshotId: id,
      throughCursor: through,
      viewSchemaVersion: 1,
      logEpoch: logEpoch,
      bootId: boot,
      connectionId: connection,
      groups: try decode([ResponseGroupSnapshot].self, groups),
      actions: try decode([ActionUpsert].self, actions),
      pendingConfirmation: try pendingConfirmation.map { try decode(ConfirmationUpsert.self, $0) },
      capabilities: try capabilities.map { try decode(RuntimeCapabilities.self, $0) },
      notices: try decode([SurfaceNotice].self, notices)
    )
  }
}

/// One state plus the effects it produced, so a test reads as a transcript.
struct Reducing {
  var state = InherentUXState()
  private(set) var effects: [InherentEffect] = []

  @discardableResult
  mutating func apply(_ event: InherentClientEvent) -> [InherentEffect] {
    let produced = InherentReducer.reduce(state: &state, event: event, now: Fx.now)
    effects += produced
    return produced
  }

  /// Opens a socket and adopts a snapshot: the shortest path to a live client.
  @discardableResult
  mutating func goLive(
    epoch: Int = 1,
    through: Int = 0,
    groups: [[String: Any]] = [],
    actions: [[String: Any]] = [],
    pendingConfirmation: [String: Any]? = nil,
    boot: String = Fx.bootID,
    connection: String = Fx.connectionID,
    voiceInput: Bool = true
  ) throws -> [InherentEffect] {
    apply(try Fx.socketOpened(epoch: epoch, connection: connection, boot: boot, voiceInput: voiceInput))
    return apply(
      .snapshotAdopted(
        socketEpoch: epoch,
        try Fx.snapshot(
          through: through, groups: groups, actions: actions,
          pendingConfirmation: pendingConfirmation, boot: boot, connection: connection
        )
      )
    )
  }

  @discardableResult
  mutating func durable(
    cursor: Int, _ changes: [[String: Any]], epoch: Int = 1, boot: String = Fx.bootID,
    messageID: String? = nil, logEpoch: String = Fx.logEpoch
  ) throws -> [InherentEffect] {
    apply(
      .durable(
        socketEpoch: epoch,
        try Fx.durable(cursor: cursor, changes, messageID: messageID, logEpoch: logEpoch, boot: boot)
      )
    )
  }

  @discardableResult
  mutating func ephemeral(
    sequence: Int, _ payload: [String: Any], epoch: Int = 1, boot: String = Fx.bootID,
    connection: String = Fx.connectionID
  ) throws -> [InherentEffect] {
    apply(
      .ephemeral(
        socketEpoch: epoch,
        try Fx.ephemeral(sequence: sequence, payload, connection: connection, boot: boot)
      )
    )
  }

  func response(_ responseID: String, in groupID: String) -> ResponseState? {
    state.responseGroups[ResponseGroupID(groupID)]?.responses[ResponseID(responseID)]
  }

  func texts(_ responseID: String, in groupID: String) -> [String] {
    response(responseID, in: groupID)?.segments.map(\.text) ?? []
  }
}

/// A deterministic PRNG for the property test: same seed, same sequence, on
/// every machine and every Swift release.
struct SplitMix64: RandomNumberGenerator {
  private var state: UInt64

  init(seed: UInt64) { state = seed }

  mutating func next() -> UInt64 {
    state &+= 0x9E37_79B9_7F4A_7C15
    var z = state
    z = (z ^ (z >> 30)) &* 0xBF58_476D_1CE4_E5B9
    z = (z ^ (z >> 27)) &* 0x94D0_49BB_1331_11EB
    return z ^ (z >> 31)
  }

  /// True with probability `probability`, drawn from the same stream.
  mutating func chance(_ probability: Double) -> Bool {
    Double(next() >> 11) * (1.0 / 9_007_199_254_740_992.0) < probability
  }

  mutating func index(below limit: Int) -> Int {
    Int(next() % UInt64(limit))
  }
}
