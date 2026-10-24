import CoreMotion
import Foundation
import UIKit

/// The phone's raw accelerometer all day (ADR 0378): the motion coprocessor records it (`CMSensorRecorder`, 50 samples a second, kept 3 days),
/// and each wake reads back the 10-minute windows that have ended, on fixed UTC boundaries, as gzipped text pieces of kind `accel` in the
/// lifelog outbox; `LifelogUploader` sends them to the brain's raw archive (`PUT /inherent/lifelog/accel/{id}`). Nothing here judges whether
/// the phone was carried. A window's piece is named by its start, so reading it again after a kill makes the same piece.
@MainActor
final class AccelRecorder: Collector {
    let name = "accel"

    nonisolated static let windowMs: Int64 = 10 * 60 * 1000
    /// how long after a window ends the recorder has surely delivered it
    nonisolated static let lagMs: Int64 = 5 * 60 * 1000
    /// the recorder keeps 3 days; nothing older is asked for
    nonisolated static let keepMs: Int64 = 3 * 24 * 3600 * 1000
    /// asked again at every wake, so recording lapses only when the app is not woken for this long
    static let recordFor: TimeInterval = 12 * 3600
    nonisolated static let header = "# jarvis accel v1 rate_hz=50\nt_ms,x,y,z\n"
    /// a wake stops reading when less background time than this is left; the next wake goes on from the cursor
    static let reserve: TimeInterval = 10

    private static let cursorKey = "accel.cursor"
    private let recorder = CMSensorRecorder()
    private var reading = false

    /// the Motion & Fitness permission the activity history asks for; a phone without the recorder does not dim the motion switch
    var access: ReportAccess {
        switch CMSensorRecorder.authorizationStatus() {
        case .notDetermined: .notAsked
        case .authorized: .granted
        default: .denied
        }
    }

    func start() { Task { await wake() } }
    func stop() {}                                  // the recorder cannot be stopped; it lapses at the end of what was asked

    func wake() async {
        guard CMSensorRecorder.isAccelerometerRecordingAvailable(), CMSensorRecorder.authorizationStatus() == .authorized, !reading else { return }
        let now = Self.ms(Date())
        recorder.recordAccelerometer(forDuration: Self.recordFor)
        let cursor = ReportMemory.date(Self.cursorKey).map(Self.ms) ?? {     // first run: from the window recording began in
            let c = Self.align(now)
            ReportMemory.set(date: Self.date(c), for: Self.cursorKey)
            return c
        }()
        let windows = Self.windows(cursor: cursor, now: now)
        guard !windows.isEmpty else { return }
        reading = true
        defer { reading = false }
        let began = Date()
        var read = 0, samples = 0, bytes = 0
        for w in windows {
            guard UIApplication.shared.backgroundTimeRemaining > Self.reserve else { break }
            let r = await Task.detached(priority: .utility) { Self.cut(w) }.value
            guard let r else { break }                  // not written: read again next wake
            ReportMemory.set(date: Self.date(w + Self.windowMs), for: Self.cursorKey)
            read += 1; samples += r.samples; bytes += r.bytes
        }
        if bytes > 0 { LifelogUploader.shared.kick() }
        StallLog.append("accel: \(read)/\(windows.count) windows, \(samples) samples, \(bytes) bytes, \(String(format: "%.1f", Date().timeIntervalSince(began))) s", to: "accel.log")
    }

    /// Reads one window from the recorder and writes its piece; an empty window writes nothing. Nil when the piece could not be written.
    private nonisolated static func cut(_ w: Int64) -> (samples: Int, bytes: Int)? {
        var text = header, n = 0
        if let list = CMSensorRecorder().accelerometerData(from: date(w), to: date(w + windowMs)) {
            for case let d as CMRecordedAccelerometerData in list {
                let t = ms(d.startDate) - w
                guard (0..<windowMs).contains(t) else { continue }
                text += row(t: t, d.acceleration.x, d.acceleration.y, d.acceleration.z)
                n += 1
            }
        }
        guard n > 0 else { return (0, 0) }
        let p = piece(w)
        LifelogStore.ensure()
        guard let gz = Gzip.compress(Data(text.utf8)),
              (try? gz.write(to: p.file, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])) != nil,
              (try? p.save()) != nil else { return nil }
        return (n, gz.count)
    }

    /// The windows to read, oldest first: from the cursor (none older than 3 days) to the last that ended `lagMs` before now.
    nonisolated static func windows(cursor: Int64, now: Int64) -> [Int64] {
        let from = max(align(cursor), align(now - keepMs + windowMs - 1))
        return Array(stride(from: from, to: align(now - lagMs), by: Int(windowMs)))
    }

    /// One sample: its time from the window's start, and x, y, z in g, gravity in, written so that no two distinct values read the same.
    nonisolated static func row(t: Int64, _ x: Double, _ y: Double, _ z: Double) -> String { "\(t),\(x),\(y),\(z)\n" }

    nonisolated static func piece(_ w: Int64) -> LifelogPiece {
        LifelogPiece(id: "accel-\(w)", startedMs: w, durationMs: windowMs, ext: "csv.gz", kind: "accel")
    }

    nonisolated static func align(_ ms: Int64) -> Int64 { ms - ((ms % windowMs) + windowMs) % windowMs }
    nonisolated static func ms(_ d: Date) -> Int64 { Int64((d.timeIntervalSince1970 * 1000).rounded()) }
    nonisolated static func date(_ ms: Int64) -> Date { Date(timeIntervalSince1970: Double(ms) / 1000) }
}

extension CMSensorDataList: @retroactive Sequence {
    public func makeIterator() -> NSFastEnumerationIterator { NSFastEnumerationIterator(self) }
}
