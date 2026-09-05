import XCTest
@testable import InherentRealtime

/// The D3 state families and the D18/D19 derivations over hand-built states.
/// No reducer is involved: these assert the shapes and the pure helpers only.
final class RealtimeStateTests: XCTestCase {
  /// Compiles only if `T` really conforms; the library builds under complete
  /// strict concurrency, so this is the conformance half of that guarantee.
  private func requireSendable<T: Sendable & Equatable>(_ value: T) -> T { value }

  private let groupOne = ResponseGroupID("G-1")
  private let groupTwo = ResponseGroupID("G-2")

  private func response(
    _ id: String,
    group: ResponseGroupID,
    lifecycle: ResponseLifecycle,
    channel: ResponseChannel = .document,
    playbacks: [PlaybackState] = []
  ) -> ResponseState {
    var state = ResponseState(
      responseID: ResponseID(id), responseGroupID: group, lifecycle: lifecycle,
      channel: channel
    )
    for playback in playbacks {
      state.playbacks[playback.generationID] = playback
    }
    state.activePlaybackGenerationID = playbacks.last?.generationID
    return state
  }

  private func state(
    group: ResponseGroupID,
    responses: [ResponseState],
    actions: [ActionViewState] = [],
    foreground: ResponseGroupID? = nil
  ) -> InherentUXState {
    var groupState = ResponseGroupState(id: group)
    for response in responses {
      groupState.responses[response.responseID] = response
      groupState.responseOrder.append(response.responseID)
    }
    var ux = InherentUXState()
    ux.responseGroups[group] = groupState
    ux.responseOrder = [group]
    for action in actions {
      ux.actions[action.actionID] = action
    }
    ux.foregroundGroupID = foreground ?? group
    return ux
  }

  // MARK: - Defaults

  func test_defaultStateIsEmptyAndValueEqual() {
    let fresh = requireSendable(InherentUXState())
    XCTAssertEqual(fresh, InherentUXState())
    XCTAssertEqual(fresh.connection, .disconnected)
    XCTAssertEqual(fresh.input.phase, .idle)
    XCTAssertNil(fresh.input.utteranceID)
    XCTAssertTrue(fresh.responseGroups.isEmpty)
    XCTAssertTrue(fresh.responseOrder.isEmpty)
    XCTAssertTrue(fresh.actions.isEmpty)
    XCTAssertNil(fresh.pendingConfirmation)
    XCTAssertNil(fresh.foregroundGroupID)
    XCTAssertEqual(fresh.capabilities.inputMode, .textOnly)

    let sync = requireSendable(fresh.synchronization)
    XCTAssertEqual(sync.activeSocketEpoch, 0)
    XCTAssertEqual(sync.lastAppliedCursor, 0)
    XCTAssertEqual(sync.snapshotThroughCursor, 0)
    XCTAssertEqual(sync.lastEphemeralSequence, 0)
    XCTAssertTrue(sync.recentMessageIDs.isEmpty)
    XCTAssertNil(sync.logEpoch)
    XCTAssertNil(sync.bootID)
    XCTAssertFalse(sync.resyncRequested)
    XCTAssertFalse(sync.needsSnapshotRepair)
    XCTAssertEqual(SynchronizationState.recentMessageIDLimit, 256)
    XCTAssertEqual(ResponseState.gapBufferLimit, 32)
  }

  func test_localPresentationDefaults() {
    let presentation = requireSendable(LocalPresentationState())
    XCTAssertTrue(presentation.isVisible)
    XCTAssertNil(presentation.selectedGroupID)
    XCTAssertTrue(presentation.expandedActionIDs.isEmpty)
    XCTAssertEqual(presentation.draftText, "")
    XCTAssertTrue(presentation.stagedAttachmentNames.isEmpty)
    XCTAssertFalse(presentation.reduceMotion)
    XCTAssertNil(presentation.panelFrame)
    XCTAssertNil(presentation.scrollAnchor)
    XCTAssertEqual(InherentUXState().presentation, presentation)
  }

  func test_capabilitiesCarryTheRuntimeAnswerAndDeriveInputMode() {
    let runtime = RuntimeCapabilities(
      textInput: true, imageInput: true, voiceInput: true, responseInterrupt: true,
      actionCancel: true, confirmationActions: true, naturalBargeIn: false,
      aecProfile: "mac_builtin"
    )
    let capabilities = InherentCapabilities(runtime)
    XCTAssertEqual(capabilities.inputMode, .textAndVoice)
    XCTAssertEqual(capabilities.aecProfile, "mac_builtin")
    XCTAssertTrue(capabilities.imageInput)

    var downgraded = capabilities
    downgraded.voiceInput = false
    XCTAssertEqual(downgraded.inputMode, .textOnly)
  }

  // MARK: - Playback presentation

  func test_playbackPresentationMapsInterruptedBySilence() {
    let generation = PlaybackGenerationID("P-1")
    var playback = PlaybackState(generationID: generation, phase: .interrupted)
    XCTAssertEqual(playback.presentation, .stopping, "the device tail may still be audible")

    playback.silenced = true
    XCTAssertEqual(playback.presentation, .stopped)

    // Silence only decides the interrupted badge; it never rewrites another phase.
    var speaking = PlaybackState(generationID: generation, phase: .speaking)
    speaking.silenced = true
    XCTAssertEqual(speaking.presentation, .speaking)

    XCTAssertEqual(PlaybackState(generationID: generation).presentation, .idle)
    XCTAssertEqual(
      PlaybackState(generationID: generation, phase: .buffering).presentation, .buffering
    )
    XCTAssertEqual(PlaybackState(generationID: generation, phase: .ducked).presentation, .ducked)
    XCTAssertEqual(
      PlaybackState(generationID: generation, phase: .draining).presentation, .draining
    )
    XCTAssertEqual(
      PlaybackState(generationID: generation, phase: .completed).presentation, .completed
    )
    XCTAssertEqual(PlaybackState(generationID: generation, phase: .failed).presentation, .failed)
  }

  // MARK: - D18 derivations

  func test_backgroundShelfHoldsNonTerminalActionsOutsideTheForegroundGroup() {
    var ux = state(
      group: groupOne,
      responses: [response("RESP-1", group: groupOne, lifecycle: .completed)],
      actions: [
        ActionViewState(actionID: ActionID("A-1"), responseGroupID: groupOne, state: .running),
        ActionViewState(actionID: ActionID("A-2"), responseGroupID: groupTwo, state: .running),
        ActionViewState(
          actionID: ActionID("A-3"), responseGroupID: groupOne, state: .resultObserved
        ),
      ]
    )
    ux.responseGroups[groupTwo] = ResponseGroupState(id: groupTwo)
    ux.responseOrder.append(groupTwo)
    ux.foregroundGroupID = groupTwo

    XCTAssertEqual(ux.backgroundShelf.map(\.actionID), [ActionID("A-1")])

    // The old group becomes foreground: its running action leaves the shelf.
    ux.foregroundGroupID = groupOne
    XCTAssertEqual(ux.backgroundShelf.map(\.actionID), [ActionID("A-2")])
  }

  func test_pendingConfirmationIsPinnedRegardlessOfForeground() {
    var ux = state(
      group: groupOne,
      responses: [response("RESP-1", group: groupOne, lifecycle: .completed)]
    )
    XCTAssertNil(ux.pinnedConfirmation)

    let confirmation = ConfirmationViewState(
      confirmationID: ConfirmationID("C-1"), responseGroupID: groupOne, summary: "unlock"
    )
    ux.pendingConfirmation = confirmation
    ux.foregroundGroupID = groupTwo
    XCTAssertEqual(ux.pinnedConfirmation, confirmation)
  }

  func test_groupPresentationDerivesFromResponsesAndLinkedActions() {
    XCTAssertEqual(
      state(group: groupOne, responses: []).groupPresentation(groupOne), .idle
    )
    XCTAssertEqual(
      InherentUXState().groupPresentation(groupOne), .idle, "an unknown group is idle"
    )
    XCTAssertEqual(
      state(
        group: groupOne, responses: [response("RESP-1", group: groupOne, lifecycle: .generating)]
      ).groupPresentation(groupOne),
      .generating
    )
    XCTAssertEqual(
      state(
        group: groupOne,
        responses: [
          response(
            "RESP-1", group: groupOne, lifecycle: .generating, channel: .speech,
            playbacks: [PlaybackState(generationID: PlaybackGenerationID("P-1"), phase: .speaking)]
          )
        ]
      ).groupPresentation(groupOne),
      .speaking
    )
    // A completed commentary beside a running action stays waiting-action.
    XCTAssertEqual(
      state(
        group: groupOne,
        responses: [response("RESP-1", group: groupOne, lifecycle: .completed)],
        actions: [
          ActionViewState(actionID: ActionID("A-1"), responseGroupID: groupOne, state: .running)
        ]
      ).groupPresentation(groupOne),
      .waitingAction
    )
    // A failed TTS beside a completed document sibling is text-only success.
    XCTAssertEqual(
      state(
        group: groupOne,
        responses: [
          response("RESP-1", group: groupOne, lifecycle: .failed, channel: .speech),
          response("RESP-2", group: groupOne, lifecycle: .completed, channel: .document),
        ]
      ).groupPresentation(groupOne),
      .textOnlySuccess
    )
    // A completed response whose speech actually played reads as completed.
    XCTAssertEqual(
      state(
        group: groupOne,
        responses: [
          response(
            "RESP-1", group: groupOne, lifecycle: .completed, channel: .both,
            playbacks: [
              PlaybackState(generationID: PlaybackGenerationID("P-1"), phase: .completed)
            ]
          )
        ]
      ).groupPresentation(groupOne),
      .completed
    )
    // A completed response stays completed after its playback was interrupted.
    XCTAssertEqual(
      state(
        group: groupOne,
        responses: [
          response(
            "RESP-1", group: groupOne, lifecycle: .completed, channel: .both,
            playbacks: [
              PlaybackState(
                generationID: PlaybackGenerationID("P-1"), phase: .interrupted, silenced: true
              )
            ]
          )
        ]
      ).groupPresentation(groupOne),
      .textOnlySuccess
    )
    XCTAssertEqual(
      state(
        group: groupOne, responses: [response("RESP-1", group: groupOne, lifecycle: .failed)]
      ).groupPresentation(groupOne),
      .failed
    )
  }

  func test_groupStateCarriesOrderAndNotices() {
    var group = ResponseGroupState(id: groupOne, turnID: TurnID("T-1"), createdAtMs: 7)
    let first = response("RESP-1", group: groupOne, lifecycle: .completed)
    let second = response("RESP-2", group: groupOne, lifecycle: .generating)
    group.responses = [first.responseID: first, second.responseID: second]
    group.responseOrder = [first.responseID, second.responseID]
    group.notices = [
      SurfaceNotice(
        noticeId: "N-1", subjectRef: nil, severity: .info, code: "fallback", text: nil,
        requiredAction: nil
      )
    ]
    XCTAssertEqual(group.responseOrder, [ResponseID("RESP-1"), ResponseID("RESP-2")])
    XCTAssertEqual(group.notices.first?.severity, .info)
    XCTAssertEqual(group.turnID, TurnID("T-1"))
    XCTAssertEqual(requireSendable(group), group)
  }
}
