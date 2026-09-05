import XCTest
@testable import InherentRealtime

/// ADR-0014 §15.3 "Identity/order": every bullet is one test, and the name says
/// which bullet it proves.
final class RealtimeReducerIdentityTests: XCTestCase {
  /// duplicate event/message gives identical state
  func test_duplicateDurableEnvelopeGivesIdenticalState() throws {
    var run = Reducing()
    try run.goLive()
    let envelope = try Fx.durable(
      cursor: 1, [Fx.opened("R-1", group: "G-1"), Fx.segment("R-1", 0, text: "hello")]
    )
    run.apply(.durable(socketEpoch: 1, envelope))
    let applied = run.state

    XCTAssertEqual(run.apply(.durable(socketEpoch: 1, envelope)), [])
    XCTAssertEqual(run.state, applied, "the same envelope twice is one application")

    // Rule 4: the same message id at a later cursor is still a duplicate.
    let replayed = try Fx.durable(
      cursor: 2, [Fx.segment("R-1", 1, text: "world")], messageID: "M-1"
    )
    XCTAssertEqual(run.apply(.durable(socketEpoch: 1, replayed)), [])
    XCTAssertEqual(run.state, applied)
  }

  /// segment order 0,2,1 renders only 0, then 0/1/2
  func test_segmentOrderZeroTwoOneRendersZeroThenZeroOneTwo() throws {
    var run = Reducing()
    try run.goLive()
    try run.durable(cursor: 1, [Fx.opened("R-1", group: "G-1")])

    try run.durable(cursor: 2, [Fx.segment("R-1", 0, text: "zero")])
    XCTAssertEqual(run.texts("R-1", in: "G-1"), ["zero"])

    let gap = try run.durable(cursor: 3, [Fx.segment("R-1", 2, text: "two")])
    XCTAssertEqual(run.texts("R-1", in: "G-1"), ["zero"], "a segment past the gap is not rendered")
    XCTAssertEqual(gap, [.requestResync(reason: .gap)])

    try run.durable(cursor: 4, [Fx.segment("R-1", 1, text: "one")])
    XCTAssertEqual(run.texts("R-1", in: "G-1"), ["zero", "one", "two"])
    XCTAssertEqual(run.response("R-1", in: "G-1")?.expectedSequence, 3)
    XCTAssertTrue(run.response("R-1", in: "G-1")!.gapBuffer.isEmpty)
  }

  /// gap overflow requests one resync and never displays corrupt suffix
  func test_gapOverflowRequestsOneResyncAndNeverDisplaysCorruptSuffix() throws {
    var run = Reducing()
    try run.goLive()
    try run.durable(cursor: 1, [Fx.opened("R-1", group: "G-1")])
    try run.durable(cursor: 2, [Fx.segment("R-1", 0, text: "zero")])

    // 32 buffered segments fill the gap buffer; the 33rd overflows it.
    for sequence in 2...34 {
      try run.durable(cursor: sequence + 10, [Fx.segment("R-1", sequence)])
    }

    let resyncs = run.effects.filter {
      if case .requestResync = $0 { return true }
      return false
    }
    XCTAssertEqual(resyncs, [.requestResync(reason: .gap)], "exactly one resync for the whole gap")
    XCTAssertEqual(run.texts("R-1", in: "G-1"), ["zero"], "only the complete prefix is rendered")
    XCTAssertEqual(run.response("R-1", in: "G-1")?.gapBuffer.count, 32)
    XCTAssertTrue(run.state.synchronization.needsSnapshotRepair)
  }

  /// terminal before a missing segment does not fabricate completion
  func test_terminalBeforeMissingSegmentDoesNotFabricateCompletion() throws {
    var run = Reducing()
    try run.goLive()
    try run.durable(
      cursor: 1, [Fx.opened("R-1", group: "G-1"), Fx.delivery("R-1", "open")]
    )
    try run.durable(cursor: 2, [Fx.segment("R-1", 0, text: "zero")])
    try run.durable(cursor: 3, [Fx.segment("R-1", 2, text: "two")])

    try run.durable(cursor: 4, [Fx.delivery("R-1", "closed")])
    XCTAssertEqual(run.response("R-1", in: "G-1")?.panelStream, .open, "the panel is not closed")
    XCTAssertEqual(run.texts("R-1", in: "G-1"), ["zero"])
    XCTAssertTrue(run.state.synchronization.needsSnapshotRepair)
  }

  /// a late panel segment cannot alter a terminal response
  func test_latePanelSegmentCannotAlterATerminalResponse() throws {
    var run = Reducing()
    try run.goLive()
    try run.durable(cursor: 1, [Fx.opened("R-1", group: "G-1")])
    try run.durable(cursor: 2, [Fx.segment("R-1", 0, text: "zero")])
    try run.durable(
      cursor: 3, [Fx.lifecycle("R-1", "completed", revision: 2), Fx.delivery("R-1", "closed")]
    )
    let terminal = run.state

    XCTAssertEqual(try run.durable(cursor: 4, [Fx.segment("R-1", 1, text: "late")]), [])
    XCTAssertEqual(run.texts("R-1", in: "G-1"), ["zero"])
    XCTAssertEqual(run.state.responseGroups, terminal.responseGroups)
  }

  /// a late panel segment cannot alter a new response
  func test_latePanelSegmentCannotAlterANewResponse() throws {
    var run = Reducing()
    try run.goLive()
    try run.durable(
      cursor: 1,
      [
        Fx.opened("R-1", group: "G-1", phase: "commentary"),
        Fx.segment("R-1", 0, text: "thinking", phase: "commentary"),
        Fx.lifecycle("R-1", "completed", revision: 2),
        Fx.delivery("R-1", "closed"),
      ]
    )
    try run.durable(cursor: 2, [Fx.opened("R-2", group: "G-1"), Fx.segment("R-2", 0, text: "answer")])
    let before = run.state

    // A segment addressed to the retired response touches neither response.
    XCTAssertEqual(try run.durable(cursor: 3, [Fx.segment("R-1", 1, text: "late")]), [])
    XCTAssertEqual(run.state.responseGroups, before.responseGroups)

    // A segment for a response id the client never saw asks for a snapshot.
    XCTAssertEqual(
      try run.durable(cursor: 4, [Fx.segment("R-9", 0, text: "ghost")]),
      [.requestResync(reason: .unknownResponse)]
    )
    XCTAssertEqual(run.state.responseGroups, before.responseGroups)
  }

  /// playback generation N cannot alter N+1
  func test_playbackGenerationNCannotAlterGenerationNPlusOne() throws {
    var run = Reducing()
    try run.goLive()
    try run.durable(cursor: 1, [Fx.opened("R-1", group: "G-1", channel: "speech")])
    try run.durable(cursor: 2, [Fx.playback("R-1", "P-1", "speaking", revision: 1)])
    try run.durable(cursor: 3, [Fx.playback("R-1", "P-2", "buffering", revision: 1)])
    XCTAssertEqual(
      run.response("R-1", in: "G-1")?.activePlaybackGenerationID, PlaybackGenerationID("P-2")
    )

    XCTAssertEqual(
      try run.durable(cursor: 4, [Fx.playback("R-1", "P-1", "failed", revision: 9)]), []
    )
    let response = run.response("R-1", in: "G-1")!
    XCTAssertEqual(response.playbacks[PlaybackGenerationID("P-1")]?.phase, .speaking)
    XCTAssertEqual(response.playbacks[PlaybackGenerationID("P-2")]?.phase, .buffering)
    XCTAssertEqual(response.activePlaybackGenerationID, PlaybackGenerationID("P-2"))
  }

  /// boot A delta after boot B snapshot is ignored
  func test_bootADeltaAfterBootBSnapshotIsIgnored() throws {
    var run = Reducing()
    try run.goLive(boot: "B-2")
    let adopted = run.state

    XCTAssertEqual(
      try run.durable(cursor: 1, [Fx.opened("R-1", group: "G-1")], boot: "B-1"), []
    )
    XCTAssertEqual(run.state, adopted, "a delta from the previous boot changes nothing")

    try run.durable(cursor: 1, [Fx.opened("R-1", group: "G-1")], boot: "B-2")
    XCTAssertNotNil(run.response("R-1", in: "G-1"), "the current boot still applies")
  }

  /// old socket epoch failure cannot alter the new connection
  func test_oldSocketEpochFailureCannotAlterTheNewConnection() throws {
    var run = Reducing()
    try run.goLive(epoch: 1)
    run.apply(try Fx.socketOpened(epoch: 2, connection: "C-2"))
    let reconnected = run.state

    XCTAssertEqual(run.apply(.socketFailed(socketEpoch: 1, reason: "dead")), [])
    XCTAssertEqual(run.state, reconnected)
    XCTAssertEqual(run.state.connection, .synchronizing)
    XCTAssertEqual(run.state.synchronization.activeConnectionID, "C-2")

    // The live epoch still owns the connection state.
    run.apply(.socketFailed(socketEpoch: 2, reason: "dead"))
    XCTAssertEqual(run.state.connection, .disconnected)
  }
}
