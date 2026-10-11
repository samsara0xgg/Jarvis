import Foundation
import MetricKit
import os
import UIKit

/// The main thread held up: every stall over 250 ms, a stall still going after 2 s, and once a minute how late the main thread ran when it was often late (a crawl, no single long stall).
/// To the unified log (category `stall`) and to `Library/Caches/stall.log`; the system's own hang reports, with their stacks (MetricKit), go to `Library/Caches/hangs/`.
/// Never what was said or typed: counts, states, and the names of the last few things that happened.
/// Pull from the phone: `xcrun devicectl device copy from --device <udid> --domain-type appDataContainer --domain-identifier com.yilunshi.jarvis --source Library/Caches/stall.log --destination <dir>`
final class StallLog: NSObject, MXMetricManagerSubscriber, @unchecked Sendable {
    static let shared = StallLog()
    /// what the app is in, read on the main thread when a stall ends (StarChrome sets it)
    @MainActor static var context: () -> String = { "" }

    private static let log = Logger(subsystem: "com.yilunshi.jarvis", category: "stall")
    private static let caches = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
    private static let file = caches.appendingPathComponent("stall.log")
    private let lock = NSLock()
    private var marks: [String] = []            // the last things that happened, oldest first
    private var counts: [String: Int] = [:]     // events counted since the last minute line
    private var started = false
    private var active = false                  // the app was on screen at the last ping

    /// a thing that happened, by name (no user text): it is listed with the next stall
    static func mark(_ s: String) { shared.locked { shared.marks.append("\(stamp()) \(s)"); if shared.marks.count > 10 { shared.marks.removeFirst() } } }
    /// an event that is cheap alone and costly in numbers (a recompose, a scroll): the minute line says how many
    static func count(_ s: String) { shared.locked { shared.counts[s, default: 0] += 1 } }

    func start() {
        guard !started else { return }
        started = true
        MXMetricManager.shared.add(self)
        let t = Thread { self.watch() }
        t.qualityOfService = .userInitiated; t.name = "stall-watch"
        t.start()
        write("start \(Bundle.main.infoDictionary?["CFBundleVersion"] as? String ?? "")")
    }

    // MARK: watching

    private func watch() {
        var lateMs = 0.0, late = 0, windowAt = Date()
        while true {
            let sem = DispatchSemaphore(value: 0), t0 = DispatchTime.now()
            var ms = 0.0
            DispatchQueue.main.async {
                ms = Double(DispatchTime.now().uptimeNanoseconds - t0.uptimeNanoseconds) / 1e6
                // a ping sent before the app was suspended comes back late: only an app on screen counts
                let on = UIApplication.shared.applicationState == .active
                self.locked { self.active = on }
                if !on { ms = 0 }
                if ms > 250 { self.stall(ms, MainActor.assumeIsolated { StallLog.context() }) }
                sem.signal()
            }
            if sem.wait(timeout: .now() + 2) == .timedOut {
                if locked({ active }) { write("STUCK >2000ms marks=[\(locked { marks.joined(separator: " | ") })]") }
                sem.wait()
            }
            if ms > 50 { late += 1; lateMs += ms }
            if Date().timeIntervalSince(windowAt) >= 60 {
                let c = locked { () -> String in defer { counts = [:] }; return counts.sorted { $0.key < $1.key }.map { "\($0.key)=\($0.value)" }.joined(separator: " ") }
                if lateMs > 1000 { write("crawl \(late) late pings, \(Int(lateMs))ms late in 60s \(c)") }
                late = 0; lateMs = 0; windowAt = Date()
            }
            Thread.sleep(forTimeInterval: 0.1)
        }
    }

    private func stall(_ ms: Double, _ ctx: String) {
        write("stall \(Int(ms))ms \(ctx) marks=[\(locked { marks.joined(separator: " | ") })]")
    }

    // MARK: MetricKit: the system's hang and crash reports, with stacks

    func didReceive(_ payloads: [MXDiagnosticPayload]) {
        let dir = Self.caches.appendingPathComponent("hangs", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        for p in payloads {
            let name = "\(Int(p.timeStampEnd.timeIntervalSince1970))-\(UUID().uuidString.prefix(6)).json"
            try? p.jsonRepresentation().write(to: dir.appendingPathComponent(name))
            write("metrickit hangs=\(p.hangDiagnostics?.count ?? 0) crashes=\(p.crashDiagnostics?.count ?? 0) -> hangs/\(name)")
        }
    }

    // MARK: the file

    private func write(_ s: String) {
        Self.log.notice("\(s, privacy: .public)")
        append(s, to: Self.file)
    }

    /// A line of another of the app's stories to its own file under Library/Caches, kept the same way (the voice: `voice.log`).
    static func append(_ s: String, to name: String) { shared.append(s, to: caches.appendingPathComponent(name)) }

    private func append(_ s: String, to f: URL) {
        locked {
            let fm = FileManager.default
            if let n = (try? fm.attributesOfItem(atPath: f.path))?[.size] as? Int, n > 100_000 {
                let old = f.deletingPathExtension().appendingPathExtension("1.log")
                try? fm.removeItem(at: old); try? fm.moveItem(at: f, to: old)
            }
            let line = Data("\(Self.stamp()) \(s)\n".utf8)
            if let h = try? FileHandle(forWritingTo: f) { h.seekToEndOfFile(); h.write(line); try? h.close() } else { try? line.write(to: f) }
        }
    }

    private func locked<T>(_ f: () -> T) -> T { lock.lock(); defer { lock.unlock() }; return f() }
    private static let fmt: DateFormatter = { let f = DateFormatter(); f.locale = Locale(identifier: "en_US_POSIX"); f.dateFormat = "MM-dd HH:mm:ss.SSS"; return f }()
    private static func stamp() -> String { fmt.string(from: Date()) }
}
