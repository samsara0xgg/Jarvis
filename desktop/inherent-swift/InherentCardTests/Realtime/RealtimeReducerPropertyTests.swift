import XCTest
@testable import InherentRealtime

/// ADR-0014 §15.3: "Property tests add legal duplicates, stale playback
/// generations, reconnect boundaries, batched mutations, and permitted
/// permutations; final reducer state must equal the canonical snapshot state."
final class RealtimeReducerPropertyTests: XCTestCase {
  private struct Step {
    let cursor: Int
    let mutations: [[String: Any]]
    /// Segments of one response, which D17 rules 7 to 9 may legally reorder.
    let permutable: Bool
  }

  /// One conversation: two groups, segments, lifecycle, playback generations,
  /// an action and a confirmation.  Cursors are ten apart so a split batch has
  /// room for an intermediate cursor and the run still ends on the same one.
  private func script() -> [Step] {
    let steps: [(mutations: [[String: Any]], permutable: Bool)] = [
      ([Fx.inputCommitted(utterance: "U-1", turn: "T-1", group: "G-1", text: "lock the door")], false),
      ([Fx.opened("R-1", group: "G-1", phase: "commentary", channel: "both")], false),
      (
        [
          Fx.segment("R-1", 0, text: "on", phase: "commentary"),
          Fx.segment("R-1", 1, text: " it", phase: "commentary"),
          Fx.segment("R-1", 2, text: " now", phase: "commentary"),
        ], true
      ),
      ([Fx.playback("R-1", "P-0", "buffering", revision: 1)], false),
      ([Fx.playback("R-1", "P-1", "speaking", revision: 1)], false),
      ([Fx.action("A-1", group: "G-1", state: "running", revision: 1)], false),
      ([Fx.lifecycle("R-1", "waiting_action", revision: 2)], false),
      ([Fx.confirmation("C-1", group: "G-1", action: "A-1", revision: 1)], false),
      ([Fx.confirmationCleared("C-1", reason: "accepted", revision: 2)], false),
      ([Fx.action("A-1", group: "G-1", state: "result_observed", revision: 2)], false),
      ([Fx.inputCommitted(utterance: "U-2", turn: "T-2", group: "G-2", text: "what time is it")], false),
      ([Fx.opened("R-2", group: "G-2")], false),
      ([Fx.segment("R-2", 0, text: "half"), Fx.segment("R-2", 1, text: " past four")], true),
      ([Fx.lifecycle("R-1", "completed", revision: 3), Fx.delivery("R-1", "closed")], false),
      ([Fx.playback("R-1", "P-1", "completed", revision: 2, heard: 2)], false),
      (
        [
          Fx.lifecycle("R-2", "completed", revision: 2), Fx.delivery("R-2", "closed"),
          Fx.notice("N-1", subject: "R-2"),
        ], false
      ),
    ]
    return steps.enumerated().map {
      Step(cursor: 10 * ($0.offset + 1), mutations: $0.element.mutations, permutable: $0.element.permutable)
    }
  }

  private enum Item {
    case envelope(cursor: Int, changes: [[String: Any]])
    /// socketClosed + socketOpened on the same identities.
    case reconnect
  }

  private func run(_ items: [Item]) throws -> InherentUXState {
    var run = Reducing()
    try run.goLive()
    for item in items {
      switch item {
      case .envelope(let cursor, let changes):
        try run.durable(cursor: cursor, changes)
      case .reconnect:
        run.apply(.socketClosed(socketEpoch: 1, code: 1006, reason: "abnormal"))
        run.apply(try Fx.socketOpened(epoch: 1))
      }
    }
    return run.state
  }

  /// The canonical order: one envelope per step, in cursor order, no duplicates.
  private func canonicalItems() -> [Item] {
    script().map { .envelope(cursor: $0.cursor, changes: $0.mutations) }
  }

  /// The same mutations, legally re-framed: batches split or merged with
  /// re-numbered cursors, segments permuted inside the gap buffer's reach,
  /// verbatim duplicates, stale playback generations, and reconnect boundaries.
  private func variantItems(seed: UInt64) -> [Item] {
    var rng = SplitMix64(seed: seed)
    let steps = script()
    var items: [Item] = []
    var sent: [(cursor: Int, changes: [[String: Any]])] = []
    var index = 0
    var lastEmittedStep = -1

    func emit(_ cursor: Int, _ changes: [[String: Any]]) {
      items.append(.envelope(cursor: cursor, changes: changes))
      sent.append((cursor, changes))
    }

    while index < steps.count {
      // P-1 takes the playback lease at step 4; only after that envelope has
      // actually been sent is a P-0 update a stale generation.
      let staleIsPossible = lastEmittedStep >= 4
      var mutations = steps[index].permutable
        ? steps[index].mutations.shuffled(using: &rng) : steps[index].mutations
      var cursor = steps[index].cursor

      if index + 1 < steps.count, rng.chance(0.25) {
        let next = steps[index + 1]
        mutations += next.permutable ? next.mutations.shuffled(using: &rng) : next.mutations
        cursor = next.cursor
        index += 1
      }

      if staleIsPossible, rng.chance(0.25) {
        emit(cursor - 8, [Fx.playback("R-1", "P-0", "speaking", revision: 9)])
      }
      if rng.chance(0.2) { items.append(.reconnect) }

      if mutations.count >= 2, rng.chance(0.4) {
        let split = 1 + rng.index(below: mutations.count - 1)
        emit(cursor - 5, Array(mutations[..<split]))
        emit(cursor, Array(mutations[split...]))
      } else {
        emit(cursor, mutations)
      }
      if !sent.isEmpty, rng.chance(0.3) {
        let earlier = sent[rng.index(below: sent.count)]
        items.append(.envelope(cursor: earlier.cursor, changes: earlier.changes))
      }

      lastEmittedStep = index
      index += 1
    }
    return items
  }

  /// The four fields a re-framing is allowed to differ in:
  /// `recentMessageIDs` remembers which envelopes carried the mutations and
  /// `unackedDurableCount` how many of them are still unACKed, so both record
  /// the framing rather than the content; `resyncRequested` records that a
  /// permutation opened a gap the client asked to repair; `connection` records
  /// the inserted reconnect boundary.  Everything else — content, identity,
  /// revisions, cursors, foreground, capabilities — must match.
  private func comparable(_ state: InherentUXState) -> InherentUXState {
    var state = state
    state.synchronization.recentMessageIDs = []
    state.synchronization.unackedDurableCount = 0
    state.synchronization.resyncRequested = false
    state.connection = .live
    return state
  }

  func test_legalReframingsReachTheCanonicalState() throws {
    let canonical = try run(canonicalItems())

    // Guard against a vacuous canonical: the script must really have landed.
    XCTAssertEqual(canonical.responseOrder, [ResponseGroupID("G-1"), ResponseGroupID("G-2")])
    XCTAssertEqual(
      canonical.responseGroups[ResponseGroupID("G-1")]?
        .responses[ResponseID("R-1")]?.segments.map(\.text), ["on", " it", " now"]
    )
    XCTAssertEqual(canonical.foregroundGroupID, ResponseGroupID("G-2"))
    XCTAssertEqual(canonical.actions[ActionID("A-1")]?.state, .resultObserved)
    XCTAssertNil(canonical.pendingConfirmation)
    XCTAssertEqual(canonical.synchronization.lastAppliedCursor, 160)

    var reframed = 0
    var permuted = 0
    for seed in UInt64(1)...200 {
      let items = variantItems(seed: seed)
      if items.count != canonicalItems().count { reframed += 1 }
      let variant = try run(items)
      // A permutation is what opens a gap, so this proves the sequences really
      // arrived out of order and the gap buffer really resolved them.
      if variant.synchronization.resyncRequested { permuted += 1 }
      XCTAssertEqual(
        comparable(variant), comparable(canonical), "seed \(seed) diverged from the canonical state"
      )
    }
    // Guard against a variant generator that quietly emits the canonical order.
    XCTAssertGreaterThan(reframed, 150, "most seeds must re-frame the envelope stream")
    XCTAssertGreaterThan(permuted, 100, "most seeds must permute segments past the expected one")
  }
}
