import CryptoKit
import Foundation

/// The ADR-0014 D4 mailbox actor, the D8 snapshot staging state, and nothing
/// else: no socket, no AppKit, no URLSession.  What the socket produces arrives
/// as an injected `AsyncStream<TransportInput>`, and what the client decides
/// leaves as an `InherentClientEvent` handed to the store.

// MARK: - Mailbox input

/// One item the socket owner puts in the mailbox.
public enum TransportInput: Sendable {
  /// One complete server text frame, exactly as received: the D8 content hash
  /// covers these bytes, so nothing may re-encode them first.
  case frame(socketEpoch: Int, Data)
  case closed(socketEpoch: Int, code: Int?, reason: String)
  case failed(socketEpoch: Int, reason: String)
  case reconnecting(socketEpoch: Int)
  case opened(
    socketEpoch: Int,
    connectionID: String,
    hello: ServerHelloPayload,
    logEpoch: String,
    bootID: String
  )
}

// MARK: - Snapshot staging (D8)

/// Why a staged snapshot may not be adopted.  Every case fails the whole
/// snapshot: a partially adopted projection is exactly what D8 forbids.
public enum SnapshotStagingError: Error, Equatable, Sendable {
  /// A page or end frame with no `snapshot.begin` in front of it.
  case noActiveSnapshot
  case snapshotIDMismatch
  case schemaMismatch
  case cursorMismatch
  /// A page for a section `snapshot.begin` never advertised; it would be
  /// invisible to both the count check and the hash.
  case unexpectedSection(SnapshotSection)
  case pageCountMismatch(SnapshotSection)
  case missingPage(SnapshotSection, pageIndex: Int)
  case hashMismatch

  /// The `snapshotFailed` reason the reducer records.
  public var reason: String {
    switch self {
    case .noActiveSnapshot: return "no_active_snapshot"
    case .snapshotIDMismatch: return "snapshot_id_mismatch"
    case .schemaMismatch: return "schema_mismatch"
    case .cursorMismatch: return "through_cursor_mismatch"
    case .unexpectedSection(let section): return "unexpected_section:\(section.rawValue)"
    case .pageCountMismatch(let section): return "page_count_mismatch:\(section.rawValue)"
    case .missingPage(let section, let index): return "missing_page:\(section.rawValue):\(index)"
    case .hashMismatch: return "content_hash_mismatch"
    }
  }
}

/// The staged half of a snapshot: begin, the pages that arrived, and the raw
/// bytes of each one.
///
/// The bytes are kept because the D8 hash is defined over the exact UTF-8 of
/// every complete `snapshot.page` frame, ordered by the advertised section
/// order and then `page_index`.  Re-encoding the decoded page would hash a
/// different byte string and verify nothing.
public struct SnapshotStagingState: Sendable {
  public struct PageKey: Hashable, Sendable {
    public var section: SnapshotSection
    public var pageIndex: Int

    public init(section: SnapshotSection, pageIndex: Int) {
      self.section = section
      self.pageIndex = pageIndex
    }
  }

  public struct StagedPage: Equatable, Sendable {
    public var page: SnapshotPage
    /// The exact frame bytes the hash covers.
    public var frame: Data
  }

  public let begin: SnapshotBegin
  public private(set) var pages: [PageKey: StagedPage] = [:]

  public init(begin: SnapshotBegin) {
    self.begin = begin
  }

  /// A re-sent page replaces the earlier copy: the last complete frame for a
  /// key is the one the server hashed.
  public mutating func stage(
    _ page: SnapshotPage, frame: Data
  ) throws(SnapshotStagingError) {
    guard page.snapshotId == begin.snapshotId else { throw .snapshotIDMismatch }
    guard begin.sectionOrder.contains(page.section) else {
      throw .unexpectedSection(page.section)
    }
    pages[PageKey(section: page.section, pageIndex: page.pageIndex)] =
      StagedPage(page: page, frame: frame)
  }

  /// Checks the D8 end conditions and, only then, builds what the reducer adopts.
  ///
  /// The identities come from the `snapshot.end` envelope: they are the
  /// connection the snapshot was actually completed on.
  public func verify(
    end: SnapshotEnd, logEpoch: String, bootID: String, connectionID: String
  ) throws(SnapshotStagingError) -> VerifiedSnapshot {
    guard end.snapshotId == begin.snapshotId else { throw .snapshotIDMismatch }
    guard begin.viewSchemaVersion == RealtimeProtocol.viewSchemaVersion else {
      throw .schemaMismatch
    }
    guard end.throughCursor == begin.throughCursor else { throw .cursorMismatch }

    var ordered: [SnapshotPage] = []
    var hasher = SHA256()
    for section in begin.sectionOrder {
      let expected = begin.counts[section.rawValue] ?? 0
      guard pages.keys.filter({ $0.section == section }).count == expected else {
        throw .pageCountMismatch(section)
      }
      for index in 0..<expected {
        guard let staged = pages[PageKey(section: section, pageIndex: index)] else {
          throw .missingPage(section, pageIndex: index)
        }
        hasher.update(data: staged.frame)
        ordered.append(staged.page)
      }
    }
    let digest = hasher.finalize().map { String(format: "%02x", $0) }.joined()
    guard digest == end.contentHash else { throw .hashMismatch }

    var groups: [ResponseGroupSnapshot] = []
    var actions: [ActionUpsert] = []
    var pendingConfirmation: ConfirmationUpsert?
    var capabilities: RuntimeCapabilities?
    var notices: [SurfaceNotice] = []
    for page in ordered {
      switch page.items {
      case .responseGroups(let items): groups += items
      case .actions(let items): actions += items
      case .pendingConfirmation(let items): pendingConfirmation = items.first ?? pendingConfirmation
      case .capabilities(let items):
        capabilities = items.first?.runtimeCapabilities ?? capabilities
      case .surfaceNotices(let items): notices += items
      }
    }
    return VerifiedSnapshot(
      snapshotId: begin.snapshotId,
      throughCursor: begin.throughCursor,
      viewSchemaVersion: begin.viewSchemaVersion,
      logEpoch: logEpoch,
      bootId: bootID,
      connectionId: connectionID,
      groups: groups,
      actions: actions,
      pendingConfirmation: pendingConfirmation,
      capabilities: capabilities,
      notices: notices
    )
  }
}

// MARK: - The mailbox actor (D4)

/// Owns exactly one receive task over one mailbox.
///
/// Per item it assigns the next `receiveIndex`, maps the item to zero or more
/// reducer events, and awaits the apply of each one before pulling the next
/// item.  Actor reentrancy cannot create a second reader: `run` is claimed
/// once, synchronously, before its first suspension.
public actor RealtimeTransport {
  /// Where an event goes.  The public initializer points it at the store; the
  /// tests point it at a recorder that can prove the applies never overlap.
  public typealias EventSink = @Sendable (Int, InherentClientEvent) async -> Void

  /// Every frame that fails to decode reports the same reason: the client
  /// cannot tell a corrupt frame from a frame it must not trust.
  static let protocolErrorReason = "protocol_error"

  private let inputs: AsyncStream<TransportInput>
  private let sink: EventSink
  private var staging: SnapshotStagingState?

  /// The count of mailbox items taken, monotonic from 1.
  public private(set) var receiveIndex = 0
  /// The last index whose events all completed their apply.
  public private(set) var lastAppliedIndex = 0
  /// True while the one receive task is draining the mailbox.
  public private(set) var isRunning = false
  /// True once the receive task has been claimed; a later `run` never reads.
  public private(set) var hasRun = false

  public init(store: RealtimeStore, inputs: AsyncStream<TransportInput>) {
    self.inputs = inputs
    self.sink = { _, event in await store.apply(event) }
  }

  init(inputs: AsyncStream<TransportInput>, sink: @escaping EventSink) {
    self.inputs = inputs
    self.sink = sink
  }

  /// Drains the mailbox to completion.  A second call, concurrent or later,
  /// returns without reading anything.
  public func run() async {
    guard !hasRun else { return }
    hasRun = true
    isRunning = true
    for await input in inputs {
      receiveIndex += 1
      for event in events(from: input) {
        await sink(receiveIndex, event)
        lastAppliedIndex = receiveIndex
      }
      lastAppliedIndex = receiveIndex
    }
    isRunning = false
  }

  // MARK: - Item to events

  private func events(from input: TransportInput) -> [InherentClientEvent] {
    switch input {
    case .opened(let epoch, let connectionID, let hello, let logEpoch, let bootID):
      return [
        .socketOpened(
          socketEpoch: epoch, connectionID: connectionID, hello: hello,
          logEpoch: logEpoch, bootID: bootID
        )
      ]
    case .closed(let epoch, let code, let reason):
      return [.socketClosed(socketEpoch: epoch, code: code, reason: reason)]
    case .failed(let epoch, let reason):
      return [.socketFailed(socketEpoch: epoch, reason: reason)]
    case .reconnecting(let epoch):
      return [.reconnecting(socketEpoch: epoch)]
    case .frame(let epoch, let data):
      return events(fromFrame: data, epoch: epoch)
    }
  }

  /// The header decodes first: `message_type` selects the payload type, and a
  /// frame whose envelope or payload does not decode fails the socket rather
  /// than being skipped (D6).
  private func events(fromFrame data: Data, epoch: Int) -> [InherentClientEvent] {
    let decoder = JSONDecoder()
    do {
      let header = try RealtimeProtocol.decodeServerEnvelope(data)
      switch header.messageType {
      case "view.delta":
        return [
          .durable(
            socketEpoch: epoch,
            try decoder.decode(ServerEnvelope<ViewDeltaPayload>.self, from: data)
          )
        ]
      case "snapshot.begin":
        staging = SnapshotStagingState(
          begin: try decoder.decode(ServerEnvelope<SnapshotBegin>.self, from: data).payload
        )
        return []
      case "snapshot.page":
        let frame = try decoder.decode(ServerEnvelope<SnapshotPage>.self, from: data)
        return stage(frame.payload, raw: data, epoch: epoch)
      case "snapshot.end":
        let frame = try decoder.decode(ServerEnvelope<SnapshotEnd>.self, from: data)
        return finish(frame, epoch: epoch)
      case "server.resync_required":
        // The server's own reason, verbatim: a caller that cannot tell
        // `client_backpressure` from `frame_over_budget` cannot tell a slow
        // reader from a frame no client could ever receive.
        let frame = try decoder.decode(ServerEnvelope<ResyncRequired>.self, from: data)
        return [.socketFailed(socketEpoch: epoch, reason: frame.payload.reason)]
      default:
        return [
          .ephemeral(
            socketEpoch: epoch,
            try decoder.decode(ServerEnvelope<EphemeralUpdate>.self, from: data)
          )
        ]
      }
    } catch {
      return [.socketFailed(socketEpoch: epoch, reason: Self.protocolErrorReason)]
    }
  }

  private func stage(
    _ page: SnapshotPage, raw: Data, epoch: Int
  ) -> [InherentClientEvent] {
    guard var pending = staging else {
      return [.snapshotFailed(socketEpoch: epoch, reason: SnapshotStagingError.noActiveSnapshot.reason)]
    }
    do {
      try pending.stage(page, frame: raw)
      staging = pending
      return []
    } catch {
      // A staging error ends the attempt: nothing half-staged is ever adopted.
      staging = nil
      return [.snapshotFailed(socketEpoch: epoch, reason: error.reason)]
    }
  }

  private func finish(
    _ frame: ServerEnvelope<SnapshotEnd>, epoch: Int
  ) -> [InherentClientEvent] {
    guard let pending = staging else {
      return [.snapshotFailed(socketEpoch: epoch, reason: SnapshotStagingError.noActiveSnapshot.reason)]
    }
    staging = nil
    do {
      let verified = try pending.verify(
        end: frame.payload, logEpoch: frame.logEpoch, bootID: frame.bootId,
        connectionID: frame.connectionId
      )
      return [.snapshotAdopted(socketEpoch: epoch, verified)]
    } catch {
      return [.snapshotFailed(socketEpoch: epoch, reason: error.reason)]
    }
  }
}
