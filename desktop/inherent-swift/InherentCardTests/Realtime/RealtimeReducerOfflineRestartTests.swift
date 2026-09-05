import XCTest
@testable import InherentRealtime

/// ADR-0014 §15.3 "Offline/restart", the bullets this card owns.  The failed
/// submit bullet belongs to the input card.
final class RealtimeReducerOfflineRestartTests: XCTestCase {
  /// disconnect preserves last content and draft
  func test_disconnectPreservesLastContentAndDraft() throws {
    var run = Reducing()
    try run.goLive()
    try run.durable(
      cursor: 1, [Fx.opened("R-1", group: "G-1"), Fx.segment("R-1", 0, text: "the answer")]
    )
    run.apply(.local(.setDraft("half a follow-up")))
    let live = run.state

    XCTAssertEqual(run.apply(.socketClosed(socketEpoch: 1, code: 1006, reason: "abnormal")), [])
    XCTAssertEqual(run.state.connection, .reconnecting)
    XCTAssertEqual(run.texts("R-1", in: "G-1"), ["the answer"])
    XCTAssertEqual(run.state.presentation.draftText, "half a follow-up")
    XCTAssertEqual(run.state.responseGroups, live.responseGroups)
    XCTAssertEqual(run.state.foregroundGroupID, live.foregroundGroupID)
  }

  /// snapshot cursor filters all older deltas
  func test_snapshotCursorFiltersAllOlderDeltas() throws {
    var run = Reducing()
    try run.goLive(
      through: 10,
      groups: [Fx.snapshotGroup("G-1", responses: [Fx.snapshotResponse("R-1")])]
    )
    let adopted = run.state
    XCTAssertEqual(run.state.synchronization.lastAppliedCursor, 10)

    for cursor in [1, 5, 10] {
      XCTAssertEqual(
        try run.durable(cursor: cursor, [Fx.segment("R-1", 0, text: "stale")]), [],
        "cursor \(cursor) is at or below the snapshot high water"
      )
    }
    XCTAssertEqual(run.state, adopted)

    try run.durable(cursor: 11, [Fx.segment("R-1", 0, text: "fresh")])
    XCTAssertEqual(run.texts("R-1", in: "G-1"), ["fresh"])
  }

  /// restart never emits a speech effect
  func test_restartEmitsOnlySendAckAndNeverASpeechEffect() throws {
    var run = Reducing()
    try run.goLive()
    try run.durable(cursor: 1, [Fx.opened("R-1", group: "G-1", channel: "speech")])

    // A daemon restart: the socket drops, the boot id changes, and the client
    // re-adopts a snapshot that already contains the spoken response.
    var restart: [InherentEffect] = []
    restart += run.apply(.socketClosed(socketEpoch: 1, code: 1001, reason: "going away"))
    restart += run.apply(try Fx.socketOpened(epoch: 2, connection: "C-2", boot: "B-2"))
    restart += run.apply(
      .snapshotAdopted(
        socketEpoch: 2,
        try Fx.snapshot(
          through: 40,
          groups: [
            Fx.snapshotGroup(
              "G-1",
              responses: [
                Fx.snapshotResponse(
                  "R-1", channel: "speech", lifecycle: "completed", panel: "closed",
                  segments: [Fx.segment("R-1", 0, text: "spoken once")],
                  playbacks: [Fx.snapshotPlayback("P-1", phase: "completed")]
                )
              ]
            )
          ],
          boot: "B-2", connection: "C-2"
        )
      )
    )

    XCTAssertEqual(restart, [.sendAck(snapshotID: "SNAP-1", throughCursor: 40)])
    XCTAssertEqual(run.state.connection, .live)
    XCTAssertEqual(run.texts("R-1", in: "G-1"), ["spoken once"])

    // The vocabulary itself has no speech case: this switch is exhaustive.
    for effect in restart {
      switch effect {
      case .sendAck, .requestResync, .scheduleLocalFade, .scheduleAckFlush,
        .announceAccessibilityMilestone:
        continue
      }
    }
  }

  /// capability downgrade selects the correct text/voice UI
  func test_capabilityDowngradeSelectsTheTextOnlyInputMode() throws {
    var run = Reducing()
    try run.goLive(voiceInput: true)
    XCTAssertEqual(run.state.capabilities.inputMode, .textAndVoice)

    run.apply(try Fx.socketOpened(epoch: 2, connection: "C-2", voiceInput: false))
    XCTAssertEqual(run.state.capabilities.inputMode, .textOnly)
    XCTAssertFalse(run.state.capabilities.voiceInput)
    XCTAssertTrue(run.state.capabilities.textInput)
  }
}
