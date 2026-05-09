import AppKit
import Foundation

final class ParentWatchdog {
  private var source: DispatchSourceProcess?

  /// Watches the parent process PID for exit, gated by env var
  /// `JARVIS_INHERENT_PARENT_LIFETIME=1`. When the parent exits, terminates
  /// this app cleanly (NSApp.terminate). Used in dev launches where Python
  /// owns the Swift app's lifetime; production launches leave the var unset.
  func startIfRequested() {
    let env = ProcessInfo.processInfo.environment
    guard env["JARVIS_INHERENT_PARENT_LIFETIME"] == "1" else { return }

    let parentPID = getppid()
    if parentPID <= 1 {
      NSLog("[watchdog] parent PID is \(parentPID) — refusing to watch init")
      return
    }
    let src = DispatchSource.makeProcessSource(identifier: parentPID, eventMask: .exit, queue: .main)
    src.setEventHandler {
      NSLog("[watchdog] parent (\(parentPID)) exited; terminating")
      NSApp.terminate(nil)
    }
    src.resume()
    self.source = src
    NSLog("[watchdog] watching parent PID \(parentPID)")
  }

  func stop() {
    source?.cancel()
    source = nil
  }
}
