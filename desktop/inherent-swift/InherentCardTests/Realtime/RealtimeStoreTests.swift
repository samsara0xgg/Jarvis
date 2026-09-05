import XCTest
@testable import InherentRealtime

/// ADR-0014 D4: the store is the sole mutation entry.  `state` is
/// `private(set)`, so "nothing else can write it" is a compile-time fact and
/// needs no test; what is worth testing is that applying an event mutates it
/// and routes the reducer's effects, in order, to the runner.
@MainActor
final class RealtimeStoreTests: XCTestCase {
  func test_applyMutatesStateAndRoutesEffectsInOrder() async throws {
    let runner = RecordingEffectRunner()
    let store = RealtimeStore(runner: runner)
    XCTAssertEqual(store.state.connection, .disconnected)

    await store.apply(try Fx.socketOpened())
    XCTAssertEqual(store.state.connection, .synchronizing)

    await store.apply(.snapshotAdopted(socketEpoch: 1, try Fx.snapshot(through: 7)))
    XCTAssertEqual(store.state.connection, .live)
    XCTAssertEqual(store.state.synchronization.lastAppliedCursor, 7)

    // A segment for a response no snapshot ever showed: D17 rule 6.
    await store.apply(
      .durable(socketEpoch: 1, try Fx.durable(cursor: 8, [Fx.segment("R-ghost", 0)]))
    )

    let recorded = await runner.recorded
    XCTAssertEqual(
      recorded,
      [
        .sendAck(snapshotID: "SNAP-1", throughCursor: 7),
        .requestResync(reason: .unknownResponse),
        // The frame was applied, so it is ACKed on the D11 rule 8 cadence even
        // though one of its mutations asked for a resync.
        .scheduleAckFlush(after: .milliseconds(SynchronizationState.ackBatchMilliseconds)),
      ]
    )
  }

  func test_presentationMirrorsTheReducedState() async throws {
    let store = RealtimeStore(runner: RecordingEffectRunner())
    XCTAssertEqual(store.presentation, store.state.presentation)

    await store.apply(.local(.setDraft("half a follow-up")))
    XCTAssertEqual(store.presentation.draftText, "half a follow-up")
    XCTAssertEqual(store.presentation, store.state.presentation)
  }

  /// D19: hiding the window flips one flag and touches nothing else.
  func test_setVisibleChangesOnlyIsVisible() async throws {
    let runner = RecordingEffectRunner()
    let store = RealtimeStore(runner: runner)
    await store.apply(try Fx.socketOpened())
    await store.apply(
      .snapshotAdopted(
        socketEpoch: 1,
        try Fx.snapshot(
          through: 3, groups: [Fx.snapshotGroup("G-1", responses: [Fx.snapshotResponse("R-1")])]
        )
      )
    )
    let live = store.state

    await store.apply(.local(.setVisible(false)))

    var expected = live
    expected.presentation.isVisible = false
    XCTAssertEqual(store.state, expected)
    let recorded = await runner.recorded
    XCTAssertEqual(recorded, [.sendAck(snapshotID: "SNAP-1", throughCursor: 3)], "a local event earns no effect")
  }
}
