import XCTest
@testable import InherentRealtime

/// ADR-0014 D18 (who is in front) and D19 (what stays local).
final class RealtimeReducerSelectionTests: XCTestCase {
  /// D18: a newly committed user turn becomes the foreground group
  func test_newCommittedTurnBecomesTheForegroundGroup() throws {
    var run = Reducing()
    try run.goLive()
    try run.durable(
      cursor: 1,
      [Fx.inputCommitted(utterance: "U-1", turn: "T-1", group: "G-1", text: "first")]
    )
    XCTAssertEqual(run.state.foregroundGroupID, ResponseGroupID("G-1"))

    // A response opening inside an existing group never moves the foreground.
    try run.durable(
      cursor: 2,
      [
        Fx.inputCommitted(utterance: "U-2", turn: "T-2", group: "G-2", text: "second"),
        Fx.opened("R-1", group: "G-1"),
      ]
    )
    XCTAssertEqual(run.state.foregroundGroupID, ResponseGroupID("G-2"))
    XCTAssertEqual(run.state.input.phase, .committed)
    XCTAssertEqual(run.state.input.utteranceID, UtteranceID("U-2"))
  }

  /// D18: the pending confirmation is pinned even when its group is background
  func test_pendingConfirmationStaysPinnedWhileAnotherGroupIsForeground() throws {
    var run = Reducing()
    try run.goLive()
    try run.durable(
      cursor: 1,
      [
        Fx.inputCommitted(utterance: "U-1", turn: "T-1", group: "G-1", text: "unlock the door"),
        Fx.action("A-1", group: "G-1", state: "proposed", revision: 1),
        Fx.confirmation("C-1", group: "G-1", action: "A-1", revision: 1),
      ]
    )

    try run.durable(
      cursor: 2,
      [
        Fx.inputCommitted(utterance: "U-2", turn: "T-2", group: "G-2", text: "what time is it"),
        Fx.opened("R-2", group: "G-2"),
        Fx.segment("R-2", 0, text: "half past four"),
      ]
    )
    XCTAssertEqual(run.state.foregroundGroupID, ResponseGroupID("G-2"))
    XCTAssertEqual(run.state.pinnedConfirmation?.confirmationID, ConfirmationID("C-1"))
    XCTAssertEqual(run.state.pinnedConfirmation?.responseGroupID, ResponseGroupID("G-1"))

    // New commentary at a lower revision never overwrites an unresolved slot.
    try run.durable(cursor: 3, [Fx.confirmation("C-2", group: "G-2", revision: 1)])
    XCTAssertEqual(run.state.pendingConfirmation?.confirmationID, ConfirmationID("C-1"))

    // A supersede clears the old slot and upserts the new one in one delta.
    try run.durable(
      cursor: 4,
      [
        Fx.confirmationCleared("C-1", reason: "superseded", revision: 2),
        Fx.confirmation("C-2", group: "G-2", revision: 1),
      ]
    )
    XCTAssertEqual(run.state.pendingConfirmation?.confirmationID, ConfirmationID("C-2"))
  }

  /// D18: a network reconnect never changes foreground identity by itself
  func test_reconnectNeverChangesForegroundIdentity() throws {
    var run = Reducing()
    try run.goLive()
    try run.durable(
      cursor: 1,
      [
        Fx.inputCommitted(utterance: "U-1", turn: "T-1", group: "G-1", text: "first"),
        Fx.inputCommitted(utterance: "U-2", turn: "T-2", group: "G-2", text: "second"),
      ]
    )
    run.apply(.local(.selectGroup(ResponseGroupID("G-1"))))
    XCTAssertEqual(run.state.foregroundGroupID, ResponseGroupID("G-2"))

    run.apply(.socketClosed(socketEpoch: 1, code: 1006, reason: "abnormal"))
    run.apply(.reconnecting(socketEpoch: 1))
    run.apply(try Fx.socketOpened(epoch: 2, connection: "C-2"))
    XCTAssertEqual(run.state.foregroundGroupID, ResponseGroupID("G-2"))

    run.apply(
      .snapshotAdopted(
        socketEpoch: 2,
        try Fx.snapshot(
          through: 20,
          groups: [Fx.snapshotGroup("G-1"), Fx.snapshotGroup("G-2")],
          connection: "C-2"
        )
      )
    )
    XCTAssertEqual(run.state.foregroundGroupID, ResponseGroupID("G-2"), "adoption keeps it")
    XCTAssertEqual(
      run.state.presentation.selectedGroupID, ResponseGroupID("G-1"), "selection is local truth"
    )
  }

  /// D19: hiding the window changes only `presentation.isVisible`
  func test_setVisibleFalseChangesOnlyIsVisible() throws {
    var run = Reducing()
    try run.goLive()
    try run.durable(
      cursor: 1,
      [
        Fx.inputCommitted(utterance: "U-1", turn: "T-1", group: "G-1", text: "question"),
        Fx.opened("R-1", group: "G-1"),
        Fx.segment("R-1", 0, text: "answer"),
        Fx.action("A-1", group: "G-1", state: "running", revision: 1),
        Fx.confirmation("C-1", group: "G-1", revision: 1),
      ]
    )
    run.apply(.local(.setDraft("a follow-up")))
    run.apply(.local(.toggleActionExpanded(ActionID("A-1"))))
    run.apply(.local(.setScrollAnchor("R-1")))
    let before = run.state

    XCTAssertEqual(run.apply(.local(.setVisible(false))), [])
    let after = run.state

    XCTAssertFalse(after.presentation.isVisible)
    XCTAssertTrue(before.presentation.isVisible)
    XCTAssertEqual(after.connection, before.connection)
    XCTAssertEqual(after.synchronization, before.synchronization)
    XCTAssertEqual(after.input, before.input)
    XCTAssertEqual(after.responseGroups, before.responseGroups)
    XCTAssertEqual(after.responseOrder, before.responseOrder)
    XCTAssertEqual(after.actions, before.actions)
    XCTAssertEqual(after.pendingConfirmation, before.pendingConfirmation)
    XCTAssertEqual(after.capabilities, before.capabilities)
    XCTAssertEqual(after.foregroundGroupID, before.foregroundGroupID)
    XCTAssertEqual(after.presentation.selectedGroupID, before.presentation.selectedGroupID)
    XCTAssertEqual(after.presentation.expandedActionIDs, before.presentation.expandedActionIDs)
    XCTAssertEqual(after.presentation.draftText, before.presentation.draftText)
    XCTAssertEqual(
      after.presentation.stagedAttachmentNames, before.presentation.stagedAttachmentNames
    )
    XCTAssertEqual(after.presentation.reduceMotion, before.presentation.reduceMotion)
    XCTAssertEqual(after.presentation.panelFrame, before.presentation.panelFrame)
    XCTAssertEqual(after.presentation.scrollAnchor, before.presentation.scrollAnchor)

    // The whole state differs in exactly that one flag.
    var restored = after
    restored.presentation.isVisible = true
    XCTAssertEqual(restored, before)
  }
}
