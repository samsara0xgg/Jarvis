import Compression
import CoreMotion
import Foundation

/// The phone's device motion while he rides (ADR 0312): 25 samples a second as text, cut into pieces of at most ten minutes, gzipped
/// into the lifelog outbox and sent by `LifelogUploader` to the brain's raw archive (`PUT /inherent/lifelog/motion/{id}`). Nothing here
/// judges braking or turning; a reader computes that from the raw values.
///
/// A piece is written as it goes to `open-motion/<id>.csv` (two seconds of rows at a time) beside `<id>.json`; closing it compresses it
/// to `<id>.csv.gz` with its `LifelogPiece` in the outbox. What a killed app left open is closed the same way at the next start.
/// Everything that touches the files runs on `queue`.
final class DriveMotion: @unchecked Sendable {
    static let shared = DriveMotion()

    static let rateHz = 25.0
    static let pieceMs: Int64 = 10 * 60 * 1000
    /// Rows written to the open file at a time.
    static let flushRows = 50
    static let columns = "t_ms,ax,ay,az,gx,gy,gz,rx,ry,rz,qw,qx,qy,qz,heading_deg"

    static var openDir: URL { LifelogStore.outbox.appendingPathComponent("open-motion", isDirectory: true) }

    private let manager = CMMotionManager()
    private let queue: OperationQueue = {
        let q = OperationQueue()
        q.maxConcurrentOperationCount = 1
        q.qualityOfService = .utility
        q.name = "jarvis.drive-motion"
        return q
    }()
    /// Set on the main actor only.
    private var running = false

    // on `queue`
    private var piece: LifelogPiece?
    private var file: FileHandle?
    private var rows = ""
    private var pending = 0
    private var lastMs: Int64 = 0
    /// wall-clock seconds of the boot, which CMLogItem timestamps count from
    private var bootEpoch: TimeInterval = 0

    /// Starts recording; does nothing when it already runs or the phone has no device motion.
    @MainActor func start() {
        guard !running, manager.isDeviceMotionAvailable else { return }
        running = true
        let frame = Self.frame()
        manager.deviceMotionUpdateInterval = 1 / Self.rateHz
        manager.startDeviceMotionUpdates(using: frame.reference, to: queue) { [weak self] m, _ in
            guard let self, let m else { return }
            self.take(m, frame: frame.name)
        }
        StallLog.append("drive motion started, frame \(frame.name)", to: "location.log")
    }

    /// Stops recording and closes the piece.
    @MainActor func stop() {
        guard running else { return }
        running = false
        manager.stopDeviceMotionUpdates()
        queue.addOperation { [self] in close() }
        StallLog.append("drive motion stopped", to: "location.log")
    }

    /// At launch: pieces a killed app left open are closed.
    func recover() {
        queue.addOperation {
            let fm = FileManager.default, dir = Self.openDir
            for name in (try? fm.contentsOfDirectory(atPath: dir.path)) ?? [] where name.hasSuffix(".csv") {
                let id = String(name.dropLast(4))
                let meta = dir.appendingPathComponent("\(id).json")
                guard var p = (try? Data(contentsOf: meta)).flatMap({ try? JSONDecoder().decode(LifelogPiece.self, from: $0) }) else {
                    let held = LifelogStore.ensure(LifelogStore.heldDir("broken"))
                    try? fm.moveItem(at: dir.appendingPathComponent(name), to: held.appendingPathComponent(name))
                    continue
                }
                p.durationMs = Self.recoveredDuration(dir.appendingPathComponent(name))
                Self.finish(p)
            }
        }
    }

    /// The best frame the phone has: magnetic north (and a heading) when it can, else an arbitrary but drift-corrected yaw.
    private static func frame() -> (reference: CMAttitudeReferenceFrame, name: String) {
        CMMotionManager.availableAttitudeReferenceFrames().contains(.xMagneticNorthZVertical)
            ? (.xMagneticNorthZVertical, "x_magnetic_north_z_vertical") : (.xArbitraryCorrectedZVertical, "x_arbitrary_corrected_z_vertical")
    }

    // MARK: on `queue`

    private func take(_ m: CMDeviceMotion, frame: String) {
        if let p = piece, ms(m) - p.startedMs >= Self.pieceMs { close() }
        if piece == nil {
            bootEpoch = Self.boot()  // read again for each piece, so a clock set meanwhile is followed
            open(at: ms(m), frame: frame)
        }
        guard let p = piece else { return }
        let t = max(ms(m) - p.startedMs, lastMs)
        lastMs = t
        rows += Self.row(t: t, m)
        pending += 1
        if pending >= Self.flushRows { write() }
    }

    /// The UTC millisecond of a sample.
    private func ms(_ m: CMDeviceMotion) -> Int64 { Int64(((bootEpoch + m.timestamp) * 1000).rounded()) }

    private static func boot() -> TimeInterval { Date().timeIntervalSince1970 - ProcessInfo.processInfo.systemUptime }

    /// One sample as a line of `columns`: acceleration without gravity and gravity in g, rotation in rad/s, the attitude quaternion,
    /// and the heading when the frame has one; all in the phone's own axes, as Core Motion gave them.
    static func row(t: Int64, _ m: CMDeviceMotion) -> String {
        let a = m.userAcceleration, g = m.gravity, r = m.rotationRate, q = m.attitude.quaternion
        return row(t: t, values: [a.x, a.y, a.z, g.x, g.y, g.z, r.x, r.y, r.z], quaternion: [q.w, q.x, q.y, q.z], heading: m.heading)
    }

    static func row(t: Int64, values: [Double], quaternion: [Double], heading: Double) -> String {
        var line = String(t)
        for v in values { line += "," + String(format: "%.4f", v) }
        for v in quaternion { line += "," + String(format: "%.5f", v) }
        line += "," + (heading >= 0 ? String(format: "%.1f", heading) : "") + "\n"
        return line
    }

    private func open(at ms: Int64, frame: String) {
        let dir = LifelogStore.ensure(Self.openDir)
        let p = LifelogPiece(id: UUID().uuidString, startedMs: ms, durationMs: 0, ext: "csv.gz", kind: "motion")
        let url = dir.appendingPathComponent("\(p.id).csv")
        let head = "# jarvis motion v1 frame=\(frame) rate_hz=\(Int(Self.rateHz))\n\(Self.columns)\n"
        guard FileManager.default.createFile(atPath: url.path, contents: Data(head.utf8),
                                             attributes: [.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication]),
              let h = try? FileHandle(forWritingTo: url) else { return }
        _ = try? h.seekToEnd()
        try? JSONEncoder().encode(p).write(to: dir.appendingPathComponent("\(p.id).json"), options: .atomic)
        piece = p
        file = h
        rows = ""
        pending = 0
        lastMs = 0
    }

    private func write() {
        guard let file, !rows.isEmpty else { return }
        try? file.write(contentsOf: Data(rows.utf8))
        rows = ""
        pending = 0
    }

    private func close() {
        write()
        try? file?.close()
        file = nil
        guard var p = piece else { return }
        piece = nil
        p.durationMs = lastMs + Int64(1000 / Self.rateHz)
        Self.finish(p)
    }

    /// Compresses the open file into the outbox with its record, and lets the uploader know; a file that cannot be read is held.
    private static func finish(_ p: LifelogPiece) {
        let fm = FileManager.default
        let csv = openDir.appendingPathComponent("\(p.id).csv"), meta = openDir.appendingPathComponent("\(p.id).json")
        defer { try? fm.removeItem(at: meta) }
        guard let text = try? Data(contentsOf: csv), let gz = Gzip.compress(text), p.durationMs > 0 else {
            let held = LifelogStore.ensure(LifelogStore.heldDir("broken"))
            try? fm.moveItem(at: csv, to: held.appendingPathComponent(csv.lastPathComponent))
            return
        }
        LifelogStore.ensure()
        guard (try? gz.write(to: p.file, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])) != nil,
              (try? p.save()) != nil else { return }
        try? fm.removeItem(at: csv)
        StallLog.append("drive motion piece \(p.id.prefix(8)) closed, \(p.durationMs / 1000) s, \(gz.count) bytes", to: "location.log")
        Task { @MainActor in LifelogUploader.shared.kick() }
    }

    /// The length of a piece a killed app left: its last whole row's time plus one sample. A row cut off mid-write is dropped.
    static func recoveredDuration(_ url: URL) -> Int64 {
        guard var text = (try? Data(contentsOf: url)).flatMap({ String(data: $0, encoding: .utf8) }) else { return 0 }
        if !text.hasSuffix("\n"), let cut = text.lastIndex(of: "\n") {
            text = String(text[...cut])
            try? Data(text.utf8).write(to: url)
        }
        let rows = text.split(separator: "\n").filter { !$0.hasPrefix("#") && !$0.hasPrefix("t_ms") }
        guard let last = rows.last, let t = Int64(last.prefix { $0 != "," }) else { return 0 }
        return t + Int64(1000 / rateHz)
    }
}

/// gzip (RFC 1952) around the raw DEFLATE stream of the Compression framework, so any tool reads a piece (`gunzip`, Python's `gzip`).
enum Gzip {
    static func compress(_ data: Data) -> Data? {
        guard let deflated = try? (data as NSData).compressed(using: .zlib) as Data else { return nil }
        var out = Data([0x1f, 0x8b, 0x08, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x03])  // deflate, no name, no time, Unix
        out.append(deflated)
        out.append(contentsOf: le(crc32(data)))
        out.append(contentsOf: le(UInt32(truncatingIfNeeded: data.count)))
        return out
    }

    private static func le(_ v: UInt32) -> [UInt8] { [UInt8(v & 0xff), UInt8(v >> 8 & 0xff), UInt8(v >> 16 & 0xff), UInt8(v >> 24 & 0xff)] }

    private static let table: [UInt32] = (0..<256).map { n in
        var c = UInt32(n)
        for _ in 0..<8 { c = c & 1 == 1 ? 0xEDB8_8320 ^ (c >> 1) : c >> 1 }
        return c
    }

    static func crc32(_ data: Data) -> UInt32 {
        var c: UInt32 = 0xFFFF_FFFF
        for b in data { c = table[Int((c ^ UInt32(b)) & 0xff)] ^ (c >> 8) }
        return c ^ 0xFFFF_FFFF
    }
}
