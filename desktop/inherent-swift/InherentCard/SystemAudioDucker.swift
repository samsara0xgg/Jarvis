import Foundation

final class SystemAudioDucker {
  struct Snapshot: Equatable {
    let outputVolume: Int
    let outputMuted: Bool
  }

  private let lock = NSLock()
  private var depth = 0
  private var snapshot: Snapshot?

  func duck() -> Bool {
    guard ProcessInfo.processInfo.operatingSystemVersion.majorVersion >= 10 else { return false }

    lock.lock()
    defer { lock.unlock() }

    if depth > 0 {
      depth += 1
      return true
    }

    do {
      let currentSnapshot = try readSnapshot()
      snapshot = currentSnapshot
      try runAppleScript("""
        set volume output volume 0
        try
          set volume output muted true
        end try
      """)
      depth = 1
      return true
    } catch {
      if let snapshot {
        try? restore(snapshot)
      }
      snapshot = nil
      depth = 0
      NSLog("[audio-ducking] failed to duck system output: \(error.localizedDescription)")
      return false
    }
  }

  func restore() {
    lock.lock()
    if depth <= 0 {
      lock.unlock()
      return
    }
    depth -= 1
    if depth > 0 {
      lock.unlock()
      return
    }
    let currentSnapshot = snapshot
    snapshot = nil
    lock.unlock()

    guard let currentSnapshot else { return }
    do {
      try restore(currentSnapshot)
    } catch {
      NSLog("[audio-ducking] failed to restore system output: \(error.localizedDescription)")
    }
  }

  func restoreAll() {
    lock.lock()
    depth = 0
    let currentSnapshot = snapshot
    snapshot = nil
    lock.unlock()

    guard let currentSnapshot else { return }
    do {
      try restore(currentSnapshot)
    } catch {
      NSLog("[audio-ducking] failed to restore system output: \(error.localizedDescription)")
    }
  }

  private func readSnapshot() throws -> Snapshot {
    let raw = try runAppleScript("""
      set s to get volume settings
      return (output volume of s as text) & "," & (output muted of s as text)
    """)
    return try Self.parseSnapshot(raw)
  }

  private func restore(_ snapshot: Snapshot) throws {
    try runAppleScript("""
      set volume output volume \(snapshot.outputVolume)
      try
        set volume output muted \(snapshot.outputMuted ? "true" : "false")
      end try
    """)
  }

  static func parseSnapshot(_ raw: String) throws -> Snapshot {
    let parts = raw.trimmingCharacters(in: .whitespacesAndNewlines)
      .split(separator: ",", omittingEmptySubsequences: false)
      .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
    guard parts.count == 2, let volume = Int(parts[0]) else {
      throw NSError(
        domain: "SystemAudioDucker",
        code: 1,
        userInfo: [NSLocalizedDescriptionKey: "Unexpected volume settings: \(raw)"]
      )
    }
    return Snapshot(
      outputVolume: max(0, min(100, volume)),
      outputMuted: parts[1].lowercased() == "true"
    )
  }

  @discardableResult
  private func runAppleScript(_ source: String) throws -> String {
    guard let script = NSAppleScript(source: source) else {
      throw NSError(
        domain: "SystemAudioDucker",
        code: 2,
        userInfo: [NSLocalizedDescriptionKey: "AppleScript compilation failed"]
      )
    }

    var errorInfo: NSDictionary?
    let result = script.executeAndReturnError(&errorInfo)
    if let errorInfo {
      throw NSError(
        domain: "SystemAudioDucker",
        code: 3,
        userInfo: errorInfo as? [String: Any] ?? [NSLocalizedDescriptionKey: "\(errorInfo)"]
      )
    }
    return result.stringValue ?? ""
  }
}
