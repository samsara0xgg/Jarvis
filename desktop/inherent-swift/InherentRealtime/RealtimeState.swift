import Foundation

/// The ADR-0014 D3 state families, the D17 synchronization baselines, and the
/// D19 local presentation state.
///
/// Every type here is a pure `Equatable, Sendable` value type.  The file is
/// deliberately free of frameworks, sockets, wall clocks and queues: state is
/// what the reducer folds, and the reducer receives time as an injected
/// `ContinuousClock.Instant`.
///
/// The computed helpers at the bottom (`backgroundShelf`, `pinnedConfirmation`,
/// `groupPresentation`) are derived, never stored: D18 says the reducer decides
/// selection, and a stored copy of a derivation is a second truth.

// MARK: - Identities

/// `response_id` is never reused (D3).
public struct ResponseID: Hashable, Sendable, Codable {
  public let rawValue: String
  public init(_ rawValue: String) { self.rawValue = rawValue }
}

public struct ResponseGroupID: Hashable, Sendable, Codable {
  public let rawValue: String
  public init(_ rawValue: String) { self.rawValue = rawValue }
}

/// Exists only after the L5 playback actor activates a speech lease (D3).
public struct PlaybackGenerationID: Hashable, Sendable, Codable {
  public let rawValue: String
  public init(_ rawValue: String) { self.rawValue = rawValue }
}

public struct ActionID: Hashable, Sendable, Codable {
  public let rawValue: String
  public init(_ rawValue: String) { self.rawValue = rawValue }
}

public struct ConfirmationID: Hashable, Sendable, Codable {
  public let rawValue: String
  public init(_ rawValue: String) { self.rawValue = rawValue }
}

public struct TurnID: Hashable, Sendable, Codable {
  public let rawValue: String
  public init(_ rawValue: String) { self.rawValue = rawValue }
}

public struct UtteranceID: Hashable, Sendable, Codable {
  public let rawValue: String
  public init(_ rawValue: String) { self.rawValue = rawValue }
}

/// The durable ordering key: `events.id` as the server counts it.
public typealias EventCursor = Int

// MARK: - Connection and synchronization

/// D3 connection family.
public enum ConnectionState: String, Equatable, Sendable {
  case disconnected
  case connecting
  case synchronizing
  case live
  case reconnecting
  case offline
}

/// Everything D17 says the store must track to order and deduplicate durables.
public struct SynchronizationState: Equatable, Sendable {
  /// D17 rule 1: frames from an older epoch are ignored.
  public var activeSocketEpoch: Int
  public var activeConnectionID: String?
  public var logEpoch: String?
  public var bootID: String?
  /// 0 before any snapshot adoption.
  public var lastAppliedCursor: EventCursor
  public var snapshotThroughCursor: EventCursor
  /// FIFO, bounded by `recentMessageIDLimit` (D17 rules 3 and 4).
  public var recentMessageIDs: [String]
  public var lastEphemeralSequence: Int
  /// Set when a resync effect was emitted; cleared by snapshot adoption.
  public var resyncRequested: Bool
  /// D17 rule 10: the rendered prefix is complete but the tail needs a snapshot.
  public var needsSnapshotRepair: Bool

  public static let recentMessageIDLimit = 256

  public init(
    activeSocketEpoch: Int = 0,
    activeConnectionID: String? = nil,
    logEpoch: String? = nil,
    bootID: String? = nil,
    lastAppliedCursor: EventCursor = 0,
    snapshotThroughCursor: EventCursor = 0,
    recentMessageIDs: [String] = [],
    lastEphemeralSequence: Int = 0,
    resyncRequested: Bool = false,
    needsSnapshotRepair: Bool = false
  ) {
    self.activeSocketEpoch = activeSocketEpoch
    self.activeConnectionID = activeConnectionID
    self.logEpoch = logEpoch
    self.bootID = bootID
    self.lastAppliedCursor = lastAppliedCursor
    self.snapshotThroughCursor = snapshotThroughCursor
    self.recentMessageIDs = recentMessageIDs
    self.lastEphemeralSequence = lastEphemeralSequence
    self.resyncRequested = resyncRequested
    self.needsSnapshotRepair = needsSnapshotRepair
  }
}

// MARK: - Input

/// D3 input family, plus the ephemeral fields `input.state` / `input.partial` fill.
public enum InputPhase: String, Equatable, Sendable {
  case idle
  case listening
  case transcribing
  case committed
  case empty
  case failed
}

public struct InputPresentationState: Equatable, Sendable {
  public var phase: InputPhase
  public var utteranceID: UtteranceID?
  public var revision: Int
  public var partialText: String?

  public init(
    phase: InputPhase = .idle,
    utteranceID: UtteranceID? = nil,
    revision: Int = 0,
    partialText: String? = nil
  ) {
    self.phase = phase
    self.utteranceID = utteranceID
    self.revision = revision
    self.partialText = partialText
  }
}

// MARK: - Responses

public struct ResponseSegmentState: Equatable, Sendable {
  public var sequence: Int
  public var phase: ResponsePhase
  public var channel: ResponseChannel
  public var text: String
  public var segmentHash: String

  public init(
    sequence: Int,
    phase: ResponsePhase,
    channel: ResponseChannel,
    text: String,
    segmentHash: String
  ) {
    self.sequence = sequence
    self.phase = phase
    self.channel = channel
    self.text = text
    self.segmentHash = segmentHash
  }
}

/// What the badge shows for one playback lease.
///
/// `stopping` and `stopped` have no wire phase of their own: a durable
/// `interrupted` terminal records the canonical outcome but is not proof the
/// device buffer tail is inaudible (D10), so the badge only reaches `stopped`
/// once a matching `playback.progress state=silenced` arrives.
public enum PlaybackPresentation: String, Equatable, Sendable {
  case idle
  case buffering
  case speaking
  case ducked
  case draining
  case stopping
  case stopped
  case completed
  case failed
}

public struct PlaybackState: Equatable, Sendable {
  public var generationID: PlaybackGenerationID
  public var phase: PlaybackPhase
  public var revision: Int
  public var heardThroughSequence: Int?
  /// Set only by a matching-boot, matching-generation `state=silenced` progress.
  public var silenced: Bool
  public var progress: PlaybackProgressState?

  public init(
    generationID: PlaybackGenerationID,
    phase: PlaybackPhase = .idle,
    revision: Int = 0,
    heardThroughSequence: Int? = nil,
    silenced: Bool = false,
    progress: PlaybackProgressState? = nil
  ) {
    self.generationID = generationID
    self.phase = phase
    self.revision = revision
    self.heardThroughSequence = heardThroughSequence
    self.silenced = silenced
    self.progress = progress
  }

  public var presentation: PlaybackPresentation {
    switch phase {
    case .interrupted: return silenced ? .stopped : .stopping
    case .idle: return .idle
    case .buffering: return .buffering
    case .speaking: return .speaking
    case .ducked: return .ducked
    case .draining: return .draining
    case .completed: return .completed
    case .failed: return .failed
    }
  }
}

public struct ResponseState: Equatable, Sendable {
  public var responseID: ResponseID
  public var responseGroupID: ResponseGroupID
  public var lifecycle: ResponseLifecycle
  public var lifecycleRevision: EventCursor
  public var phase: ResponsePhase
  public var channel: ResponseChannel
  public var panelStream: PanelStreamState
  public var terminalReason: String?
  /// Rendered segments only: contiguous from 0 (D17 rules 7 to 9).
  public var segments: [ResponseSegmentState]
  public var expectedSequence: Int
  /// Segments past the gap, never rendered, at most `gapBufferLimit` of them.
  public var gapBuffer: [Int: ResponseSegmentState]
  public var playbacks: [PlaybackGenerationID: PlaybackState]
  public var activePlaybackGenerationID: PlaybackGenerationID?

  public static let gapBufferLimit = 32

  public init(
    responseID: ResponseID,
    responseGroupID: ResponseGroupID,
    lifecycle: ResponseLifecycle = .generating,
    lifecycleRevision: EventCursor = 0,
    phase: ResponsePhase = .final,
    channel: ResponseChannel = .document,
    panelStream: PanelStreamState = .unopened,
    terminalReason: String? = nil,
    segments: [ResponseSegmentState] = [],
    expectedSequence: Int = 0,
    gapBuffer: [Int: ResponseSegmentState] = [:],
    playbacks: [PlaybackGenerationID: PlaybackState] = [:],
    activePlaybackGenerationID: PlaybackGenerationID? = nil
  ) {
    self.responseID = responseID
    self.responseGroupID = responseGroupID
    self.lifecycle = lifecycle
    self.lifecycleRevision = lifecycleRevision
    self.phase = phase
    self.channel = channel
    self.panelStream = panelStream
    self.terminalReason = terminalReason
    self.segments = segments
    self.expectedSequence = expectedSequence
    self.gapBuffer = gapBuffer
    self.playbacks = playbacks
    self.activePlaybackGenerationID = activePlaybackGenerationID
  }
}

public struct ResponseGroupState: Equatable, Sendable {
  public var id: ResponseGroupID
  public var turnID: TurnID?
  public var question: String?
  public var createdAtMs: Int
  public var responses: [ResponseID: ResponseState]
  /// Creation order.
  public var responseOrder: [ResponseID]
  public var notices: [SurfaceNotice]

  public init(
    id: ResponseGroupID,
    turnID: TurnID? = nil,
    question: String? = nil,
    createdAtMs: Int = 0,
    responses: [ResponseID: ResponseState] = [:],
    responseOrder: [ResponseID] = [],
    notices: [SurfaceNotice] = []
  ) {
    self.id = id
    self.turnID = turnID
    self.question = question
    self.createdAtMs = createdAtMs
    self.responses = responses
    self.responseOrder = responseOrder
    self.notices = notices
  }
}

// MARK: - Actions and confirmations

/// D3 local command overlay: what the user asked for, not canonical truth.
public enum ActionCommandOverlay: String, Equatable, Sendable {
  case none
  case cancelSubmitting = "cancel_submitting"
  case cancelRequested = "cancel_requested"
  case cancelRejected = "cancel_rejected"
}

public struct ActionViewState: Equatable, Sendable {
  public var actionID: ActionID
  public var responseGroupID: ResponseGroupID?
  public var taskID: String?
  public var state: ActionCanonicalState
  public var label: String
  public var target: String?
  public var revision: Int
  public var cancellable: Bool
  public var freshnessMs: Int?
  public var commandOverlay: ActionCommandOverlay
  public var progressHint: ActionProgressHint?

  public init(
    actionID: ActionID,
    responseGroupID: ResponseGroupID? = nil,
    taskID: String? = nil,
    state: ActionCanonicalState = .proposed,
    label: String = "",
    target: String? = nil,
    revision: Int = 0,
    cancellable: Bool = false,
    freshnessMs: Int? = nil,
    commandOverlay: ActionCommandOverlay = .none,
    progressHint: ActionProgressHint? = nil
  ) {
    self.actionID = actionID
    self.responseGroupID = responseGroupID
    self.taskID = taskID
    self.state = state
    self.label = label
    self.target = target
    self.revision = revision
    self.cancellable = cancellable
    self.freshnessMs = freshnessMs
    self.commandOverlay = commandOverlay
    self.progressHint = progressHint
  }
}

public struct ConfirmationViewState: Equatable, Sendable {
  public var confirmationID: ConfirmationID
  public var responseGroupID: ResponseGroupID?
  public var actionID: ActionID?
  public var summary: String
  public var target: String?
  public var risk: String
  public var options: [String]
  public var expiresAtMs: Int
  public var revision: Int

  public init(
    confirmationID: ConfirmationID,
    responseGroupID: ResponseGroupID? = nil,
    actionID: ActionID? = nil,
    summary: String = "",
    target: String? = nil,
    risk: String = "",
    options: [String] = [],
    expiresAtMs: Int = 0,
    revision: Int = 0
  ) {
    self.confirmationID = confirmationID
    self.responseGroupID = responseGroupID
    self.actionID = actionID
    self.summary = summary
    self.target = target
    self.risk = risk
    self.options = options
    self.expiresAtMs = expiresAtMs
    self.revision = revision
  }
}

// MARK: - Capabilities

/// Which input affordances the surface offers, derived from the daemon's answer.
public enum InputMode: String, Equatable, Sendable {
  case textOnly = "text_only"
  case textAndVoice = "text_and_voice"
}

/// Card 1's `RuntimeCapabilities` values, plus the derived input mode.
public struct InherentCapabilities: Equatable, Sendable {
  public var textInput: Bool
  public var imageInput: Bool
  public var voiceInput: Bool
  public var responseInterrupt: Bool
  public var actionCancel: Bool
  public var confirmationActions: Bool
  public var naturalBargeIn: Bool
  public var aecProfile: String

  public init(
    textInput: Bool = true,
    imageInput: Bool = false,
    voiceInput: Bool = false,
    responseInterrupt: Bool = false,
    actionCancel: Bool = false,
    confirmationActions: Bool = false,
    naturalBargeIn: Bool = false,
    aecProfile: String = ""
  ) {
    self.textInput = textInput
    self.imageInput = imageInput
    self.voiceInput = voiceInput
    self.responseInterrupt = responseInterrupt
    self.actionCancel = actionCancel
    self.confirmationActions = confirmationActions
    self.naturalBargeIn = naturalBargeIn
    self.aecProfile = aecProfile
  }

  public init(_ runtime: RuntimeCapabilities) {
    self.init(
      textInput: runtime.textInput,
      imageInput: runtime.imageInput,
      voiceInput: runtime.voiceInput,
      responseInterrupt: runtime.responseInterrupt,
      actionCancel: runtime.actionCancel,
      confirmationActions: runtime.confirmationActions,
      naturalBargeIn: runtime.naturalBargeIn,
      aecProfile: runtime.aecProfile
    )
  }

  /// A capability downgrade selects the text-only UI state.
  public var inputMode: InputMode { voiceInput ? .textAndVoice : .textOnly }
}

// MARK: - Local presentation (D19)

public struct PanelFrame: Equatable, Sendable {
  public var x: Double
  public var y: Double
  public var width: Double
  public var height: Double

  public init(x: Double, y: Double, width: Double, height: Double) {
    self.x = x
    self.y = y
    self.width = width
    self.height = height
  }
}

/// The D19 list: none of this is ever server truth, and none of it is replaced
/// by a snapshot adoption.
public struct LocalPresentationState: Equatable, Sendable {
  public var isVisible: Bool
  public var selectedGroupID: ResponseGroupID?
  public var expandedActionIDs: Set<ActionID>
  public var draftText: String
  public var stagedAttachmentNames: [String]
  public var reduceMotion: Bool
  public var panelFrame: PanelFrame?
  public var scrollAnchor: String?

  public init(
    isVisible: Bool = true,
    selectedGroupID: ResponseGroupID? = nil,
    expandedActionIDs: Set<ActionID> = [],
    draftText: String = "",
    stagedAttachmentNames: [String] = [],
    reduceMotion: Bool = false,
    panelFrame: PanelFrame? = nil,
    scrollAnchor: String? = nil
  ) {
    self.isVisible = isVisible
    self.selectedGroupID = selectedGroupID
    self.expandedActionIDs = expandedActionIDs
    self.draftText = draftText
    self.stagedAttachmentNames = stagedAttachmentNames
    self.reduceMotion = reduceMotion
    self.panelFrame = panelFrame
    self.scrollAnchor = scrollAnchor
  }
}

// MARK: - The root state

/// The D3 root: orthogonal families and separate identities, no single phase label.
public struct InherentUXState: Equatable, Sendable {
  public var connection: ConnectionState
  public var synchronization: SynchronizationState
  public var input: InputPresentationState
  public var responseGroups: [ResponseGroupID: ResponseGroupState]
  /// Creation order.
  public var responseOrder: [ResponseGroupID]
  public var actions: [ActionID: ActionViewState]
  public var pendingConfirmation: ConfirmationViewState?
  public var capabilities: InherentCapabilities
  public var foregroundGroupID: ResponseGroupID?
  public var presentation: LocalPresentationState

  public init(
    connection: ConnectionState = .disconnected,
    synchronization: SynchronizationState = SynchronizationState(),
    input: InputPresentationState = InputPresentationState(),
    responseGroups: [ResponseGroupID: ResponseGroupState] = [:],
    responseOrder: [ResponseGroupID] = [],
    actions: [ActionID: ActionViewState] = [:],
    pendingConfirmation: ConfirmationViewState? = nil,
    capabilities: InherentCapabilities = InherentCapabilities(),
    foregroundGroupID: ResponseGroupID? = nil,
    presentation: LocalPresentationState = LocalPresentationState()
  ) {
    self.connection = connection
    self.synchronization = synchronization
    self.input = input
    self.responseGroups = responseGroups
    self.responseOrder = responseOrder
    self.actions = actions
    self.pendingConfirmation = pendingConfirmation
    self.capabilities = capabilities
    self.foregroundGroupID = foregroundGroupID
    self.presentation = presentation
  }
}

/// How one response group reads to the user (D18); derived, never stored.
public enum GroupPresentation: String, Equatable, Sendable {
  case idle
  case generating
  case speaking
  case waitingAction = "waiting_action"
  case textOnlySuccess = "text_only_success"
  case completed
  case failed
}

extension InherentUXState {
  /// D18: a group with a non-terminal action stays on the background shelf while
  /// another group is foreground.  Ordered by action id so views and tests agree.
  public var backgroundShelf: [ActionViewState] {
    actions.values
      .filter { !$0.state.isTerminal && $0.responseGroupID != foregroundGroupID }
      .sorted { $0.actionID.rawValue < $1.actionID.rawValue }
  }

  /// D18: the globally unique confirmation is pinned regardless of foreground.
  public var pinnedConfirmation: ConfirmationViewState? { pendingConfirmation }

  /// Actions linked to one group, in action-id order.
  public func actions(inGroup groupID: ResponseGroupID) -> [ActionViewState] {
    actions.values
      .filter { $0.responseGroupID == groupID }
      .sorted { $0.actionID.rawValue < $1.actionID.rawValue }
  }

  /// The presentation a group reads as, in this precedence:
  ///
  /// 1. `failed` — every response is terminal and none completed, one failed;
  /// 2. `speaking` — some playback badge is speaking or ducked;
  /// 3. `waitingAction` — a response waits on an action, or a linked action is
  ///    still running (so a completed commentary beside a running action stays
  ///    waiting-action);
  /// 4. `generating` — some response lifecycle is still open;
  /// 5. `textOnlySuccess` — everything terminal, something completed, and no
  ///    playback ever completed (a failed TTS beside a completed document);
  /// 6. `completed`;
  /// 7. `idle` — an empty group.
  public func groupPresentation(_ groupID: ResponseGroupID) -> GroupPresentation {
    guard let group = responseGroups[groupID], !group.responses.isEmpty else { return .idle }
    let responses = Array(group.responses.values)
    let linked = actions(inGroup: groupID)
    let allTerminal = responses.allSatisfy { $0.lifecycle.isTerminal }
    let anyCompleted = responses.contains { $0.lifecycle == .completed }

    if allTerminal, !anyCompleted, responses.contains(where: { $0.lifecycle == .failed }) {
      return .failed
    }
    let speaking = responses.contains { response in
      response.playbacks.values.contains {
        $0.presentation == .speaking || $0.presentation == .ducked
      }
    }
    if speaking { return .speaking }
    if responses.contains(where: { $0.lifecycle == .waitingAction })
      || linked.contains(where: { !$0.state.isTerminal }) {
      return .waitingAction
    }
    if !allTerminal { return .generating }
    guard anyCompleted else { return .failed }
    let anyPlaybackCompleted = responses.contains { response in
      response.playbacks.values.contains { $0.phase == .completed }
    }
    if !anyPlaybackCompleted,
      responses.contains(where: { $0.lifecycle == .completed && $0.channel != .speech }) {
      return .textOnlySuccess
    }
    return .completed
  }
}
