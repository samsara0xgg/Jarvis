import XCTest
@testable import InherentRealtime

/// ADR-0014 §15.3 "State combinations": the orthogonal families of D3 must
/// coexist, and no family may overwrite another's truth.
final class RealtimeReducerStateCombinationTests: XCTestCase {
  private let groupOne = ResponseGroupID("G-1")

  /// Opens one speech response with an active playback generation.
  private func speaking() throws -> Reducing {
    var run = Reducing()
    try run.goLive()
    try run.durable(
      cursor: 1,
      [
        Fx.opened("R-1", group: "G-1", channel: "speech"),
        Fx.playback("R-1", "P-1", "speaking", revision: 1),
      ]
    )
    return run
  }

  private func playback(_ run: Reducing, _ generation: String = "P-1") -> PlaybackState? {
    run.response("R-1", in: "G-1")?.playbacks[PlaybackGenerationID(generation)]
  }

  /// listening and speaking/ducked coexist
  func test_listeningAndSpeakingOrDuckedCoexist() throws {
    var run = try speaking()
    try run.ephemeral(sequence: 1, Fx.inputState("U-1", revision: 1, state: "listening"))

    XCTAssertEqual(run.state.input.phase, .listening)
    XCTAssertEqual(playback(run)?.presentation, .speaking)
    XCTAssertEqual(run.state.groupPresentation(groupOne), .speaking)

    try run.durable(cursor: 2, [Fx.playback("R-1", "P-1", "ducked", revision: 2)])
    XCTAssertEqual(run.state.input.phase, .listening, "ducking speech does not stop listening")
    XCTAssertEqual(playback(run)?.presentation, .ducked)
    XCTAssertEqual(run.state.groupPresentation(groupOne), .speaking)
  }

  /// speech interruption leaves linked action running
  func test_speechInterruptionLeavesTheLinkedActionRunning() throws {
    var run = try speaking()
    try run.durable(cursor: 2, [Fx.action("A-1", group: "G-1", state: "running", revision: 1)])

    try run.durable(cursor: 3, [Fx.playback("R-1", "P-1", "interrupted", revision: 2)])
    XCTAssertEqual(playback(run)?.presentation, .stopping)
    XCTAssertEqual(run.state.actions[ActionID("A-1")]?.state, .running)
    XCTAssertEqual(run.state.groupPresentation(groupOne), .waitingAction)
  }

  /// response completed stays completed when playback later interrupts
  func test_completedResponseStaysCompletedAfterPlaybackInterrupt() throws {
    var run = try speaking()
    try run.durable(cursor: 2, [Fx.lifecycle("R-1", "completed", revision: 2)])

    try run.durable(cursor: 3, [Fx.playback("R-1", "P-1", "interrupted", revision: 2)])
    XCTAssertEqual(run.response("R-1", in: "G-1")?.lifecycle, .completed)
    XCTAssertEqual(playback(run)?.presentation, .stopping)
  }

  /// playback interrupted remains "stopping" until matching-boot/generation silence
  func test_interruptedPlaybackStaysStoppingUntilMatchingSilence() throws {
    var run = try speaking()
    try run.durable(cursor: 2, [Fx.playback("R-1", "P-1", "interrupted", revision: 2)])
    XCTAssertEqual(playback(run)?.presentation, .stopping)

    try run.ephemeral(sequence: 1, Fx.playbackProgress("R-1", "P-1", "draining"))
    XCTAssertEqual(playback(run)?.presentation, .stopping, "draining is not proof of silence")

    try run.ephemeral(sequence: 2, Fx.playbackProgress("R-1", "P-1", "silenced"))
    XCTAssertEqual(playback(run)?.presentation, .stopped)
  }

  /// wrong-generation silence cannot change the badge
  func test_wrongGenerationSilenceCannotChangeTheStoppingBadge() throws {
    var run = try speaking()
    try run.durable(cursor: 2, [Fx.playback("R-1", "P-1", "interrupted", revision: 2)])

    try run.ephemeral(sequence: 1, Fx.playbackProgress("R-1", "P-2", "silenced"))
    XCTAssertEqual(playback(run)?.presentation, .stopping)
    XCTAssertNil(playback(run, "P-2"), "an unknown generation creates no lease")
  }

  /// stale silence cannot change the badge
  func test_staleSequenceSilenceCannotChangeTheStoppingBadge() throws {
    var run = try speaking()
    try run.durable(cursor: 2, [Fx.playback("R-1", "P-1", "interrupted", revision: 2)])
    try run.ephemeral(sequence: 7, Fx.inputState("U-1", revision: 1, state: "listening"))

    try run.ephemeral(sequence: 3, Fx.playbackProgress("R-1", "P-1", "silenced"))
    XCTAssertEqual(playback(run)?.presentation, .stopping)
    XCTAssertEqual(run.state.synchronization.lastEphemeralSequence, 7)
  }

  /// a silence from another boot cannot change the badge
  func test_wrongBootSilenceCannotChangeTheStoppingBadge() throws {
    var run = try speaking()
    try run.durable(cursor: 2, [Fx.playback("R-1", "P-1", "interrupted", revision: 2)])

    try run.ephemeral(sequence: 1, Fx.playbackProgress("R-1", "P-1", "silenced"), boot: "B-9")
    XCTAssertEqual(playback(run)?.presentation, .stopping)
  }

  /// TTS failure plus document completion is a text-only success
  func test_ttsFailurePlusDocumentCompletionIsTextOnlySuccess() throws {
    var run = Reducing()
    try run.goLive()
    try run.durable(
      cursor: 1,
      [
        Fx.opened("R-speech", group: "G-1", channel: "speech"),
        Fx.opened("R-doc", group: "G-1", channel: "document"),
        Fx.playback("R-speech", "P-1", "failed", revision: 1),
        Fx.lifecycle("R-speech", "failed", revision: 2, reason: "tts_unavailable"),
        Fx.segment("R-doc", 0, text: "the answer"),
        Fx.lifecycle("R-doc", "completed", revision: 2),
        Fx.delivery("R-doc", "closed"),
      ]
    )

    XCTAssertEqual(run.state.groupPresentation(groupOne), .textOnlySuccess)
    XCTAssertEqual(run.texts("R-doc", in: "G-1"), ["the answer"])
    XCTAssertEqual(run.response("R-speech", in: "G-1")?.terminalReason, "tts_unavailable")
  }

  /// commentary completion plus action running remains waiting-action
  func test_commentaryCompletionPlusActionRunningStaysWaitingAction() throws {
    var run = Reducing()
    try run.goLive()
    try run.durable(
      cursor: 1,
      [
        Fx.opened("R-1", group: "G-1", phase: "commentary"),
        Fx.segment("R-1", 0, text: "on it", phase: "commentary"),
        Fx.lifecycle("R-1", "completed", revision: 2),
        Fx.delivery("R-1", "closed"),
        Fx.action("A-1", group: "G-1", state: "running", revision: 1),
      ]
    )

    XCTAssertEqual(run.response("R-1", in: "G-1")?.lifecycle, .completed)
    XCTAssertEqual(run.state.groupPresentation(groupOne), .waitingAction)
  }

  /// action result creates/updates the final sibling without overwriting commentary
  func test_actionResultCreatesFinalSiblingWithoutOverwritingCommentary() throws {
    var run = Reducing()
    try run.goLive()
    try run.durable(
      cursor: 1,
      [
        Fx.inputCommitted(utterance: "U-1", turn: "T-1", group: "G-1", text: "lock the door"),
        Fx.opened("R-1", group: "G-1", phase: "commentary"),
        Fx.segment("R-1", 0, text: "on it", phase: "commentary"),
        Fx.lifecycle("R-1", "completed", revision: 2),
        Fx.action("A-1", group: "G-1", state: "running", revision: 1),
      ]
    )
    // A new turn takes the foreground while the old action is still running.
    try run.durable(
      cursor: 2,
      [Fx.inputCommitted(utterance: "U-2", turn: "T-2", group: "G-2", text: "what time is it")]
    )

    try run.durable(
      cursor: 3,
      [
        Fx.action("A-1", group: "G-1", state: "result_observed", revision: 2),
        Fx.opened("R-2", group: "G-1"),
        Fx.segment("R-2", 0, text: "the door is locked"),
        Fx.lifecycle("R-2", "completed", revision: 2),
      ]
    )

    XCTAssertEqual(run.texts("R-1", in: "G-1"), ["on it"], "commentary survives the result")
    XCTAssertEqual(run.texts("R-2", in: "G-1"), ["the door is locked"])
    XCTAssertEqual(
      run.state.responseGroups[groupOne]?.responseOrder, [ResponseID("R-1"), ResponseID("R-2")]
    )
    XCTAssertEqual(run.state.foregroundGroupID, ResponseGroupID("G-2"), "D18: foreground is unmoved")
  }

  /// old action remains in the background shelf during a new foreground turn
  func test_oldActionStaysInTheBackgroundShelfDuringANewForegroundTurn() throws {
    var run = Reducing()
    try run.goLive()
    try run.durable(
      cursor: 1,
      [
        Fx.inputCommitted(utterance: "U-1", turn: "T-1", group: "G-1", text: "lock the door"),
        Fx.action("A-1", group: "G-1", state: "running", revision: 1),
      ]
    )
    XCTAssertEqual(run.state.backgroundShelf, [], "its own group is foreground")

    try run.durable(
      cursor: 2,
      [Fx.inputCommitted(utterance: "U-2", turn: "T-2", group: "G-2", text: "what time is it")]
    )
    XCTAssertEqual(run.state.foregroundGroupID, ResponseGroupID("G-2"))
    XCTAssertEqual(run.state.backgroundShelf.map(\.actionID), [ActionID("A-1")])

    // A terminal action leaves the shelf.
    try run.durable(
      cursor: 3, [Fx.action("A-1", group: "G-1", state: "result_observed", revision: 2)]
    )
    XCTAssertEqual(run.state.backgroundShelf, [])
  }
}
