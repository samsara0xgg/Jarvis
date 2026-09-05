import XCTest
@testable import InherentRealtime

/// ADR-0014 D11 rules 6 and 8: the cumulative durable ACK and its cadence.
///
/// The cadence is what makes the server's rule-9 ack-stall close safe, so the
/// property these cases pin is stronger than "25 or 100 ms": while the socket
/// is live, no applied durable frame stays unACKed past one batch window,
/// whatever the arrival pattern — a burst that stops included.  The reducer is
/// pure, so the deadline arrives as `.ackDeadline`, exactly as the runner
/// delivers it after performing `.scheduleAckFlush`.
final class RealtimeReducerAckCadenceTests: XCTestCase {
  private let flush = InherentEffect.scheduleAckFlush(
    after: .milliseconds(SynchronizationState.ackBatchMilliseconds)
  )

  /// One `response.segment` at `cursor`; sequences stay contiguous from 0 so
  /// no case here trips the gap rule instead of the cadence it means to test.
  private func segment(_ run: inout Reducing, cursor: Int) throws -> [InherentEffect] {
    try run.durable(cursor: cursor, [Fx.segment("R-1", cursor - 1)])
  }

  private func live() throws -> Reducing {
    var run = Reducing()
    try run.goLive(
      through: 0, groups: [Fx.snapshotGroup("G-1", responses: [Fx.snapshotResponse("R-1")])]
    )
    return run
  }

  /// Rule 8, the count half: the 25th applied frame ACKs, and only it.
  func test_ackBatchesAt25DurableMessages() throws {
    var run = try live()
    XCTAssertEqual(SynchronizationState.ackBatchMessages, 25)

    var effects: [[InherentEffect]] = []
    for cursor in 1...SynchronizationState.ackBatchMessages {
      effects.append(try segment(&run, cursor: cursor))
    }

    // The first frame arms the deadline; nothing between it and the 25th acks.
    XCTAssertEqual(effects.first, [flush])
    XCTAssertEqual(effects.dropFirst().dropLast(), Array(repeating: [], count: 23)[...])
    XCTAssertEqual(effects.last, [.sendAck(snapshotID: nil, throughCursor: 25)])
    XCTAssertEqual(run.state.synchronization.unackedDurableCount, 0)

    // The batch after it arms a fresh deadline and counts from zero again.
    XCTAssertEqual(try segment(&run, cursor: 26), [flush])
    XCTAssertEqual(run.state.synchronization.unackedDurableCount, 1)
  }

  /// Rule 8, the time half: fewer than 25 frames still ACK at the deadline.
  func test_ackBatchesAtTheDeadlineWithFewerThan25Messages() throws {
    var run = try live()
    XCTAssertEqual(SynchronizationState.ackBatchMilliseconds, 100)

    XCTAssertEqual(try segment(&run, cursor: 1), [flush])
    XCTAssertEqual(try segment(&run, cursor: 2), [])
    XCTAssertEqual(try segment(&run, cursor: 3), [])

    XCTAssertEqual(run.apply(.ackDeadline(socketEpoch: 1)), [
      .sendAck(snapshotID: nil, throughCursor: 3)
    ])
    XCTAssertEqual(run.state.synchronization.unackedDurableCount, 0)
    // A deadline with an empty window says nothing: rule 7 sees no ACK at all.
    XCTAssertEqual(run.apply(.ackDeadline(socketEpoch: 1)), [])
  }

  /// The observable contract: a burst that stops mid-batch leaves nothing
  /// unACKed past the window it opened, so the server's rule-9 timer never
  /// closes a healthy client.
  func test_aBurstThatStopsLeavesNoAppliedFrameUnacked() throws {
    var run = try live()
    var armed = 0
    // 61 frames: two full count-batches, then eleven and silence.
    for cursor in 1...61 where try segment(&run, cursor: cursor) == [flush] {
      armed += 1
    }
    XCTAssertEqual(armed, 3, "each batch arms exactly one deadline")
    XCTAssertEqual(run.state.synchronization.unackedDurableCount, 11)

    XCTAssertEqual(run.apply(.ackDeadline(socketEpoch: 1)), [
      .sendAck(snapshotID: nil, throughCursor: 61)
    ])
    XCTAssertEqual(run.state.synchronization.unackedDurableCount, 0)
    XCTAssertEqual(run.state.synchronization.lastAppliedCursor, 61)
    // Every ACK the run emitted is cumulative and never regresses (rule 7).
    let acked = run.effects.compactMap { effect -> EventCursor? in
      if case .sendAck(let snapshotID, let cursor) = effect, snapshotID == nil { return cursor }
      return nil
    }
    XCTAssertEqual(acked, [25, 50, 61])
  }

  /// A dead socket's deadline cannot ACK on the live connection (D17 rule 1).
  func test_anOlderEpochDeadlineIsIgnored() throws {
    var run = try live()
    XCTAssertEqual(try run.durable(cursor: 1, [Fx.segment("R-1", 0)], epoch: 1), [flush])
    run.apply(try Fx.socketOpened(epoch: 2))

    XCTAssertEqual(run.apply(.ackDeadline(socketEpoch: 1)), [])
    XCTAssertEqual(run.state.synchronization.unackedDurableCount, 1)
  }

  /// D8 step 4 is untouched: adoption still ACKs the exact snapshot id and H,
  /// and it starts the durable window empty.
  func test_theSnapshotAdoptionAckIsUnchanged() throws {
    var run = Reducing()
    let adopted = try run.goLive(
      through: 42, groups: [Fx.snapshotGroup("G-1", responses: [Fx.snapshotResponse("R-1")])]
    )
    XCTAssertEqual(adopted, [.sendAck(snapshotID: "SNAP-1", throughCursor: 42)])
    XCTAssertEqual(run.state.synchronization.unackedDurableCount, 0)

    // A re-adoption after a durable batch clears the window with its own ACK.
    XCTAssertEqual(try run.durable(cursor: 43, [Fx.segment("R-1", 0)]), [flush])
    let readopted = try run.goLive(
      epoch: 2, through: 50,
      groups: [Fx.snapshotGroup("G-1", responses: [Fx.snapshotResponse("R-1")])]
    )
    XCTAssertEqual(readopted, [.sendAck(snapshotID: "SNAP-1", throughCursor: 50)])
    XCTAssertEqual(run.state.synchronization.unackedDurableCount, 0)
    XCTAssertEqual(run.apply(.ackDeadline(socketEpoch: 2)), [])
  }
}
