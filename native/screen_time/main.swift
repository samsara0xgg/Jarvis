// jarvis-screen-time: iPhone/iPad app focus events that macOS syncs into Biome, written as one JSON object.
// Usage: jarvis-screen-time <outfile> [--since-ms N] [--root <biome dir>]
// Must run as the .app bundle (see Info.plist): Full Disk Access is granted to the app, not to Python.
//
// Reads <biome>/streams/restricted/App.InFocus/remote/<device id>/<segment> (SEGB files, v1 or v2,
// each record one protobuf AppInFocus event) and <biome>/sync/sync.db for device names.
// Record format: docs/adr/0304-*.md. Remote devices only; this Mac's own "local" stream is not read.
import Foundation
import SQLite3

let args = Array(CommandLine.arguments.dropFirst())
let outPath = args.first.flatMap { $0.hasPrefix("--") ? nil : $0 } ?? "/dev/stdout"
func option(_ name: String) -> String? {
    guard let i = args.firstIndex(of: name), i + 1 < args.count else { return nil }
    return args[i + 1]
}
let sinceMs = option("--since-ms").flatMap { Int64($0) } ?? 0
let biome = option("--root") ?? NSHomeDirectory() + "/Library/Biome"

func emit(_ object: [String: Any]) -> Never {
    let data = (try? JSONSerialization.data(withJSONObject: object)) ?? Data("{\"error\":\"encode\"}".utf8)
    try? (String(decoding: data, as: UTF8.self) + "\n").write(toFile: outPath, atomically: true, encoding: .utf8)
    exit(0)
}

// MARK: little-endian reads (bounds-checked: a torn or foreign file yields nil, never a crash)

func u32(_ b: [UInt8], _ o: Int) -> UInt32? {
    guard o >= 0, o + 4 <= b.count else { return nil }
    return UInt32(b[o]) | UInt32(b[o + 1]) << 8 | UInt32(b[o + 2]) << 16 | UInt32(b[o + 3]) << 24
}
func i32(_ b: [UInt8], _ o: Int) -> Int? { u32(b, o).map { Int(Int32(bitPattern: $0)) } }
func f64(_ b: [UInt8], _ o: Int) -> Double? {
    guard o >= 0, o + 8 <= b.count else { return nil }
    var bits: UInt64 = 0
    for k in 0..<8 { bits |= UInt64(b[o + k]) << UInt64(8 * k) }
    return Double(bitPattern: bits)
}

// MARK: SEGB: returns the data of every record in state 1 (written); 3 = deleted (zeroed), 4 = empty

func segbRecords(_ b: [UInt8]) -> [[UInt8]] {
    var out: [[UInt8]] = []
    if b.count >= 56, Array(b[52..<56]) == Array("SEGB".utf8) {
        // v1: 56-byte header (end of data offset first, magic last); records are a 32-byte header
        // (length, state, two timestamps, crc, unknown) + data, padded to 8 bytes.
        let end = Int(u32(b, 0) ?? 0)
        var pos = 56
        while pos < end, let len = i32(b, pos), let state = i32(b, pos + 4), len >= 0, pos + 32 + len <= b.count {
            if state == 1 { out.append(Array(b[(pos + 32)..<(pos + 32 + len)])) }
            pos += 32 + len
            if pos % 8 != 0 { pos += 8 - pos % 8 }
        }
    } else if b.count >= 32, Array(b[0..<4]) == Array("SEGB".utf8) {
        // v2: 32-byte header (magic, entry count, creation time) then records (4-byte crc, 4 unknown, data),
        // then a trailer of 16-byte entries (end offset relative to the data area, state, time).
        // ponytail: the stored CRC32 is not checked; the strict protobuf parse rejects garbage instead.
        guard let n = i32(b, 4), n >= 0, n * 16 <= b.count - 32 else { return out }
        let trailer = b.count - n * 16
        var entries: [(end: Int, state: Int, idx: Int)] = []
        for k in 0..<n {
            if let e = i32(b, trailer + 16 * k), let s = i32(b, trailer + 16 * k + 4), [1, 3, 4].contains(s) {
                entries.append((e, s, k))
            }
        }
        entries.sort { ($0.end, $0.idx) < ($1.end, $1.idx) }
        var pos = 32
        var prev: (end: Int, data: [UInt8])?
        for e in entries {
            if e.state == 4 { continue }
            if let p = prev, p.end == e.end {  // written, then marked deleted: same data region
                if e.state == 1 { out.append(p.data) }
                continue
            }
            let len = e.end - (pos - 32)
            guard len >= 8, pos + len <= trailer else { continue }
            let data = Array(b[(pos + 8)..<(pos + len)])
            if e.state == 1 { out.append(data) }
            prev = (e.end, data)
            pos += len
            if e.end % 4 != 0 { pos += 4 - e.end % 4 }
        }
    }
    return out
}

// MARK: protobuf AppInFocus: field 3 varint (1 = gained focus), 4 double (CFAbsoluteTime), 6 string (bundle id)

struct Focus { let bundle: String; let gained: Bool; let atMs: Int64 }

func parseFocus(_ d: [UInt8]) -> Focus? {
    var i = 0
    func varint() -> UInt64? {
        var v: UInt64 = 0, shift: UInt64 = 0
        while i < d.count, shift < 64 {
            let byte = d[i]; i += 1
            v |= UInt64(byte & 0x7F) << shift
            if byte < 0x80 { return v }
            shift += 7
        }
        return nil
    }
    var gained: UInt64?, time: Double?, bundle: String?
    while i < d.count {
        guard let key = varint() else { return nil }
        switch key & 7 {
        case 0:
            guard let v = varint() else { return nil }
            if key >> 3 == 3 { gained = v }
        case 1:
            guard let v = f64(d, i) else { return nil }
            i += 8
            if key >> 3 == 4 { time = v }
        case 2:
            guard let len = varint(), len <= UInt64(d.count - i) else { return nil }
            if key >> 3 == 6 { bundle = String(bytes: d[i..<(i + Int(len))], encoding: .utf8) }
            i += Int(len)
        case 5:
            i += 4
            if i > d.count { return nil }
        default:
            return nil
        }
    }
    guard let t = time, t.isFinite, let b = bundle, !b.isEmpty else { return nil }
    // CFAbsoluteTime counts seconds from 2001-01-01; unix epoch is 978307200 s earlier.
    return Focus(bundle: b, gained: gained == 1, atMs: Int64(((t + 978_307_200) * 1000).rounded()))
}

// MARK: sqlite (read-only, immutable: never touches Apple's -wal/-shm files)

func rows(_ path: String, _ sql: String) -> [[String?]] {
    var db: OpaquePointer?
    let uri = URL(fileURLWithPath: path).absoluteString + "?mode=ro&immutable=1"
    guard sqlite3_open_v2(uri, &db, SQLITE_OPEN_READONLY | SQLITE_OPEN_URI, nil) == SQLITE_OK else {
        sqlite3_close(db); return []
    }
    defer { sqlite3_close(db) }
    var stmt: OpaquePointer?
    guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { return [] }
    defer { sqlite3_finalize(stmt) }
    var out: [[String?]] = []
    while sqlite3_step(stmt) == SQLITE_ROW {
        out.append((0..<sqlite3_column_count(stmt)).map { c in
            sqlite3_column_text(stmt, c).map { String(cString: $0) }
        })
    }
    return out
}

// MARK: main

let remote = biome + "/streams/restricted/App.InFocus/remote"
guard let dir = opendir(remote) else {
    emit(["error": errno == ENOENT ? "no_remote_streams" : (errno == EPERM || errno == EACCES ? "no_access" : "open_failed")])
}
var ids: [String] = []
while let entry = readdir(dir) {
    let name = withUnsafePointer(to: entry.pointee.d_name) {
        $0.withMemoryRebound(to: CChar.self, capacity: Int(entry.pointee.d_namlen) + 1) { String(cString: $0) }
    }
    if !name.hasPrefix(".") { ids.append(name) }
}
closedir(dir)
ids.sort()

// Device names (and, when readable, the hardware model) are a bonus: any failure leaves them null.
var names: [String: (name: String?, ids: String?)] = [:]
for r in rows(biome + "/sync/sync.db", "SELECT device_identifier, name, ids_device_identifier FROM DevicePeer") {
    if let id = r[0] { names[id] = (r[1], r[2]) }
}
var models: [String: String] = [:]
for r in rows(biome + "/../Application Support/Knowledge/knowledgeC.db",
              "SELECT ZRAPPORTID, ZMODEL FROM ZSYNCPEER WHERE ZRAPPORTID IS NOT NULL AND ZMODEL IS NOT NULL") {
    if let k = r[0], let m = r[1] { models[k] = m }
}

var devices: [[String: Any]] = []
var events: [[String: Any]] = []
for id in ids {
    let info = names[id]
    var device: [String: Any] = ["id": id, "name": (info?.name).flatMap { $0.isEmpty ? nil : $0 } ?? NSNull()]
    if let model = info?.ids.flatMap({ models[$0] }) { device["model"] = model }
    devices.append(device)
    guard let files = try? FileManager.default.contentsOfDirectory(atPath: remote + "/" + id) else { continue }
    for file in files where !file.hasPrefix(".") {
        let data: Data
        do { data = try Data(contentsOf: URL(fileURLWithPath: remote + "/" + id + "/" + file)) } catch {
            if (error as NSError).code == NSFileReadNoPermissionError { emit(["error": "no_access"]) }
            continue
        }
        for record in segbRecords([UInt8](data)) {
            guard let f = parseFocus(record), f.atMs >= sinceMs else { continue }
            events.append(["device": id, "bundle_id": f.bundle, "focus": f.gained ? "in" : "out", "at_ms": f.atMs])
        }
    }
}
events.sort { ($0["at_ms"] as! Int64) < ($1["at_ms"] as! Int64) }
emit(["devices": devices, "events": events])
