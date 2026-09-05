import CryptoKit
import XCTest
@testable import InherentRealtime

/// Server frames as bytes.
///
/// The mailbox carries raw `Data` because the D8 content hash is defined over
/// the exact bytes of every `snapshot.page` frame, so a test that hands the
/// transport re-encoded values would verify a hash no server ever computed.
/// Each builder returns the JSON object; the test turns it into bytes and, for
/// the expectation, decodes the same object through the real DTOs.
private enum Frames {
  static func envelope(
    _ messageType: String,
    payload: Any,
    id: String,
    delivery: String = "protocol",
    cursor: Int? = nil,
    sequence: Int? = nil
  ) -> [String: Any] {
    var frame: [String: Any] = [
      "protocol_version": 2, "message_type": messageType, "message_id": id,
      "delivery_class": delivery, "connection_id": Fx.connectionID,
      "log_epoch": Fx.logEpoch, "boot_id": Fx.bootID, "sent_at_ms": 1_788_200_000_000,
      "payload": payload,
    ]
    if let cursor { frame["event_cursor"] = cursor }
    if let sequence { frame["ephemeral_sequence"] = sequence }
    return frame
  }

  static func viewDelta(cursor: Int, _ changes: [[String: Any]]) -> [String: Any] {
    envelope(
      "view.delta",
      payload: ["source_event_uid": "E-\(cursor)", "changes": changes],
      id: "M-\(cursor)", delivery: "durable", cursor: cursor
    )
  }

  static func snapshotBegin(
    through: Int, sections: [String], counts: [String: Int], id: String = "SNAP-1"
  ) -> [String: Any] {
    envelope(
      "snapshot.begin",
      payload: [
        "snapshot_id": id, "through_cursor": through, "view_schema_version": 1,
        "section_order": sections, "counts": counts,
      ],
      id: "SB-1"
    )
  }

  static func snapshotPage(
    section: String, index: Int, items: [[String: Any]], id: String = "SNAP-1"
  ) -> [String: Any] {
    envelope(
      "snapshot.page",
      payload: ["snapshot_id": id, "section": section, "page_index": index, "items": items],
      id: "SP-\(section)-\(index)"
    )
  }

  static func snapshotEnd(
    through: Int, hash: String, id: String = "SNAP-1"
  ) -> [String: Any] {
    envelope(
      "snapshot.end",
      payload: ["snapshot_id": id, "through_cursor": through, "content_hash": hash],
      id: "SE-1"
    )
  }

  static func bytes(_ object: [String: Any]) throws -> Data {
    try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
  }

  /// The D8 hash, computed here the way the server computes it: over the page
  /// frames in section order, then page index.
  static func contentHash(_ pages: [Data]) -> String {
    var hasher = SHA256()
    for page in pages { hasher.update(data: page) }
    return hasher.finalize().map { String(format: "%02x", $0) }.joined()
  }
}

/// A sink that records what it was given and how many applies overlapped.
private actor ApplyRecorder {
  struct Applied: Equatable {
    let index: Int
    let event: InherentClientEvent
  }

  private(set) var applied: [Applied] = []
  private(set) var inFlight = 0
  private(set) var maxInFlight = 0

  func enter(_ index: Int, _ event: InherentClientEvent) {
    applied.append(Applied(index: index, event: event))
    inFlight += 1
    maxInFlight = max(maxInFlight, inFlight)
  }

  func leave() {
    inFlight -= 1
  }
}

/// Holds each apply open long enough that a second reader would overlap it.
private func recordingSink(
  _ recorder: ApplyRecorder, hold: Duration = .milliseconds(2)
) -> RealtimeTransport.EventSink {
  { index, event in
    await recorder.enter(index, event)
    try? await Task.sleep(for: hold)
    await recorder.leave()
  }
}

private func mailbox(_ inputs: [TransportInput]) -> AsyncStream<TransportInput> {
  let (stream, continuation) = AsyncStream<TransportInput>.makeStream()
  for input in inputs { continuation.yield(input) }
  continuation.finish()
  return stream
}

private func drain(
  _ inputs: [TransportInput], sink: @escaping RealtimeTransport.EventSink
) async -> RealtimeTransport {
  let transport = RealtimeTransport(inputs: mailbox(inputs), sink: sink)
  await transport.run()
  return transport
}

private func waitUntil(
  _ description: String, _ condition: () async -> Bool
) async throws {
  for _ in 0..<500 {
    if await condition() { return }
    try await Task.sleep(for: .milliseconds(2))
  }
  XCTFail("timed out waiting for \(description)")
}

/// ADR-0014 D4 (one receive task, one mailbox, monotonic `receiveIndex`) and D8
/// (staged snapshot, hash verified over the raw page bytes before adoption).
final class RealtimeTransportTests: XCTestCase {
  private static let opened = TransportInput.opened(
    socketEpoch: 1, connectionID: Fx.connectionID, hello: try! Fx.hello(),
    logEpoch: Fx.logEpoch, bootID: Fx.bootID
  )

  // MARK: - Ordering

  /// D4: items are taken in order, one at a time, and each apply completes
  /// before the next item is pulled.
  func test_itemsApplyInReceiveIndexOrderOneAtATime() async throws {
    var inputs: [TransportInput] = [Self.opened]
    var expected: [InherentClientEvent] = [try Fx.socketOpened(epoch: 1)]

    for cursor in 1...10 {
      let object = Frames.viewDelta(cursor: cursor, [Fx.opened("R-\(cursor)", group: "G-1")])
      inputs.append(.frame(socketEpoch: 1, try Frames.bytes(object)))
      expected.append(
        .durable(socketEpoch: 1, try Fx.decode(ServerEnvelope<ViewDeltaPayload>.self, object))
      )
    }
    for sequence in 1...5 {
      let object = Frames.envelope(
        "input.partial", payload: Fx.inputPartial("U-1", revision: sequence, text: "half a"),
        id: "EM-\(sequence)", delivery: "ephemeral", sequence: sequence
      )
      inputs.append(.frame(socketEpoch: 1, try Frames.bytes(object)))
      expected.append(
        .ephemeral(socketEpoch: 1, try Fx.decode(ServerEnvelope<EphemeralUpdate>.self, object))
      )
    }
    inputs.append(.reconnecting(socketEpoch: 1))
    expected.append(.reconnecting(socketEpoch: 1))
    inputs.append(.closed(socketEpoch: 1, code: 1006, reason: "abnormal"))
    expected.append(.socketClosed(socketEpoch: 1, code: 1006, reason: "abnormal"))
    inputs.append(.failed(socketEpoch: 1, reason: "timeout"))
    expected.append(.socketFailed(socketEpoch: 1, reason: "timeout"))
    let last = Frames.viewDelta(cursor: 11, [Fx.segment("R-1", 0)])
    inputs.append(.frame(socketEpoch: 1, try Frames.bytes(last)))
    expected.append(
      .durable(socketEpoch: 1, try Fx.decode(ServerEnvelope<ViewDeltaPayload>.self, last))
    )
    XCTAssertEqual(inputs.count, 20)

    let recorder = ApplyRecorder()
    let transport = await drain(inputs, sink: recordingSink(recorder))

    let applied = await recorder.applied
    XCTAssertEqual(applied.map(\.index), Array(1...20))
    XCTAssertEqual(applied.map(\.event), expected)
    let maxInFlight = await recorder.maxInFlight
    XCTAssertEqual(maxInFlight, 1, "an apply must finish before the next item is pulled")
    let receiveIndex = await transport.receiveIndex
    let lastAppliedIndex = await transport.lastAppliedIndex
    XCTAssertEqual(receiveIndex, 20)
    XCTAssertEqual(lastAppliedIndex, 20)
  }

  /// D4: actor reentrancy may not create a second reader.
  func test_aSecondRunNeverReadsTheMailbox() async throws {
    let (stream, continuation) = AsyncStream<TransportInput>.makeStream()
    let recorder = ApplyRecorder()
    let transport = RealtimeTransport(
      inputs: stream, sink: recordingSink(recorder, hold: .milliseconds(20))
    )
    for epoch in 1...3 { continuation.yield(.reconnecting(socketEpoch: epoch)) }

    let first = Task { await transport.run() }
    try await waitUntil("the first run to apply an item") { await transport.lastAppliedIndex >= 1 }
    let isRunning = await transport.isRunning
    XCTAssertTrue(isRunning)

    // A concurrent second call: it returns without taking an item.
    await transport.run()

    continuation.finish()
    await first.value

    let stillRunning = await transport.isRunning
    let hasRun = await transport.hasRun
    let receiveIndex = await transport.receiveIndex
    let applied = await recorder.applied
    XCTAssertFalse(stillRunning)
    XCTAssertTrue(hasRun)
    XCTAssertEqual(receiveIndex, 3)
    XCTAssertEqual(applied.map(\.index), [1, 2, 3], "every item was read exactly once")

    // And a call after the stream finished reads nothing either.
    await transport.run()
    let finalIndex = await transport.receiveIndex
    let finalApplied = await recorder.applied
    XCTAssertEqual(finalIndex, 3)
    XCTAssertEqual(finalApplied.count, 3)
  }

  /// A frame the client cannot decode is a protocol violation, not a skip.
  func test_anUndecodableFrameFailsTheSocket() async throws {
    var wrongVersion = Frames.viewDelta(cursor: 1, [])
    wrongVersion["protocol_version"] = 3
    let unknownDurableKind = Frames.viewDelta(cursor: 2, [["kind": "response.telepathy"]])

    let recorder = ApplyRecorder()
    _ = await drain(
      [
        .frame(socketEpoch: 7, Data("{\"protocol_version\": 2".utf8)),
        .frame(socketEpoch: 7, try Frames.bytes(wrongVersion)),
        .frame(socketEpoch: 7, try Frames.bytes(unknownDurableKind)),
      ],
      sink: recordingSink(recorder)
    )

    let applied = await recorder.applied
    XCTAssertEqual(applied.map(\.index), [1, 2, 3])
    XCTAssertEqual(
      applied.map(\.event),
      Array(repeating: .socketFailed(socketEpoch: 7, reason: "protocol_error"), count: 3)
    )
  }

  // MARK: - Snapshot staging (D8)

  @MainActor
  func test_aVerifiedSnapshotDrivesTheStoreLive() async throws {
    let staged = try Self.stagedSnapshot()
    let end = try Frames.bytes(Frames.snapshotEnd(through: 42, hash: staged.hash))
    let runner = RecordingEffectRunner()
    let store = RealtimeStore(runner: runner)

    await Self.drive(begin: staged.begin, pages: staged.pages, end: end, into: store)

    XCTAssertEqual(store.state.connection, .live)
    XCTAssertEqual(store.state.synchronization.lastAppliedCursor, 42)
    let group = try XCTUnwrap(store.state.responseGroups[ResponseGroupID("G-1")])
    XCTAssertEqual(group.question, "what is the weather")
    XCTAssertEqual(
      group.responses[ResponseID("R-1")]?.segments.map(\.text), ["cold and clear"]
    )
    XCTAssertEqual(store.state.actions[ActionID("A-1")]?.state, .running)
    let recorded = await runner.recorded
    XCTAssertEqual(recorded, [.sendAck(snapshotID: "SNAP-1", throughCursor: 42)])
  }

  @MainActor
  func test_aWrongContentHashIsNeverAdopted() async throws {
    let staged = try Self.stagedSnapshot()
    let end = try Frames.bytes(
      Frames.snapshotEnd(through: 42, hash: String(repeating: "0", count: 64))
    )
    try await Self.expectNoAdoption(begin: staged.begin, pages: staged.pages, end: end)
  }

  @MainActor
  func test_aPageCountMismatchIsNeverAdopted() async throws {
    // `snapshot.begin` promises two response_groups pages; one arrives.
    let staged = try Self.stagedSnapshot(counts: ["response_groups": 2, "actions": 1])
    let end = try Frames.bytes(Frames.snapshotEnd(through: 42, hash: staged.hash))
    try await Self.expectNoAdoption(begin: staged.begin, pages: staged.pages, end: end)
  }

  @MainActor
  func test_aThroughCursorMismatchIsNeverAdopted() async throws {
    let staged = try Self.stagedSnapshot()
    let end = try Frames.bytes(Frames.snapshotEnd(through: 43, hash: staged.hash))
    try await Self.expectNoAdoption(begin: staged.begin, pages: staged.pages, end: end)
  }

  // MARK: - Snapshot helpers

  /// One snapshot: begin, two pages in two sections, and the hash over their
  /// exact bytes in section order.
  private static func stagedSnapshot(
    through: Int = 42, counts: [String: Int]? = nil
  ) throws -> (begin: Data, pages: [Data], hash: String) {
    let groupsPage = Frames.snapshotPage(
      section: "response_groups", index: 0,
      items: [
        Fx.snapshotGroup(
          "G-1", turn: "T-1", question: "what is the weather",
          responses: [
            Fx.snapshotResponse(
              "R-1", segments: [Fx.segment("R-1", 0, text: "cold and clear")]
            )
          ]
        )
      ]
    )
    let actionsPage = Frames.snapshotPage(
      section: "actions", index: 0,
      items: [Fx.action("A-1", group: "G-1", state: "running", revision: 3)]
    )
    let pages = [try Frames.bytes(groupsPage), try Frames.bytes(actionsPage)]
    let begin = try Frames.bytes(
      Frames.snapshotBegin(
        through: through, sections: ["response_groups", "actions"],
        counts: counts ?? ["response_groups": 1, "actions": 1]
      )
    )
    return (begin, pages, Frames.contentHash(pages))
  }

  @MainActor
  private static func drive(
    begin: Data, pages: [Data], end: Data, into store: RealtimeStore
  ) async {
    var inputs: [TransportInput] = [opened, .frame(socketEpoch: 1, begin)]
    inputs += pages.map { .frame(socketEpoch: 1, $0) }
    inputs.append(.frame(socketEpoch: 1, end))
    await RealtimeTransport(store: store, inputs: mailbox(inputs)).run()
  }

  /// Nothing is adopted, and the only difference from the pre-snapshot state is
  /// what the reducer does with `.snapshotFailed`: it raises the reconnect
  /// banner and asks for a repair (RealtimeReducer.swift `case .snapshotFailed`).
  @MainActor
  private static func expectNoAdoption(
    begin: Data, pages: [Data], end: Data,
    file: StaticString = #filePath, line: UInt = #line
  ) async throws {
    let runner = RecordingEffectRunner()
    let store = RealtimeStore(runner: runner)
    await drive(begin: begin, pages: pages, end: end, into: store)

    let reference = RealtimeStore(runner: RecordingEffectRunner())
    await reference.apply(try Fx.socketOpened(epoch: 1))
    var expected = reference.state
    expected.connection = .reconnecting
    expected.synchronization.needsSnapshotRepair = true

    XCTAssertEqual(store.state, expected, file: file, line: line)
    let recorded = await runner.recorded
    XCTAssertEqual(recorded, [], file: file, line: line)
  }
}
