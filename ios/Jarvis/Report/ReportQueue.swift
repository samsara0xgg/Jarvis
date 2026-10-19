import Foundation
import SQLite3

/// The frames waiting for the brain, in the app's own sandbox (Application Support, excluded from backups).
/// One SQLite table keyed by `event_uid`, so appending the same observation twice stores it once.
/// Past `cap` frames the oldest are dropped: a phone that cannot reach the brain for days keeps the recent past. A day of Watch heart rate alone
/// is a few thousand frames (ADR 0293), so the cap holds several days of everything.
final class ReportQueue: @unchecked Sendable {
    static let defaultCap = 50_000

    /// The queue the app uses.
    static let standard: ReportQueue = {
        let dir = URL.applicationSupportDirectory.appending(path: "Report", directoryHint: .isDirectory)
        return ReportQueue(url: dir.appending(path: "queue.sqlite"))
    }()

    private var db: OpaquePointer?
    private let lock = NSLock()
    private let cap: Int

    init(url: URL, cap: Int = ReportQueue.defaultCap) {
        self.cap = cap
        let dir = url.deletingLastPathComponent()
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        var values = URLResourceValues()
        values.isExcludedFromBackup = true
        var dirURL = dir
        try? dirURL.setResourceValues(values)
        sqlite3_open_v2(url.path, &db, SQLITE_OPEN_CREATE | SQLITE_OPEN_READWRITE | SQLITE_OPEN_FULLMUTEX, nil)
        exec("CREATE TABLE IF NOT EXISTS frames (seq INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT NOT NULL UNIQUE, json BLOB NOT NULL, sense TEXT NOT NULL DEFAULT '')")
    }

    deinit { sqlite3_close(db) }

    var count: Int {
        locked {
            var n = 0
            statement("SELECT COUNT(*) FROM frames") { _ in } row: { n = Int(sqlite3_column_int64($0, 0)) }
            return n
        }
    }

    /// Adds frames (a uid already queued is kept as it is), then trims to the cap. `sense` names the Settings
    /// switch they came from, so turning it off can drop what it had queued.
    func append(_ frames: [ReportFrame], sense: String = "") {
        guard !frames.isEmpty else { return }
        locked {
            exec("BEGIN")
            for f in frames {
                statement("INSERT OR IGNORE INTO frames (uid, json, sense) VALUES (?, ?, ?)") { st in
                    sqlite3_bind_text(st, 1, f.eventUid, -1, Self.transient)
                    f.json.withUnsafeBytes { _ = sqlite3_bind_blob(st, 2, $0.baseAddress, Int32($0.count), Self.transient) }
                    sqlite3_bind_text(st, 3, sense, -1, Self.transient)
                } row: { _ in }
            }
            statement("DELETE FROM frames WHERE seq <= (SELECT seq FROM frames ORDER BY seq DESC LIMIT 1 OFFSET ?)") {
                sqlite3_bind_int64($0, 1, Int64(self.cap))
            } row: { _ in }
            exec("COMMIT")
        }
    }

    /// The oldest frames that fit one batch.
    func head(maxFrames: Int = ReportBatch.maxFrames, maxBytes: Int = ReportBatch.maxBytes) -> [ReportFrame] {
        let rows: [ReportFrame] = locked {
            var out: [ReportFrame] = []
            statement("SELECT uid, json FROM frames ORDER BY seq LIMIT ?") {
                sqlite3_bind_int64($0, 1, Int64(maxFrames))
            } row: { st in
                guard let uid = sqlite3_column_text(st, 0), let blob = sqlite3_column_blob(st, 1) else { return }
                out.append(ReportFrame(eventUid: String(cString: uid), json: Data(bytes: blob, count: Int(sqlite3_column_bytes(st, 1)))))
            }
            return out
        }
        return ReportBatch.prefix(rows, maxFrames: maxFrames, maxBytes: maxBytes)
    }

    func remove(_ uids: [String]) {
        guard !uids.isEmpty else { return }
        locked {
            exec("BEGIN")
            for uid in uids {
                statement("DELETE FROM frames WHERE uid = ?") { sqlite3_bind_text($0, 1, uid, -1, Self.transient) } row: { _ in }
            }
            exec("COMMIT")
        }
    }

    func remove(sense: String) {
        locked { statement("DELETE FROM frames WHERE sense = ?") { sqlite3_bind_text($0, 1, sense, -1, Self.transient) } row: { _ in } }
    }

    func removeAll() { locked { exec("DELETE FROM frames") } }

    // MARK: SQLite plumbing

    private static let transient = unsafeBitCast(-1, to: sqlite3_destructor_type.self)

    private func locked<T>(_ body: () -> T) -> T {
        lock.lock()
        defer { lock.unlock() }
        return body()
    }

    private func exec(_ sql: String) { sqlite3_exec(db, sql, nil, nil, nil) }

    private func statement(_ sql: String, bind: (OpaquePointer?) -> Void, row: (OpaquePointer?) -> Void) {
        var st: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &st, nil) == SQLITE_OK else { return }
        defer { sqlite3_finalize(st) }
        bind(st)
        while sqlite3_step(st) == SQLITE_ROW { row(st) }
    }
}
