import Foundation

/// The ADR-0014 D4 reducer vocabulary: what goes in, and what comes out.
///
/// Everything the reducer emits is a value.  The runner performs the network
/// and presentation work outside the reducer, and its results come back as new
/// events; nothing here mutates state.

/// Why the client asked the server to resynchronize (D17).
public enum ResyncReason: String, Sendable, Equatable {
  /// A durable cursor below `last_applied_cursor` that is not a known duplicate.
  case cursorRegression = "cursor_regression"
  /// A panel sequence past the expected one (rule 9): emitted once per gap.
  case gap
  /// A 33rd buffered segment (rule 10): the rendered prefix is kept.
  case gapOverflow = "gap_overflow"
  case unknownResponse = "unknown_response"
  case terminalConflict = "terminal_conflict"
  case logEpochChanged = "log_epoch_changed"
  case hashConflict = "hash_conflict"
  case schemaMismatch = "schema_mismatch"
}

/// The D19 local events.  None of these is server truth and none leaves the client.
public enum LocalPresentationEvent: Sendable, Equatable {
  /// Hiding the window changes only `presentation.isVisible`.
  case setVisible(Bool)
  case selectGroup(ResponseGroupID?)
  case toggleActionExpanded(ActionID)
  case setDraft(String)
  case setReduceMotion(Bool)
  case setPanelFrame(PanelFrame?)
  case setScrollAnchor(String?)
}

/// Everything the reducer accepts.  Socket-scoped cases carry the epoch they
/// belong to so a dead connection cannot alter the live one (D17 rule 1).
public enum InherentClientEvent: Sendable, Equatable {
  case socketOpened(
    socketEpoch: Int,
    connectionID: String,
    hello: ServerHelloPayload,
    logEpoch: String,
    bootID: String
  )
  case socketClosed(socketEpoch: Int, code: Int?, reason: String)
  case socketFailed(socketEpoch: Int, reason: String)
  case reconnecting(socketEpoch: Int)
  /// Handed over only after the transport verified the D8 content hash.
  case snapshotAdopted(socketEpoch: Int, VerifiedSnapshot)
  case snapshotFailed(socketEpoch: Int, reason: String)
  case durable(socketEpoch: Int, ServerEnvelope<ViewDeltaPayload>)
  /// The `scheduleAckFlush` deadline elapsed (D11 rule 8's time half).
  case ackDeadline(socketEpoch: Int)
  case ephemeral(socketEpoch: Int, ServerEnvelope<EphemeralUpdate>)
  case local(LocalPresentationEvent)
}

/// Everything the reducer emits.  There is deliberately no speech or play
/// effect: snapshot adoption never creates one (D8).
public enum InherentEffect: Sendable, Equatable {
  /// D8 step 4 with the snapshot id, or D11 rule 6's cumulative durable ACK
  /// with none: `throughCursor` is what the MainActor has applied.
  case sendAck(snapshotID: String?, throughCursor: EventCursor)
  /// D11 rule 8: come back with `.ackDeadline` after this, so a burst that
  /// stops never leaves an applied durable frame unACKed.
  case scheduleAckFlush(after: Duration)
  case requestResync(reason: ResyncReason)
  case scheduleLocalFade(groupID: ResponseGroupID, after: Duration)
  case announceAccessibilityMilestone(String)
}

/// Performs effects outside the reducer (D4).
public protocol RealtimeEffectRunner: Sendable {
  func run(_ effects: [InherentEffect]) async
}

/// The test runner: it records intents instead of performing them.
public actor RecordingEffectRunner: RealtimeEffectRunner {
  public private(set) var recorded: [InherentEffect] = []

  public init() {}

  public func run(_ effects: [InherentEffect]) async {
    recorded.append(contentsOf: effects)
  }
}
