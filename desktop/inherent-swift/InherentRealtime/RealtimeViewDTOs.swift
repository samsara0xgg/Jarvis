import Foundation

/// Codable mirrors of the ADR-0014 D10 message catalog and D8 snapshot frames.
///
/// These are the payloads the reducer consumes.  They follow the same rules as
/// `RealtimeProtocol.swift`: wire keys are spelled out in `CodingKeys` rather
/// than derived by a decoding strategy, unknown keys are ignored (D6), and
/// server-to-client frames are `Decodable` only — nothing in the client encodes
/// them, and card 1's `RuntimeCapabilities` (embedded in the snapshot) is
/// `Decodable` for the same reason.
///
/// Discriminated unions carry a `"kind"` string beside their fields.  An
/// unknown `kind` inside a durable `view.delta` throws: a durable mutation the
/// client cannot apply is a protocol violation, not something to skip (D6/D17).
/// An unknown ephemeral kind decodes to `.unknown` so the reducer can ignore
/// and meter it.

// MARK: - Vocabulary

/// D3 response phase.
public enum ResponsePhase: String, Decodable, Equatable, Sendable {
  case commentary
  case `final`
}

/// D3 response channel.
public enum ResponseChannel: String, Decodable, Equatable, Sendable {
  case speech
  case document
  case both
}

/// D3 ResponseRun lifecycle, keyed only by `response_id`.
public enum ResponseLifecycle: String, Decodable, Equatable, Sendable {
  case generating
  case waitingAction = "waiting_action"
  case finalizing
  case completed
  case cancelled
  case failed

  /// L3 owns exactly one of these per `response_id` (D3).
  public var isTerminal: Bool {
    switch self {
    case .completed, .cancelled, .failed: return true
    case .generating, .waitingAction, .finalizing: return false
    }
  }
}

/// D3 panel stream state, keyed by `response_id`.
public enum PanelStreamState: String, Decodable, Equatable, Sendable {
  case unopened
  case open
  case closed
  case failed

  public var isTerminal: Bool { self == .closed || self == .failed }
}

/// D3 playback lease phase, keyed by response + playback generation.
public enum PlaybackPhase: String, Decodable, Equatable, Sendable {
  case idle
  case buffering
  case speaking
  case ducked
  case draining
  case completed
  case interrupted
  case failed

  public var isTerminal: Bool {
    switch self {
    case .completed, .interrupted, .failed: return true
    case .idle, .buffering, .speaking, .ducked, .draining: return false
    }
  }
}

/// D3 canonical action state.
public enum ActionCanonicalState: String, Decodable, Equatable, Sendable {
  case proposed
  case authorized
  case dispatched
  case running
  case resultObserved = "result_observed"
  case failed
  case timeoutAssumed = "timeout_assumed"
  case cancelled

  public var isTerminal: Bool {
    switch self {
    case .resultObserved, .failed, .timeoutAssumed, .cancelled: return true
    case .proposed, .authorized, .dispatched, .running: return false
    }
  }
}

/// Why the globally unique confirmation slot was cleared (D10).
public enum ConfirmationClearReason: String, Decodable, Equatable, Sendable {
  case accepted
  case rejected
  case expired
  case superseded
}

/// How a committed utterance reached the daemon (D10).
public enum InputSource: String, Decodable, Equatable, Sendable {
  case text
  case voicePtt = "voice_ptt"
  case voiceWake = "voice_wake"
  case image
}

/// Severity of a projection-backed surface notice (D10).
public enum NoticeSeverity: String, Decodable, Equatable, Sendable {
  case info
  case warning
  case error
}

/// The ephemeral `playback.progress` state (D10).
public enum PlaybackProgressState: String, Decodable, Equatable, Sendable {
  case buffering
  case playing
  case draining
  case silenced
}

/// The ephemeral `input.state` value (D10); the local idle state is not on the wire.
public enum InputStateValue: String, Decodable, Equatable, Sendable {
  case listening
  case transcribing
  case committed
  case empty
  case failed
}

/// The discriminator every union in this file reads.
private enum KindKey: String, CodingKey {
  case kind
}

private func decodedKind(_ decoder: Decoder) throws -> String {
  try decoder.container(keyedBy: KindKey.self).decode(String.self, forKey: .kind)
}

// MARK: - Durable mutations (D10)

/// `response.opened`.
public struct ResponseOpened: Decodable, Equatable, Sendable {
  public var responseId: String
  public var responseGroupId: String
  public var turnId: String
  public var phase: ResponsePhase
  public var channel: ResponseChannel
  public var lifecycle: ResponseLifecycle
  public var question: String?
  public var summary: String?
  public var createdAtMs: Int
  public var revision: Int
  /// D21: the `request_id` of the v2 submission whose turn opened this group,
  /// or nil for a turn that did not come through the authenticated inbox.
  public var sourceClientRequestId: String?

  enum CodingKeys: String, CodingKey {
    case responseId = "response_id"
    case responseGroupId = "response_group_id"
    case turnId = "turn_id"
    case phase
    case channel
    case lifecycle
    case question
    case summary
    case createdAtMs = "created_at_ms"
    case revision
    case sourceClientRequestId = "source_client_request_id"
  }
}

/// `response.segment`.
public struct ResponseSegment: Decodable, Equatable, Sendable {
  public var responseId: String
  public var sequence: Int
  public var phase: ResponsePhase
  public var channel: ResponseChannel
  public var text: String
  public var segmentHash: String

  enum CodingKeys: String, CodingKey {
    case responseId = "response_id"
    case sequence
    case phase
    case channel
    case text
    case segmentHash = "segment_hash"
  }

  public init(from decoder: Decoder) throws {
    let container = try decoder.container(keyedBy: CodingKeys.self)
    responseId = try container.decode(String.self, forKey: .responseId)
    sequence = try container.decode(Int.self, forKey: .sequence)
    guard sequence >= 0 else {
      throw RealtimeProtocolError.invalidField(field: "sequence", reason: "must be >= 0")
    }
    phase = try container.decode(ResponsePhase.self, forKey: .phase)
    channel = try container.decode(ResponseChannel.self, forKey: .channel)
    text = try container.decode(String.self, forKey: .text)
    segmentHash = try container.decode(String.self, forKey: .segmentHash)
  }
}

/// `response.delivery`.
public struct ResponseDelivery: Decodable, Equatable, Sendable {
  public var responseId: String
  public var panelStream: PanelStreamState
  public var reason: String?

  enum CodingKeys: String, CodingKey {
    case responseId = "response_id"
    case panelStream = "panel_stream"
    case reason
  }
}

/// `response.lifecycle`.
public struct ResponseLifecycleChange: Decodable, Equatable, Sendable {
  public var responseId: String
  public var lifecycle: ResponseLifecycle
  public var terminalReason: String?
  public var revision: Int

  enum CodingKeys: String, CodingKey {
    case responseId = "response_id"
    case lifecycle
    case terminalReason = "terminal_reason"
    case revision
  }
}

/// `playback.state` — the durable playback checkpoint/terminal.
public struct PlaybackStateChange: Decodable, Equatable, Sendable {
  public var responseId: String
  public var playbackGenerationId: String
  public var phase: PlaybackPhase
  public var heardThroughSequence: Int?
  public var revision: Int

  enum CodingKeys: String, CodingKey {
    case responseId = "response_id"
    case playbackGenerationId = "playback_generation_id"
    case phase
    case heardThroughSequence = "heard_through_sequence"
    case revision
  }
}

/// `action.upsert`.
public struct ActionUpsert: Decodable, Equatable, Sendable {
  public var actionId: String
  public var responseGroupId: String?
  public var taskId: String?
  public var state: ActionCanonicalState
  public var label: String
  public var target: String?
  public var revision: Int
  public var cancellable: Bool
  public var freshnessMs: Int?

  enum CodingKeys: String, CodingKey {
    case actionId = "action_id"
    case responseGroupId = "response_group_id"
    case taskId = "task_id"
    case state
    case label
    case target
    case revision
    case cancellable
    case freshnessMs = "freshness_ms"
  }
}

/// `confirmation.upsert` — the globally unique live slot.
public struct ConfirmationUpsert: Decodable, Equatable, Sendable {
  public var confirmationId: String
  public var responseGroupId: String?
  public var actionId: String?
  public var summary: String
  public var target: String?
  public var risk: String
  public var options: [String]
  public var expiresAtMs: Int
  public var revision: Int

  enum CodingKeys: String, CodingKey {
    case confirmationId = "confirmation_id"
    case responseGroupId = "response_group_id"
    case actionId = "action_id"
    case summary
    case target
    case risk
    case options
    case expiresAtMs = "expires_at_ms"
    case revision
  }
}

/// `confirmation.cleared`.
public struct ConfirmationCleared: Decodable, Equatable, Sendable {
  public var confirmationId: String
  public var reason: ConfirmationClearReason
  public var revision: Int

  enum CodingKeys: String, CodingKey {
    case confirmationId = "confirmation_id"
    case reason
    case revision
  }
}

/// `input.committed`.
public struct InputCommitted: Decodable, Equatable, Sendable {
  public var utteranceId: String
  public var turnId: String
  public var responseGroupId: String
  public var source: InputSource
  public var text: String?

  enum CodingKeys: String, CodingKey {
    case utteranceId = "utterance_id"
    case turnId = "turn_id"
    case responseGroupId = "response_group_id"
    case source
    case text
  }
}

/// `surface.notice`.
public struct SurfaceNotice: Decodable, Equatable, Sendable {
  public var noticeId: String
  public var subjectRef: String?
  public var severity: NoticeSeverity
  public var code: String
  public var text: String?
  public var requiredAction: String?

  enum CodingKeys: String, CodingKey {
    case noticeId = "notice_id"
    case subjectRef = "subject_ref"
    case severity
    case code
    case text
    case requiredAction = "required_action"
  }
}

/// One durable mutation inside a `view.delta` (D10).
public enum ViewMutation: Decodable, Equatable, Sendable {
  case responseOpened(ResponseOpened)
  case responseSegment(ResponseSegment)
  case responseDelivery(ResponseDelivery)
  case responseLifecycle(ResponseLifecycleChange)
  case playbackState(PlaybackStateChange)
  case actionUpsert(ActionUpsert)
  case confirmationUpsert(ConfirmationUpsert)
  case confirmationCleared(ConfirmationCleared)
  case inputCommitted(InputCommitted)
  case surfaceNotice(SurfaceNotice)

  public init(from decoder: Decoder) throws {
    let kind = try decodedKind(decoder)
    switch kind {
    case "response.opened": self = .responseOpened(try ResponseOpened(from: decoder))
    case "response.segment": self = .responseSegment(try ResponseSegment(from: decoder))
    case "response.delivery": self = .responseDelivery(try ResponseDelivery(from: decoder))
    case "response.lifecycle":
      self = .responseLifecycle(try ResponseLifecycleChange(from: decoder))
    case "playback.state": self = .playbackState(try PlaybackStateChange(from: decoder))
    case "action.upsert": self = .actionUpsert(try ActionUpsert(from: decoder))
    case "confirmation.upsert": self = .confirmationUpsert(try ConfirmationUpsert(from: decoder))
    case "confirmation.cleared":
      self = .confirmationCleared(try ConfirmationCleared(from: decoder))
    case "input.committed": self = .inputCommitted(try InputCommitted(from: decoder))
    case "surface.notice": self = .surfaceNotice(try SurfaceNotice(from: decoder))
    default:
      throw RealtimeProtocolError.invalidField(
        field: "kind", reason: "unknown durable mutation \(kind)"
      )
    }
  }
}

/// The payload of one durable `view.delta` frame: one ordered mutation batch (D9).
public struct ViewDeltaPayload: Decodable, Equatable, Sendable {
  public var sourceEventUid: String
  public var changes: [ViewMutation]

  enum CodingKeys: String, CodingKey {
    case sourceEventUid = "source_event_uid"
    case changes
  }
}

// MARK: - Ephemeral updates (D10)

/// `input.state`.
public struct InputStateUpdate: Decodable, Equatable, Sendable {
  public var utteranceId: String
  public var revision: Int
  public var state: InputStateValue

  enum CodingKeys: String, CodingKey {
    case utteranceId = "utterance_id"
    case revision
    case state
  }
}

/// `input.partial` — never an authoritative transcript.
public struct InputPartialUpdate: Decodable, Equatable, Sendable {
  public var utteranceId: String
  public var revision: Int
  public var text: String

  enum CodingKeys: String, CodingKey {
    case utteranceId = "utterance_id"
    case revision
    case text
  }
}

/// `playback.progress` — only the L5 playback actor emits `state == .silenced`.
public struct PlaybackProgressUpdate: Decodable, Equatable, Sendable {
  public var responseId: String
  public var playbackGenerationId: String
  public var state: PlaybackProgressState
  public var heardThroughSequence: Int?
  public var silenceAtMonotonicNs: Int?
  public var cursorQuality: String?

  enum CodingKeys: String, CodingKey {
    case responseId = "response_id"
    case playbackGenerationId = "playback_generation_id"
    case state
    case heardThroughSequence = "heard_through_sequence"
    case silenceAtMonotonicNs = "silence_at_monotonic_ns"
    case cursorQuality = "cursor_quality"
  }
}

/// `action.progress_hint` — must point at a real ActionRun and never invent completion.
public struct ActionProgressHint: Decodable, Equatable, Sendable {
  public var actionId: String
  public var stage: String
  public var text: String?

  enum CodingKeys: String, CodingKey {
    case actionId = "action_id"
    case stage
    case text
  }
}

/// `connection.notice`.
public struct ConnectionNotice: Decodable, Equatable, Sendable {
  public var code: String
  public var text: String?
}

/// `ephemeral.clear` — removes exactly one evicted key.
public struct EphemeralClear: Decodable, Equatable, Sendable {
  public var key: String
}

/// One latest-value item inside an `ephemeral.baseline`.
///
/// `typed_payload` carries no `kind` of its own; `item_message_type` selects it.
public struct EphemeralBaselineItem: Decodable, Equatable, Sendable {
  public var key: String
  public var itemMessageType: String
  public var itemSequence: Int
  public var typedPayload: EphemeralUpdate

  enum CodingKeys: String, CodingKey {
    case key
    case itemMessageType = "item_message_type"
    case itemSequence = "item_sequence"
    case typedPayload = "typed_payload"
  }

  public init(from decoder: Decoder) throws {
    let container = try decoder.container(keyedBy: CodingKeys.self)
    key = try container.decode(String.self, forKey: .key)
    itemMessageType = try container.decode(String.self, forKey: .itemMessageType)
    itemSequence = try container.decode(Int.self, forKey: .itemSequence)
    typedPayload = try EphemeralUpdate(
      kind: itemMessageType, from: container.superDecoder(forKey: .typedPayload)
    )
  }
}

/// `ephemeral.baseline` — one atomic latest-value set plus a global watermark.
public struct EphemeralBaseline: Decodable, Equatable, Sendable {
  public var watermarkSequence: Int
  public var items: [EphemeralBaselineItem]

  enum CodingKeys: String, CodingKey {
    case watermarkSequence = "watermark_sequence"
    case items
  }
}

/// One ephemeral server update, discriminated by its D10 message type.
public enum EphemeralUpdate: Decodable, Equatable, Sendable {
  case inputState(InputStateUpdate)
  case inputPartial(InputPartialUpdate)
  case playbackProgress(PlaybackProgressUpdate)
  case actionProgressHint(ActionProgressHint)
  case connectionNotice(ConnectionNotice)
  case clear(EphemeralClear)
  case baseline(EphemeralBaseline)
  /// A kind this build does not know.  Ephemeral truth is disposable, so the
  /// reducer ignores and meters it instead of failing the socket.
  case unknown(kind: String)

  public init(from decoder: Decoder) throws {
    try self.init(kind: decodedKind(decoder), from: decoder)
  }

  init(kind: String, from decoder: Decoder) throws {
    switch kind {
    case "input.state": self = .inputState(try InputStateUpdate(from: decoder))
    case "input.partial": self = .inputPartial(try InputPartialUpdate(from: decoder))
    case "playback.progress":
      self = .playbackProgress(try PlaybackProgressUpdate(from: decoder))
    case "action.progress_hint":
      self = .actionProgressHint(try ActionProgressHint(from: decoder))
    case "connection.notice": self = .connectionNotice(try ConnectionNotice(from: decoder))
    case "ephemeral.clear": self = .clear(try EphemeralClear(from: decoder))
    case "ephemeral.baseline": self = .baseline(try EphemeralBaseline(from: decoder))
    default: self = .unknown(kind: kind)
    }
  }
}

// MARK: - Snapshot frames (D8)

/// The five D8 snapshot sections, in the order `snapshot.begin` advertises.
public enum SnapshotSection: String, Decodable, Equatable, Sendable {
  case responseGroups = "response_groups"
  case actions
  case pendingConfirmation = "pending_confirmation"
  case capabilities
  case surfaceNotices = "surface_notices"
}

/// One playback lease inside a snapshot response.
public struct PlaybackSnapshot: Decodable, Equatable, Sendable {
  public var playbackGenerationId: String
  public var phase: PlaybackPhase
  public var heardThroughSequence: Int?
  public var revision: Int

  enum CodingKeys: String, CodingKey {
    case playbackGenerationId = "playback_generation_id"
    case phase
    case heardThroughSequence = "heard_through_sequence"
    case revision
  }
}

/// One ResponseRun inside a snapshot group.
public struct ResponseSnapshot: Decodable, Equatable, Sendable {
  public var responseId: String
  public var phase: ResponsePhase
  public var channel: ResponseChannel
  public var lifecycle: ResponseLifecycle
  public var revision: Int
  public var panelStream: PanelStreamState
  public var segments: [ResponseSegment]
  public var playbacks: [PlaybackSnapshot]

  enum CodingKeys: String, CodingKey {
    case responseId = "response_id"
    case phase
    case channel
    case lifecycle
    case revision
    case panelStream = "panel_stream"
    case segments
    case playbacks
  }
}

/// One response group inside the `response_groups` section.
public struct ResponseGroupSnapshot: Decodable, Equatable, Sendable {
  public var responseGroupId: String
  public var turnId: String?
  public var question: String?
  public var createdAtMs: Int
  public var responses: [ResponseSnapshot]

  enum CodingKeys: String, CodingKey {
    case responseGroupId = "response_group_id"
    case turnId = "turn_id"
    case question
    case createdAtMs = "created_at_ms"
    case responses
  }
}

/// The `capabilities` section item.
public struct CapabilitiesSnapshot: Decodable, Equatable, Sendable {
  public var runtimeCapabilities: RuntimeCapabilities
  public var projectionFreshnessMs: Int?

  enum CodingKeys: String, CodingKey {
    case runtimeCapabilities = "runtime_capabilities"
    case projectionFreshnessMs = "projection_freshness_ms"
  }
}

/// `snapshot.begin`.
public struct SnapshotBegin: Decodable, Equatable, Sendable {
  public var snapshotId: String
  public var throughCursor: Int
  public var viewSchemaVersion: Int
  public var sectionOrder: [SnapshotSection]
  public var counts: [String: Int]

  enum CodingKeys: String, CodingKey {
    case snapshotId = "snapshot_id"
    case throughCursor = "through_cursor"
    case viewSchemaVersion = "view_schema_version"
    case sectionOrder = "section_order"
    case counts
  }
}

/// The items of one `snapshot.page`, decoded by the page's section.
public enum SnapshotPageItems: Equatable, Sendable {
  case responseGroups([ResponseGroupSnapshot])
  case actions([ActionUpsert])
  /// Zero or one; the slot is globally unique.
  case pendingConfirmation([ConfirmationUpsert])
  case capabilities([CapabilitiesSnapshot])
  case surfaceNotices([SurfaceNotice])
}

/// `snapshot.page`.
public struct SnapshotPage: Decodable, Equatable, Sendable {
  public var snapshotId: String
  public var section: SnapshotSection
  public var pageIndex: Int
  public var items: SnapshotPageItems

  enum CodingKeys: String, CodingKey {
    case snapshotId = "snapshot_id"
    case section
    case pageIndex = "page_index"
    case items
  }

  public init(from decoder: Decoder) throws {
    let container = try decoder.container(keyedBy: CodingKeys.self)
    snapshotId = try container.decode(String.self, forKey: .snapshotId)
    section = try container.decode(SnapshotSection.self, forKey: .section)
    pageIndex = try container.decode(Int.self, forKey: .pageIndex)
    switch section {
    case .responseGroups:
      items = .responseGroups(try container.decode([ResponseGroupSnapshot].self, forKey: .items))
    case .actions:
      items = .actions(try container.decode([ActionUpsert].self, forKey: .items))
    case .pendingConfirmation:
      items = .pendingConfirmation(try container.decode([ConfirmationUpsert].self, forKey: .items))
    case .capabilities:
      items = .capabilities(try container.decode([CapabilitiesSnapshot].self, forKey: .items))
    case .surfaceNotices:
      items = .surfaceNotices(try container.decode([SurfaceNotice].self, forKey: .items))
    }
  }
}

/// `snapshot.end`.
public struct SnapshotEnd: Decodable, Equatable, Sendable {
  public var snapshotId: String
  public var throughCursor: Int
  public var contentHash: String

  enum CodingKeys: String, CodingKey {
    case snapshotId = "snapshot_id"
    case throughCursor = "through_cursor"
    case contentHash = "content_hash"
  }
}

/// What the transport hands the reducer once staging matched the D8 end frame.
///
/// It is not a wire type: it exists only after snapshot ID, counts, schema,
/// through-cursor and content hash all verified.
public struct VerifiedSnapshot: Equatable, Sendable {
  public var snapshotId: String
  public var throughCursor: Int
  public var viewSchemaVersion: Int
  public var logEpoch: String
  public var bootId: String
  public var connectionId: String
  public var groups: [ResponseGroupSnapshot]
  public var actions: [ActionUpsert]
  public var pendingConfirmation: ConfirmationUpsert?
  public var capabilities: RuntimeCapabilities?
  public var notices: [SurfaceNotice]

  public init(
    snapshotId: String,
    throughCursor: Int,
    viewSchemaVersion: Int,
    logEpoch: String,
    bootId: String,
    connectionId: String,
    groups: [ResponseGroupSnapshot],
    actions: [ActionUpsert],
    pendingConfirmation: ConfirmationUpsert?,
    capabilities: RuntimeCapabilities?,
    notices: [SurfaceNotice]
  ) {
    self.snapshotId = snapshotId
    self.throughCursor = throughCursor
    self.viewSchemaVersion = viewSchemaVersion
    self.logEpoch = logEpoch
    self.bootId = bootId
    self.connectionId = connectionId
    self.groups = groups
    self.actions = actions
    self.pendingConfirmation = pendingConfirmation
    self.capabilities = capabilities
    self.notices = notices
  }
}
