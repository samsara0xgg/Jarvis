import Foundation

/// The ADR-0014 D4 reducer: the one place `InherentUXState` changes.
///
/// It is a pure function of (state, event, injected time).  No socket, no
/// clock, no queue, no framework: everything it wants done comes back as an
/// `InherentEffect` value the runner performs elsewhere.  The rules it applies
/// are D17 (ordering and deduplication), D18 (foreground selection) and D19
/// (local presentation), and its input vocabulary is D10's message catalog
/// plus D8's verified snapshot.
public enum InherentReducer {
  /// Folds one event into the state and returns the effects it earned.
  ///
  /// `now` is the injected monotonic time (D4).  Both time-shaped effects,
  /// `scheduleLocalFade` and `scheduleAckFlush`, carry a `Duration` the runner
  /// schedules and answer with an event, so `now` is carried but never read:
  /// the reducer must never grow a wall clock later.
  public static func reduce(
    state: inout InherentUXState,
    event: InherentClientEvent,
    now: ContinuousClock.Instant
  ) -> [InherentEffect] {
    switch event {
    case .socketOpened(let epoch, let connectionID, let hello, let logEpoch, let bootID):
      guard isLive(epoch, state) else { return [] }
      return openSocket(
        &state, epoch: epoch, connectionID: connectionID, hello: hello,
        logEpoch: logEpoch, bootID: bootID
      )

    case .socketClosed(let epoch, _, _), .reconnecting(let epoch):
      guard isLive(epoch, state) else { return [] }
      // Content, draft and foreground all survive a reconnect (D18/D19).
      state.connection = .reconnecting
      return []

    case .socketFailed(let epoch, _):
      guard isLive(epoch, state) else { return [] }
      state.connection = .disconnected
      return []

    case .snapshotFailed(let epoch, _):
      guard isLive(epoch, state) else { return [] }
      // D8: the old visible state remains behind a reconnect/resync banner.
      state.connection = .reconnecting
      state.synchronization.needsSnapshotRepair = true
      return []

    case .snapshotAdopted(let epoch, let snapshot):
      guard isLive(epoch, state) else { return [] }
      return adopt(snapshot, into: &state)

    case .durable(let epoch, let envelope):
      guard isLive(epoch, state) else { return [] }
      return applyDurable(envelope, to: &state)

    case .ackDeadline(let epoch):
      guard isLive(epoch, state) else { return [] }
      return flushAck(&state.synchronization)

    case .ephemeral(let epoch, let envelope):
      guard isLive(epoch, state) else { return [] }
      return applyEphemeral(envelope, to: &state)

    case .local(let local):
      apply(local, to: &state)
      return []
    }
  }

  // MARK: - Rule 1: socket epoch

  /// D17 rule 1: a frame from an older socket epoch cannot alter the live
  /// connection.  An equal epoch is the live one; a newer epoch is a new
  /// connection announcing itself.
  private static func isLive(_ epoch: Int, _ state: InherentUXState) -> Bool {
    epoch >= state.synchronization.activeSocketEpoch
  }

  private static func openSocket(
    _ state: inout InherentUXState,
    epoch: Int,
    connectionID: String,
    hello: ServerHelloPayload,
    logEpoch: String,
    bootID: String
  ) -> [InherentEffect] {
    let previousLogEpoch = state.synchronization.logEpoch
    let previousBootID = state.synchronization.bootID

    state.synchronization.activeSocketEpoch = epoch
    state.synchronization.activeConnectionID = connectionID
    state.connection = .synchronizing
    state.capabilities = InherentCapabilities(hello.runtimeCapabilities)

    if let previousLogEpoch, previousLogEpoch != logEpoch {
      // D17 rule 2: no old cursor carries across a log epoch, and nothing may
      // be applied again until a snapshot re-establishes the baseline.
      state.synchronization.lastAppliedCursor = 0
      state.synchronization.snapshotThroughCursor = 0
      state.synchronization.recentMessageIDs = []
      state.synchronization.needsSnapshotRepair = true
    }
    if let previousBootID, previousBootID != bootID {
      // D8: old input and playback ephemeral state is cleared on boot change.
      // `silenced` is not cleared: it is a latched safety fact about a tail
      // that was heard to stop, and no later frame would re-assert it.
      clearEphemeralStore(&state)
    }
    state.synchronization.logEpoch = logEpoch
    state.synchronization.bootID = bootID
    return []
  }

  /// Everything the ephemeral lane owns, reset in one place (D10 baseline, D8 boot change).
  private static func clearEphemeralStore(_ state: inout InherentUXState) {
    state.input = InputPresentationState()
    state.synchronization.lastEphemeralSequence = 0
    for groupID in Array(state.responseGroups.keys) {
      for responseID in Array(state.responseGroups[groupID]!.responses.keys) {
        for generation in Array(
          state.responseGroups[groupID]!.responses[responseID]!.playbacks.keys
        ) {
          state.responseGroups[groupID]!.responses[responseID]!
            .playbacks[generation]!.progress = nil
        }
      }
    }
    for actionID in Array(state.actions.keys) {
      state.actions[actionID]!.progressHint = nil
    }
  }

  // MARK: - Rule 2: snapshot adoption

  private static func adopt(
    _ snapshot: VerifiedSnapshot, into state: inout InherentUXState
  ) -> [InherentEffect] {
    var groups: [ResponseGroupID: ResponseGroupState] = [:]
    var order: [ResponseGroupID] = []
    for group in snapshot.groups {
      let groupID = ResponseGroupID(group.responseGroupId)
      var groupState = ResponseGroupState(
        id: groupID,
        turnID: group.turnId.map(TurnID.init),
        question: group.question,
        createdAtMs: group.createdAtMs
      )
      for response in group.responses {
        let responseID = ResponseID(response.responseId)
        let segments = response.segments
          .sorted { $0.sequence < $1.sequence }
          .map {
            ResponseSegmentState(
              sequence: $0.sequence, phase: $0.phase, channel: $0.channel,
              text: $0.text, segmentHash: $0.segmentHash
            )
          }
        var playbacks: [PlaybackGenerationID: PlaybackState] = [:]
        for playback in response.playbacks {
          let generation = PlaybackGenerationID(playback.playbackGenerationId)
          playbacks[generation] = PlaybackState(
            generationID: generation, phase: playback.phase, revision: playback.revision,
            heardThroughSequence: playback.heardThroughSequence
          )
        }
        groupState.responses[responseID] = ResponseState(
          responseID: responseID,
          responseGroupID: groupID,
          lifecycle: response.lifecycle,
          lifecycleRevision: response.revision,
          phase: response.phase,
          channel: response.channel,
          panelStream: response.panelStream,
          segments: segments,
          // D17 rule 12: the dedup and sequence baselines become exactly what
          // the snapshot contains.
          expectedSequence: segments.count,
          gapBuffer: [:],
          playbacks: playbacks,
          activePlaybackGenerationID: response.playbacks.last
            .map { PlaybackGenerationID($0.playbackGenerationId) }
        )
        groupState.responseOrder.append(responseID)
      }
      groups[groupID] = groupState
      order.append(groupID)
    }

    state.responseGroups = groups
    state.responseOrder = order
    state.actions = [:]
    for action in snapshot.actions {
      state.actions[ActionID(action.actionId)] = viewState(of: action)
    }
    state.pendingConfirmation = snapshot.pendingConfirmation.map(viewState(of:))
    if let capabilities = snapshot.capabilities {
      state.capabilities = InherentCapabilities(capabilities)
    }
    for notice in snapshot.notices {
      attach(notice, to: &state)
    }

    state.synchronization.activeConnectionID = snapshot.connectionId
    state.synchronization.logEpoch = snapshot.logEpoch
    state.synchronization.bootID = snapshot.bootId
    state.synchronization.lastAppliedCursor = snapshot.throughCursor
    state.synchronization.snapshotThroughCursor = snapshot.throughCursor
    state.synchronization.recentMessageIDs = []
    state.synchronization.resyncRequested = false
    state.synchronization.needsSnapshotRepair = false
    // The adoption ACK covers everything through H, so no durable is pending.
    state.synchronization.unackedDurableCount = 0
    state.connection = .live

    // D18: a reconnect or resync never moves the foreground by itself; it only
    // falls back when the group it named is no longer in the snapshot.
    let keptForeground = state.foregroundGroupID.flatMap { groups[$0] != nil ? $0 : nil }
    state.foregroundGroupID = keptForeground ?? order.last
    // `state.presentation` is untouched: D19 local truth survives the swap.
    return [.sendAck(snapshotID: snapshot.snapshotId, throughCursor: snapshot.throughCursor)]
  }

  // MARK: - Rules 3 and 4: durable ordering

  private static func applyDurable(
    _ envelope: ServerEnvelope<ViewDeltaPayload>, to state: inout InherentUXState
  ) -> [InherentEffect] {
    let sync = state.synchronization
    // Rule 1/D17 rule 2: identity must match the connection we synchronized on.
    // A boot A delta after a boot B snapshot fails here.
    guard let connectionID = sync.activeConnectionID, connectionID == envelope.connectionId,
      let logEpoch = sync.logEpoch, logEpoch == envelope.logEpoch,
      let bootID = sync.bootID, bootID == envelope.bootId,
      let cursor = envelope.eventCursor
    else { return [] }

    // The snapshot filters every older delta (D8 step 4).
    guard cursor > sync.snapshotThroughCursor else { return [] }
    // Rule 4: a duplicate message id is idempotent at any cursor.
    guard !sync.recentMessageIDs.contains(envelope.messageId) else { return [] }
    // Rule 3: a cursor at or below the last applied one that is not a known
    // duplicate means the client lost the thread.
    guard cursor > sync.lastAppliedCursor else {
      return resync(.cursorRegression, &state.synchronization)
    }

    // One ordered mutation batch lands as a unit (D9): a batch applies to a
    // working copy, so no view ever sees half of it.
    var draft = state
    var effects: [InherentEffect] = []
    for mutation in envelope.payload.changes {
      effects += apply(mutation, to: &draft)
    }
    draft.synchronization.lastAppliedCursor = cursor
    draft.synchronization.recentMessageIDs.append(envelope.messageId)
    if draft.synchronization.recentMessageIDs.count > SynchronizationState.recentMessageIDLimit {
      draft.synchronization.recentMessageIDs.removeFirst(
        draft.synchronization.recentMessageIDs.count - SynchronizationState.recentMessageIDLimit
      )
    }
    state = draft
    return effects + ackEffects(&state.synchronization)
  }

  /// D11 rule 8: ACK at `ackBatchMessages`, otherwise arm the `ackBatchMilliseconds`
  /// deadline once for the batch this frame opened.  Every applied durable frame
  /// is therefore ACKed within one batch window of the batch's first frame — a
  /// burst that stops is flushed by the deadline, not left in the server's window.
  private static func ackEffects(_ sync: inout SynchronizationState) -> [InherentEffect] {
    sync.unackedDurableCount += 1
    if sync.unackedDurableCount >= SynchronizationState.ackBatchMessages {
      return flushAck(&sync)
    }
    guard sync.unackedDurableCount == 1 else { return [] }
    return [.scheduleAckFlush(after: .milliseconds(SynchronizationState.ackBatchMilliseconds))]
  }

  /// The cumulative durable ACK: no snapshot id, `through_cursor` = last applied.
  private static func flushAck(_ sync: inout SynchronizationState) -> [InherentEffect] {
    guard sync.unackedDurableCount > 0 else { return [] }
    sync.unackedDurableCount = 0
    return [.sendAck(snapshotID: nil, throughCursor: sync.lastAppliedCursor)]
  }

  /// D17: a resync is one request, and `resyncRequested` records that one is
  /// outstanding so a gap storm cannot become a request storm.
  private static func resync(
    _ reason: ResyncReason, _ sync: inout SynchronizationState, onlyIfNoneOutstanding: Bool = false
  ) -> [InherentEffect] {
    if onlyIfNoneOutstanding, sync.resyncRequested { return [] }
    sync.resyncRequested = true
    return [.requestResync(reason: reason)]
  }

  // MARK: - Durable mutations (D10)

  private static func apply(
    _ mutation: ViewMutation, to state: inout InherentUXState
  ) -> [InherentEffect] {
    switch mutation {
    case .responseOpened(let opened): return applyOpened(opened, to: &state)
    case .responseSegment(let segment): return applySegment(segment, to: &state)
    case .responseDelivery(let delivery): return applyDelivery(delivery, to: &state)
    case .responseLifecycle(let change): return applyLifecycle(change, to: &state)
    case .playbackState(let change): return applyPlaybackState(change, to: &state)
    case .actionUpsert(let upsert): return applyAction(upsert, to: &state)
    case .confirmationUpsert(let upsert): return applyConfirmation(upsert, to: &state)
    case .confirmationCleared(let cleared): return applyConfirmationCleared(cleared, to: &state)
    case .inputCommitted(let committed): return applyInputCommitted(committed, to: &state)
    case .surfaceNotice(let notice):
      attach(notice, to: &state)
      return []
    }
  }

  /// The group that owns a response, or nil when the client never saw it.
  private static func groupID(
    of responseID: ResponseID, in state: InherentUXState
  ) -> ResponseGroupID? {
    for (groupID, group) in state.responseGroups where group.responses[responseID] != nil {
      return groupID
    }
    return nil
  }

  private static func applyOpened(
    _ opened: ResponseOpened, to state: inout InherentUXState
  ) -> [InherentEffect] {
    // D21: the server just named the group this client's submission produced.
    // An id this client never sent is simply not ours — another window, an
    // older process — and is ignored rather than treated as a mismatch.
    if let requestID = opened.sourceClientRequestId {
      state.pendingInputs.removeValue(forKey: requestID)
    }
    let groupID = ResponseGroupID(opened.responseGroupId)
    let responseID = ResponseID(opened.responseId)
    if state.responseGroups[groupID] == nil {
      state.responseGroups[groupID] = ResponseGroupState(
        id: groupID,
        turnID: TurnID(opened.turnId),
        question: opened.question,
        createdAtMs: opened.createdAtMs
      )
      state.responseOrder.append(groupID)
      // D18: only a committed turn or an explicit selection moves the
      // foreground; a first group has nothing to displace.
      if state.foregroundGroupID == nil { state.foregroundGroupID = groupID }
    } else if state.responseGroups[groupID]!.question == nil {
      state.responseGroups[groupID]!.question = opened.question
    }
    // `response_id` is never reused (D3): a repeat is a duplicate, and a
    // duplicate never re-announces.
    guard state.responseGroups[groupID]!.responses[responseID] == nil else { return [] }

    state.responseGroups[groupID]!.responses[responseID] = ResponseState(
      responseID: responseID,
      responseGroupID: groupID,
      lifecycle: opened.lifecycle,
      lifecycleRevision: opened.revision,
      phase: opened.phase,
      channel: opened.channel
    )
    state.responseGroups[groupID]!.responseOrder.append(responseID)
    return [.announceAccessibilityMilestone("response_opened:\(opened.responseId)")]
  }

  // MARK: - Rules 6 to 10: panel segments

  private static func applySegment(
    _ segment: ResponseSegment, to state: inout InherentUXState
  ) -> [InherentEffect] {
    let responseID = ResponseID(segment.responseId)
    guard let groupID = groupID(of: responseID, in: state) else {
      // Rule 6: a segment for a response no snapshot ever showed.
      return resync(.unknownResponse, &state.synchronization)
    }
    var response = state.responseGroups[groupID]!.responses[responseID]!
    // Rule 6: a late segment cannot alter a terminal response.
    guard !response.panelStream.isTerminal, !response.lifecycle.isTerminal else { return [] }

    var effects: [InherentEffect] = []
    if segment.sequence == response.expectedSequence {
      // Rule 7: append once, then let the buffered tail follow in order.
      response.segments.append(rendered(segment))
      response.expectedSequence += 1
      while let buffered = response.gapBuffer.removeValue(forKey: response.expectedSequence) {
        response.segments.append(buffered)
        response.expectedSequence += 1
      }
    } else if segment.sequence < response.expectedSequence {
      // Rule 8: already rendered; the hash must agree or the prefix is a lie.
      let stored = response.segments[segment.sequence]
      if stored.segmentHash != segment.segmentHash {
        effects = resync(.hashConflict, &state.synchronization)
      }
    } else if response.gapBuffer[segment.sequence] == nil {
      if response.gapBuffer.count >= ResponseState.gapBufferLimit {
        // Rule 10: drop the newest, keep the complete prefix, require repair.
        state.synchronization.needsSnapshotRepair = true
        effects = resync(.gapOverflow, &state.synchronization, onlyIfNoneOutstanding: true)
      } else {
        // Rule 9: buffered, never rendered, and exactly one request per gap.
        response.gapBuffer[segment.sequence] = rendered(segment)
        effects = resync(.gap, &state.synchronization, onlyIfNoneOutstanding: true)
      }
    }
    state.responseGroups[groupID]!.responses[responseID] = response
    return effects
  }

  private static func rendered(_ segment: ResponseSegment) -> ResponseSegmentState {
    ResponseSegmentState(
      sequence: segment.sequence, phase: segment.phase, channel: segment.channel,
      text: segment.text, segmentHash: segment.segmentHash
    )
  }

  private static func applyDelivery(
    _ delivery: ResponseDelivery, to state: inout InherentUXState
  ) -> [InherentEffect] {
    let responseID = ResponseID(delivery.responseId)
    guard let groupID = groupID(of: responseID, in: state) else {
      return resync(.unknownResponse, &state.synchronization)
    }
    var response = state.responseGroups[groupID]!.responses[responseID]!
    let wasFadeReady = fadeReady(response)

    if delivery.panelStream.isTerminal, !response.gapBuffer.isEmpty {
      // Rule 10: a terminal across an unresolved gap never fabricates
      // completion.  The prefix stands and the tail needs a snapshot.
      state.synchronization.needsSnapshotRepair = true
      return []
    }
    var effects: [InherentEffect] = []
    if response.panelStream.isTerminal {
      // Rule 11: one delivery terminal per response.
      if response.panelStream != delivery.panelStream {
        effects = resync(.terminalConflict, &state.synchronization)
      }
    } else {
      response.panelStream = delivery.panelStream
      if let reason = delivery.reason { response.terminalReason = reason }
    }
    state.responseGroups[groupID]!.responses[responseID] = response
    return effects + fade(groupID, wasFadeReady, fadeReady(response), state)
  }

  private static func applyLifecycle(
    _ change: ResponseLifecycleChange, to state: inout InherentUXState
  ) -> [InherentEffect] {
    let responseID = ResponseID(change.responseId)
    guard let groupID = groupID(of: responseID, in: state) else {
      return resync(.unknownResponse, &state.synchronization)
    }
    var response = state.responseGroups[groupID]!.responses[responseID]!
    let wasFadeReady = fadeReady(response)

    // Rule 5: below the stored revision is stale.
    guard change.revision >= response.lifecycleRevision else { return [] }
    let identical = change.lifecycle == response.lifecycle
      && change.terminalReason == response.terminalReason
    if identical { return [] }
    if change.revision == response.lifecycleRevision {
      // Rule 11: the same revision claiming a different terminal is a conflict;
      // a non-terminal disagreement at the same revision is simply not news.
      return change.lifecycle.isTerminal || response.lifecycle.isTerminal
        ? resync(.terminalConflict, &state.synchronization) : []
    }
    guard !response.lifecycle.isTerminal else {
      // Rule 11 / §15.3: a completed response stays completed, whatever
      // arrives later.  A second, different terminal is a conflict.
      return resync(.terminalConflict, &state.synchronization)
    }

    response.lifecycle = change.lifecycle
    response.lifecycleRevision = change.revision
    response.terminalReason = change.terminalReason
    state.responseGroups[groupID]!.responses[responseID] = response

    var effects: [InherentEffect] = []
    if change.lifecycle == .completed {
      effects.append(.announceAccessibilityMilestone("response_completed:\(change.responseId)"))
    }
    return effects + fade(groupID, wasFadeReady, fadeReady(response), state)
  }

  /// A foreground group's final response reaching completed and closed is the
  /// only thing this card fades.
  private static func fadeReady(_ response: ResponseState) -> Bool {
    response.phase == .final && response.lifecycle == .completed && response.panelStream == .closed
  }

  private static func fade(
    _ groupID: ResponseGroupID, _ was: Bool, _ isReady: Bool, _ state: InherentUXState
  ) -> [InherentEffect] {
    guard !was, isReady, state.foregroundGroupID == groupID else { return [] }
    return [.scheduleLocalFade(groupID: groupID, after: .seconds(5))]
  }

  // MARK: - Rule 6: playback generations

  private static func applyPlaybackState(
    _ change: PlaybackStateChange, to state: inout InherentUXState
  ) -> [InherentEffect] {
    let responseID = ResponseID(change.responseId)
    guard let groupID = groupID(of: responseID, in: state) else {
      return resync(.unknownResponse, &state.synchronization)
    }
    var response = state.responseGroups[groupID]!.responses[responseID]!
    let generation = PlaybackGenerationID(change.playbackGenerationId)

    if let active = response.activePlaybackGenerationID, active != generation {
      // Rule 6: the first checkpoint for an unseen generation takes the lease;
      // a generation the client already retired cannot alter the current one.
      guard response.playbacks[generation] == nil else { return [] }
      response.activePlaybackGenerationID = generation
    } else if response.activePlaybackGenerationID == nil {
      response.activePlaybackGenerationID = generation
    }

    var effects: [InherentEffect] = []
    if var playback = response.playbacks[generation] {
      guard change.revision >= playback.revision else { return [] }
      let identical = change.phase == playback.phase
        && change.heardThroughSequence == playback.heardThroughSequence
      if identical { return [] }
      if change.revision == playback.revision {
        // Rule 11: one terminal per (response, playback generation).
        return change.phase.isTerminal || playback.phase.isTerminal
          ? resync(.terminalConflict, &state.synchronization) : []
      }
      if playback.phase.isTerminal, change.phase != playback.phase {
        effects = resync(.terminalConflict, &state.synchronization)
        state.responseGroups[groupID]!.responses[responseID] = response
        return effects
      }
      playback.phase = change.phase
      playback.revision = change.revision
      playback.heardThroughSequence = change.heardThroughSequence
      // The durable interrupt records the canonical outcome; only a matching
      // ephemeral silence proves the device buffer is inaudible (D10).
      if change.phase == .interrupted { playback.silenced = false }
      response.playbacks[generation] = playback
    } else {
      response.playbacks[generation] = PlaybackState(
        generationID: generation,
        phase: change.phase,
        revision: change.revision,
        heardThroughSequence: change.heardThroughSequence
      )
    }
    state.responseGroups[groupID]!.responses[responseID] = response
    return effects
  }

  // MARK: - Actions and confirmations

  private static func viewState(of upsert: ActionUpsert) -> ActionViewState {
    ActionViewState(
      actionID: ActionID(upsert.actionId),
      responseGroupID: upsert.responseGroupId.map(ResponseGroupID.init),
      taskID: upsert.taskId,
      state: upsert.state,
      label: upsert.label,
      target: upsert.target,
      revision: upsert.revision,
      cancellable: upsert.cancellable,
      freshnessMs: upsert.freshnessMs
    )
  }

  private static func viewState(of upsert: ConfirmationUpsert) -> ConfirmationViewState {
    ConfirmationViewState(
      confirmationID: ConfirmationID(upsert.confirmationId),
      responseGroupID: upsert.responseGroupId.map(ResponseGroupID.init),
      actionID: upsert.actionId.map(ActionID.init),
      summary: upsert.summary,
      target: upsert.target,
      risk: upsert.risk,
      options: upsert.options,
      expiresAtMs: upsert.expiresAtMs,
      revision: upsert.revision
    )
  }

  private static func applyAction(
    _ upsert: ActionUpsert, to state: inout InherentUXState
  ) -> [InherentEffect] {
    let actionID = ActionID(upsert.actionId)
    guard let existing = state.actions[actionID] else {
      state.actions[actionID] = viewState(of: upsert)
      return []
    }
    // Rule 5.
    guard upsert.revision >= existing.revision else { return [] }
    var updated = viewState(of: upsert)
    // The command overlay and the progress hint are local and ephemeral truth;
    // a canonical upsert never carries them and never erases them.
    updated.commandOverlay = existing.commandOverlay
    updated.progressHint = existing.progressHint
    if upsert.revision == existing.revision {
      guard updated != existing else { return [] }
      return upsert.state.isTerminal || existing.state.isTerminal
        ? resync(.terminalConflict, &state.synchronization) : []
    }
    if existing.state.isTerminal, upsert.state != existing.state {
      return resync(.terminalConflict, &state.synchronization)
    }
    state.actions[actionID] = updated
    return []
  }

  private static func applyConfirmation(
    _ upsert: ConfirmationUpsert, to state: inout InherentUXState
  ) -> [InherentEffect] {
    let incoming = viewState(of: upsert)
    guard let pending = state.pendingConfirmation else {
      state.pendingConfirmation = incoming
      return []
    }
    // Rule 5 for the same id; D18 for a different one: new commentary never
    // overwrites an unresolved confirmation, so a genuine supersede either
    // cleared the old slot earlier in this same delta (D10) or outranks it.
    guard upsert.revision > pending.revision else { return [] }
    state.pendingConfirmation = incoming
    return []
  }

  private static func applyConfirmationCleared(
    _ cleared: ConfirmationCleared, to state: inout InherentUXState
  ) -> [InherentEffect] {
    guard let pending = state.pendingConfirmation,
      pending.confirmationID == ConfirmationID(cleared.confirmationId),
      cleared.revision >= pending.revision
    else { return [] }
    state.pendingConfirmation = nil
    return []
  }

  private static func applyInputCommitted(
    _ committed: InputCommitted, to state: inout InherentUXState
  ) -> [InherentEffect] {
    let groupID = ResponseGroupID(committed.responseGroupId)
    if state.responseGroups[groupID] == nil {
      state.responseGroups[groupID] = ResponseGroupState(
        id: groupID, turnID: TurnID(committed.turnId), question: committed.text
      )
      state.responseOrder.append(groupID)
    } else {
      if state.responseGroups[groupID]!.turnID == nil {
        state.responseGroups[groupID]!.turnID = TurnID(committed.turnId)
      }
      if state.responseGroups[groupID]!.question == nil {
        state.responseGroups[groupID]!.question = committed.text
      }
    }
    // D18: a newly committed user turn becomes the foreground group.
    state.foregroundGroupID = groupID
    state.input.phase = .committed
    state.input.utteranceID = UtteranceID(committed.utteranceId)
    state.input.partialText = nil
    return []
  }

  /// A notice belongs to the group its subject sits in; D3 gives notices no
  /// home of their own, so an unattributable one lands on the newest group.
  private static func attach(_ notice: SurfaceNotice, to state: inout InherentUXState) {
    var target: ResponseGroupID?
    if let subject = notice.subjectRef {
      target = groupID(of: ResponseID(subject), in: state)
        ?? (state.responseGroups[ResponseGroupID(subject)] != nil
          ? ResponseGroupID(subject) : nil)
    }
    guard let groupID = target ?? state.responseOrder.last else { return }
    if let index = state.responseGroups[groupID]!.notices
      .firstIndex(where: { $0.noticeId == notice.noticeId }) {
      state.responseGroups[groupID]!.notices[index] = notice
    } else {
      state.responseGroups[groupID]!.notices.append(notice)
    }
  }

  // MARK: - Rule 8: ephemeral ordering

  private static func applyEphemeral(
    _ envelope: ServerEnvelope<EphemeralUpdate>, to state: inout InherentUXState
  ) -> [InherentEffect] {
    // Rule 1 applies to the ephemeral lane too.
    guard let connectionID = state.synchronization.activeConnectionID,
      connectionID == envelope.connectionId,
      let sequence = envelope.ephemeralSequence,
      sequence > state.synchronization.lastEphemeralSequence
    else { return [] }

    if case .baseline(let baseline) = envelope.payload {
      // One atomic latest-value set: the complete ephemeral store is replaced,
      // so a key absent from the baseline is cleared (D10).
      clearEphemeralStore(&state)
      for item in baseline.items {
        apply(ephemeral: item.typedPayload, bootID: envelope.bootId, to: &state)
      }
      state.synchronization.lastEphemeralSequence = baseline.watermarkSequence
      return []
    }
    apply(ephemeral: envelope.payload, bootID: envelope.bootId, to: &state)
    state.synchronization.lastEphemeralSequence = sequence
    return []
  }

  private static func apply(
    ephemeral update: EphemeralUpdate, bootID: String, to state: inout InherentUXState
  ) {
    switch update {
    case .inputState(let input):
      let utteranceID = UtteranceID(input.utteranceId)
      guard state.input.utteranceID != utteranceID || input.revision >= state.input.revision
      else { return }
      state.input.utteranceID = utteranceID
      state.input.revision = input.revision
      state.input.phase = phase(of: input.state)
      if input.state != .listening, input.state != .transcribing { state.input.partialText = nil }

    case .inputPartial(let partial):
      let utteranceID = UtteranceID(partial.utteranceId)
      guard state.input.utteranceID != utteranceID || partial.revision >= state.input.revision
      else { return }
      state.input.utteranceID = utteranceID
      state.input.revision = partial.revision
      state.input.partialText = partial.text

    case .playbackProgress(let progress):
      let responseID = ResponseID(progress.responseId)
      guard let groupID = groupID(of: responseID, in: state) else { return }
      let generation = PlaybackGenerationID(progress.playbackGenerationId)
      guard var playback = state.responseGroups[groupID]!.responses[responseID]!
        .playbacks[generation]
      else { return }
      playback.progress = progress.state
      if let heard = progress.heardThroughSequence { playback.heardThroughSequence = heard }
      // Rule 6: only a matching-boot, matching-generation silence retires the
      // "stopping" badge.  A stale sequence never reaches here (rule 8).
      if progress.state == .silenced, bootID == state.synchronization.bootID {
        playback.silenced = true
      }
      state.responseGroups[groupID]!.responses[responseID]!.playbacks[generation] = playback

    case .actionProgressHint(let hint):
      // D10: a hint must point at a real ActionRun and never invents completion.
      guard state.actions[ActionID(hint.actionId)] != nil else { return }
      state.actions[ActionID(hint.actionId)]!.progressHint = hint

    case .connectionNotice:
      // D3 gives no state field a connection notice: the banner reads
      // `connection`.  Ignored rather than stored in a second truth.
      return

    case .clear(let clear):
      clearEphemeralKey(clear.key, to: &state)

    case .baseline:
      // A baseline nested inside a baseline item is not a thing (D10).
      return

    case .unknown:
      // Ephemeral truth is disposable: an unknown kind is ignored, not fatal.
      return
    }
  }

  /// The ephemeral key namespace: `input:<utterance>`, `playback:<response>:<generation>`
  /// and `action:<action>`.  D10 names the rule ("remove one exact evicted key")
  /// but not the spelling; this is the spelling the client accepts.
  private static func clearEphemeralKey(_ key: String, to state: inout InherentUXState) {
    let parts = key.split(separator: ":", omittingEmptySubsequences: false).map(String.init)
    switch (parts.first, parts.count) {
    case ("input", 2):
      guard state.input.utteranceID == UtteranceID(parts[1]) else { return }
      state.input = InputPresentationState()
    case ("playback", 3):
      let responseID = ResponseID(parts[1])
      guard let groupID = groupID(of: responseID, in: state) else { return }
      let generation = PlaybackGenerationID(parts[2])
      state.responseGroups[groupID]!.responses[responseID]!.playbacks[generation]?.progress = nil
    case ("action", 2):
      state.actions[ActionID(parts[1])]?.progressHint = nil
    default:
      return
    }
  }

  private static func phase(of value: InputStateValue) -> InputPhase {
    switch value {
    case .listening: return .listening
    case .transcribing: return .transcribing
    case .committed: return .committed
    case .empty: return .empty
    case .failed: return .failed
    }
  }

  // MARK: - Rule 10: local presentation (D19)

  /// Nothing here leaves the client and nothing here is server truth: hiding
  /// the window flips one flag and cancels nothing.
  private static func apply(
    _ event: LocalPresentationEvent, to state: inout InherentUXState
  ) {
    // D21: the only local event that is not presentation — a pending input is
    // this client's own bookkeeping about a submission the server has not yet
    // answered with a group.
    guard case .inputSubmitted(let pending) = event else {
      apply(event, to: &state.presentation)
      return
    }
    state.pendingInputs[pending.requestID] = pending
    while state.pendingInputs.count > pendingInputLimit {
      guard
        let oldest = state.pendingInputs.values.min(by: {
          ($0.submittedAtMs, $0.requestID) < ($1.submittedAtMs, $1.requestID)
        })
      else { break }
      state.pendingInputs.removeValue(forKey: oldest.requestID)
    }
  }

  private static func apply(
    _ event: LocalPresentationEvent, to presentation: inout LocalPresentationState
  ) {
    switch event {
    case .setVisible(let visible): presentation.isVisible = visible
    case .selectGroup(let groupID): presentation.selectedGroupID = groupID
    case .toggleActionExpanded(let actionID):
      if presentation.expandedActionIDs.contains(actionID) {
        presentation.expandedActionIDs.remove(actionID)
      } else {
        presentation.expandedActionIDs.insert(actionID)
      }
    case .setDraft(let text): presentation.draftText = text
    case .setReduceMotion(let reduce): presentation.reduceMotion = reduce
    case .setPanelFrame(let frame): presentation.panelFrame = frame
    case .setScrollAnchor(let anchor): presentation.scrollAnchor = anchor
    case .inputSubmitted: break  // handled by the InherentUXState overload
    }
  }
}
