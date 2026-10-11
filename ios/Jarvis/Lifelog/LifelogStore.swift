import Foundation

// His recorded day (ADR 0238): what the recorder (LifelogRecorder), the uploader (LifelogUploader) and the switches (control, Live Activity,
// settings) share. The switch's state lives in the App Group's defaults, so the control in the widget extension reads it too.
//
// The outbox, Application Support/lifelog-outbox/ (kept out of backups, readable after the first unlock):
//   open/<id>.<ext>         the piece being written now; one at most. Left over at launch: closed if it reads, else moved to held/broken/
//   <id>.<ext> + <id>.json  closed pieces waiting for the brain (`LifelogPiece`); deleted only when the brain's sha256 matches
//   held/<why>/…            pieces never sent again and never deleted: conflict (409), rejected (400/411/413/415/422), broken

enum LifelogStore {
    static let groupDefaults = UserDefaults(suiteName: "group.com.yilunshi.jarvis") ?? .standard
    private static let onKey = "lifelog.on"

    /// The switch: on until he turns it off. An interruption only pauses the recorder; it starts again when the interruption ends.
    static var isOn: Bool {
        get { groupDefaults.bool(forKey: onKey) }
        set { groupDefaults.set(newValue, forKey: onKey) }
    }

    static let outbox: URL = {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        return base.appendingPathComponent("lifelog-outbox", isDirectory: true)
    }()
    static var openDir: URL { outbox.appendingPathComponent("open", isDirectory: true) }
    static func heldDir(_ why: String) -> URL { outbox.appendingPathComponent("held/\(why)", isDirectory: true) }

    /// Makes the outbox (and the directory asked for) if missing: out of iCloud and device backups, protected until the first unlock
    /// (a locked phone still records and uploads).
    @discardableResult static func ensure(_ dir: URL = outbox) -> URL {
        let fm = FileManager.default
        try? fm.createDirectory(at: dir, withIntermediateDirectories: true,
                                attributes: [.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication])
        var root = outbox
        var v = URLResourceValues(); v.isExcludedFromBackup = true
        try? root.setResourceValues(v)
        return dir
    }

    /// Closed pieces waiting for the brain, oldest first.
    static func waiting() -> [LifelogPiece] {
        let fm = FileManager.default
        let names = (try? fm.contentsOfDirectory(atPath: outbox.path)) ?? []
        return names.filter { $0.hasSuffix(".json") }
            .compactMap { try? JSONDecoder().decode(LifelogPiece.self, from: Data(contentsOf: outbox.appendingPathComponent($0))) }
            .sorted { $0.startedMs < $1.startedMs }
    }

    // MARK: today's total, for the Live Activity and the settings line

    private static let dayKey = "lifelog.day", dayMsKey = "lifelog.dayMs"

    /// milliseconds recorded in closed pieces since local midnight (a piece is counted on the day it was closed)
    static var closedTodayMs: Int64 {
        groupDefaults.string(forKey: dayKey) == today() ? Int64(groupDefaults.integer(forKey: dayMsKey)) : 0
    }

    static func addClosed(ms: Int64) {
        let d = today()
        let base = groupDefaults.string(forKey: dayKey) == d ? Int64(groupDefaults.integer(forKey: dayMsKey)) : 0
        groupDefaults.set(d, forKey: dayKey)
        groupDefaults.set(Int(base + ms), forKey: dayMsKey)
    }

    private static func today() -> String {
        let c = Calendar.current.dateComponents([.year, .month, .day], from: Date())
        return "\(c.year!)-\(c.month!)-\(c.day!)"
    }
}

/// One closed piece, as `<id>.json` beside its audio. The query of the PUT is `startedMs` and `durationMs`.
struct LifelogPiece: Codable, Equatable {
    /// a UUID string: 36 of A-Za-z0-9_- (the brain takes 1 to 64)
    var id: String
    /// UTC milliseconds of the first sample
    var startedMs: Int64
    /// from the samples written, not the clock
    var durationMs: Int64
    /// `ogg` (Opus) or `m4a` (HE-AAC, the fallback)
    var ext: String
    /// sends tried so far, and not before this (UTC milliseconds); 0 means now
    var attempts: Int = 0
    var nextTryMs: Int64 = 0

    var audio: URL { LifelogStore.outbox.appendingPathComponent("\(id).\(ext)") }
    var sidecar: URL { LifelogStore.outbox.appendingPathComponent("\(id).json") }

    func save() throws { try JSONEncoder().encode(self).write(to: sidecar, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication]) }
}
