// jarvis-screen-time: iPhone/iPad app focus events that macOS syncs into Biome, written as one JSON object.
// Usage: jarvis-screen-time <outfile> [--since-ms N] [--root <biome dir>] [--streams a,b,c]
// --streams adds a "streams" object with typed records of the other synced streams: now_playing
// (Media.NowPlaying), bluetooth (Device.Wireless.Bluetooth) and interactions (the four
// Siri.Remembers.* streams), and of this Mac's own databases: photos (Photos.sqlite), messages
// (Messages/chat.db) and safari (Safari/History.db); without it the output is only devices and events.
// --local-since-ms N limits those three to rows touched (made, changed, read) at or after N.
// --home <dir> replaces the home folder those three databases are looked for in.
// Must run as the .app bundle (see Info.plist): Full Disk Access is granted to the app, not to Python.
//
// Reads <biome>/streams/restricted/App.InFocus/remote/<device id>/<segment> (SEGB files, v1 or v2,
// each record one protobuf AppInFocus event) and <biome>/sync/sync.db for device names.
// Record format: docs/adr/0304-*.md, 0305-*.md and 0309-*.md. Remote devices only; this Mac's own "local" stream
// is not read, nor are the tombstone folders (logs of deletions, not payloads).
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
let wantedStreams = Set((option("--streams") ?? "").split(separator: ",").map(String.init))
let localSinceMs = option("--local-since-ms").flatMap { Int64($0) }
let home = option("--home") ?? NSHomeDirectory()

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

// MARK: SEGB: returns the data and the entry time (CFAbsoluteTime) of every record in state 1
// (written); 3 = deleted (zeroed), 4 = empty

typealias Segment = (data: [UInt8], time: Double?)

func segbRecords(_ b: [UInt8]) -> [Segment] {
    var out: [Segment] = []
    if b.count >= 56, Array(b[52..<56]) == Array("SEGB".utf8) {
        // v1: 56-byte header (end of data offset first, magic last); records are a 32-byte header
        // (length, state, two timestamps, crc, unknown) + data, padded to 8 bytes.
        let end = Int(u32(b, 0) ?? 0)
        var pos = 56
        while pos < end, let len = i32(b, pos), let state = i32(b, pos + 4), len >= 0, pos + 32 + len <= b.count {
            if state == 1 { out.append((Array(b[(pos + 32)..<(pos + 32 + len)]), f64(b, pos + 8))) }
            pos += 32 + len
            if pos % 8 != 0 { pos += 8 - pos % 8 }
        }
    } else if b.count >= 32, Array(b[0..<4]) == Array("SEGB".utf8) {
        // v2: 32-byte header (magic, entry count, creation time) then records (4-byte crc, 4 unknown, data),
        // then a trailer of 16-byte entries (end offset relative to the data area, state, time).
        // ponytail: the stored CRC32 is not checked; the strict protobuf parse rejects garbage instead.
        guard let n = i32(b, 4), n >= 0, n * 16 <= b.count - 32 else { return out }
        let trailer = b.count - n * 16
        var entries: [(end: Int, state: Int, idx: Int, time: Double?)] = []
        for k in 0..<n {
            if let e = i32(b, trailer + 16 * k), let s = i32(b, trailer + 16 * k + 4), [1, 3, 4].contains(s) {
                entries.append((e, s, k, f64(b, trailer + 16 * k + 8)))
            }
        }
        entries.sort { ($0.end, $0.idx) < ($1.end, $1.idx) }
        var pos = 32
        var prev: (end: Int, data: [UInt8])?
        for e in entries {
            if e.state == 4 { continue }
            if let p = prev, p.end == e.end {  // written, then marked deleted: same data region
                if e.state == 1 { out.append((p.data, e.time)) }
                continue
            }
            let len = e.end - (pos - 32)
            guard len >= 8, pos + len <= trailer else { continue }
            let data = Array(b[(pos + 8)..<(pos + len)])
            if e.state == 1 { out.append((data, e.time)) }
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

// MARK: generic protobuf (the other streams are read by field number; length-delimited fields stay
// bytes until the caller says whether they are a string or a message)

enum PBValue { case varint(UInt64), double(Double), bytes([UInt8]) }
struct PBField { let num: Int; let value: PBValue }

func pbFields(_ d: [UInt8]) -> [PBField]? {
    var i = 0
    var out: [PBField] = []
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
    while i < d.count {
        guard let key = varint() else { return nil }
        let num = Int(key >> 3)
        guard num >= 1, num < 2000 else { return nil }
        switch key & 7 {
        case 0:
            guard let v = varint() else { return nil }
            out.append(PBField(num: num, value: .varint(v)))
        case 1:
            guard let v = f64(d, i) else { return nil }
            i += 8
            out.append(PBField(num: num, value: .double(v)))
        case 2:
            guard let len = varint(), len <= UInt64(d.count - i) else { return nil }
            out.append(PBField(num: num, value: .bytes(Array(d[i..<(i + Int(len))]))))
            i += Int(len)
        case 5:
            guard let v = u32(d, i) else { return nil }
            i += 4
            out.append(PBField(num: num, value: .varint(UInt64(v))))
        default:
            return nil
        }
    }
    return out
}

extension Array where Element == PBField {
    func varint(_ n: Int) -> UInt64? {
        for f in self where f.num == n { if case .varint(let v) = f.value { return v } }
        return nil
    }
    func double(_ n: Int) -> Double? {
        for f in self where f.num == n { if case .double(let v) = f.value { return v } }
        return nil
    }
    func string(_ n: Int) -> String? {
        for f in self where f.num == n { if case .bytes(let b) = f.value { return String(bytes: b, encoding: .utf8) } }
        return nil
    }
    func message(_ n: Int) -> [PBField]? {
        for f in self where f.num == n { if case .bytes(let b) = f.value { return pbFields(b) } }
        return nil
    }
    func messages(_ n: Int) -> [[PBField]] {
        compactMap { f in
            if f.num == n, case .bytes(let b) = f.value { return pbFields(b) }
            return nil
        }
    }
}

func unixMs(cfSeconds t: Double?) -> Int64? {
    guard let t = t, t.isFinite, t > 0 else { return nil }
    return Int64(((t + 978_307_200) * 1000).rounded())  // CFAbsoluteTime counts from 2001-01-01
}

/// A string field, left out when empty.
func text(_ m: [PBField], _ n: Int) -> String? {
    m.string(n).flatMap { $0.isEmpty ? nil : $0 }
}

// MARK: Media.NowPlaying: 2 time (CFAbsoluteTime), 3 playback state, 4 title, 5 artist, 6 duration s
// (4294967295 = unknown), 8 album / post text / lyric line, 10 media type, 14 route {1, 2, 3 name},
// 15 bundle id

func parseNowPlaying(_ device: String, _ seg: Segment) -> [String: Any]? {
    guard let m = pbFields(seg.data), let at = unixMs(cfSeconds: m.double(2)), let state = m.varint(3) else { return nil }
    var out: [String: Any] = ["device": device, "at_ms": at, "state": Int(state)]
    if let v = text(m, 4) { out["title"] = v }
    if let v = text(m, 5) { out["artist"] = v }
    if let v = m.varint(6), v != 4_294_967_295 { out["duration_s"] = Int(v) }
    if let v = text(m, 8) { out["text"] = v }
    if let v = text(m, 10) { out["media_type"] = v }
    if let r = m.message(14) {
        if let v = text(r, 3) { out["route"] = v }
        out["route_codes"] = [r.varint(1) ?? 0, r.varint(2) ?? 0].map { Int($0) }
    }
    if let v = text(m, 15) { out["bundle_id"] = v }
    return out
}

// MARK: Device.Wireless.Bluetooth: no time in the payload, so the SEGB entry time stands; 1 address,
// 2 name, 3 product id, 4 connected, 6 7 8 battery (in field order), 11 vendor id; 5 9 10 constants

func parseBluetooth(_ device: String, _ seg: Segment) -> [String: Any]? {
    guard let m = pbFields(seg.data), let at = unixMs(cfSeconds: seg.time), let address = text(m, 1) else { return nil }
    var out: [String: Any] = ["device": device, "at_ms": at, "address": address, "connected": m.varint(4) == 1]
    if let v = text(m, 2) { out["name"] = v }
    if let v = m.varint(3) { out["product_id"] = Int(v) }
    if let v = m.varint(11) { out["vendor_id"] = Int(v) }
    out["battery"] = [6, 7, 8].compactMap { m.varint($0) }.map { Int($0) }
    var extra: [String: Int] = [:]
    for n in [5, 9, 10] { if let v = m.varint(n) { extra[String(n)] = Int(v) } }
    if !extra.isEmpty { out["extra"] = extra }
    return out
}

// MARK: Siri.Remembers.*: 1 = {1 uuid, 2 intent class, 3 domain, 4 bundle id, 5 origin, 6 direction
// (1 sent, 2 received), 8 UNIX seconds, 9 call duration s, 11 JSON {"isGroup"}, 12 conversation id},
// 2 repeated = {1 role, 2 = {1 value, 2 type, 4 JSON}}; no message text exists in these streams

let siriStreams = [
    "Siri.Remembers.MessageHistory": "message", "Siri.Remembers.CallHistory": "call",
    "Siri.Remembers.InteractionHistory": "interaction", "Siri.Remembers.AudioHistory": "audio",
]

func parseSiri(_ device: String, _ source: String, _ seg: Segment) -> [String: Any]? {
    guard let m = pbFields(seg.data), let top = m.message(1), let uuid = text(top, 1),
          let t = top.double(8), t.isFinite, t > 1_000_000_000, t < 4_102_444_800 else { return nil }
    var out: [String: Any] = ["device": device, "source": source, "uuid": uuid, "at_ms": Int64((t * 1000).rounded())]
    if let v = text(top, 2) { out["intent"] = v }
    if let v = text(top, 3) { out["domain"] = v }
    if let v = text(top, 4) { out["bundle_id"] = v }
    if let v = top.varint(5) { out["origin"] = Int(v) }
    if let v = top.varint(6) { out["direction"] = Int(v) }
    if let v = top.double(9), v.isFinite, v > 0 { out["duration_s"] = Int(v.rounded()) }
    if let j = text(top, 11), let o = (try? JSONSerialization.jsonObject(with: Data(j.utf8))) as? [String: Any],
       let g = o["isGroup"] as? Bool { out["is_group"] = g }
    if let v = text(top, 12) { out["conversation_id"] = v }
    var entries: [[String: Any]] = []
    for e in m.messages(2) {
        guard let role = text(e, 1), let v = e.message(2) else { continue }
        var one: [String: Any] = ["role": role]
        if let x = text(v, 1) { one["value"] = x }
        if let x = text(v, 2) { one["type"] = x }
        if let j = text(v, 4), let o = (try? JSONSerialization.jsonObject(with: Data(j.utf8))) as? [String: Any] {
            // the JSON's own "type" (phone, email, other) is the handle's kind, not the entry's
            for (from, to) in [("name", "name"), ("type", "handle_type"), ("label", "label"), ("countryCode", "country"),
                               ("title", "title"), ("artist", "artist"), ("mediaType", "media_type"),
                               ("identifier", "identifier")] {
                if let x = o[from] as? String, !x.isEmpty { one[to] = x }
            }
        }
        entries.append(one)
    }
    out["entries"] = entries
    return out
}

// MARK: this Mac's own databases (ADR 0309): Photos, Messages, Safari. Opened read-only and never as
// immutable: the apps keep them in WAL mode, so the newest rows may live only in the -wal file, and a
// reader does not block the app that writes. One read transaction per database, so its queries agree.
// Columns vary by macOS version: they are looked up by name and a missing one is left out.

enum SQLValue { case null, int(Int64), real(Double), text(String), blob([UInt8]) }
typealias SQLRow = [String: SQLValue]

extension Dictionary where Key == String, Value == SQLValue {
    func int(_ k: String) -> Int64? {
        switch self[k] {
        case .int(let v)?: return v
        case .real(let v)?: return v.isFinite ? Int64(v) : nil
        default: return nil
        }
    }
    func real(_ k: String) -> Double? {
        switch self[k] {
        case .int(let v)?: return Double(v)
        case .real(let v)?: return v.isFinite ? v : nil
        default: return nil
        }
    }
    func text(_ k: String) -> String? {
        if case .text(let v)? = self[k], !v.isEmpty { return v }
        return nil
    }
    func blob(_ k: String) -> [UInt8]? {
        if case .blob(let v)? = self[k], !v.isEmpty { return v }
        return nil
    }
}

final class ReadOnlyDB {
    private var db: OpaquePointer?
    /// "missing" (no such file) or "no_access" (it will not open or read: Full Disk Access), else nil.
    private(set) var failure: String?

    init(_ path: String) {
        if sqlite3_open_v2(path, &db, SQLITE_OPEN_READONLY, nil) != SQLITE_OK {
            failure = access(path, F_OK) != 0 && errno == ENOENT ? "missing" : "no_access"
            return
        }
        sqlite3_busy_timeout(db, 3000)
        // TCC can let the open through and refuse the first read.
        if sqlite3_exec(db, "BEGIN; SELECT count(*) FROM sqlite_master;", nil, nil, nil) != SQLITE_OK {
            failure = "no_access"
        }
    }
    deinit { sqlite3_close(db) }

    /// Every row of `sql`, or nil when it cannot be prepared or the read fails (busy past the timeout).
    func rows(_ sql: String) -> [SQLRow]? {
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { return nil }
        defer { sqlite3_finalize(stmt) }
        let count = Int(sqlite3_column_count(stmt))
        let names = (0..<count).map { String(cString: sqlite3_column_name(stmt, Int32($0))) }
        var out: [SQLRow] = []
        while true {
            let rc = sqlite3_step(stmt)
            if rc == SQLITE_DONE { return out }
            guard rc == SQLITE_ROW else { return nil }
            var row: SQLRow = [:]
            for c in 0..<count {
                let i = Int32(c)
                switch sqlite3_column_type(stmt, i) {
                case SQLITE_INTEGER: row[names[c]] = .int(sqlite3_column_int64(stmt, i))
                case SQLITE_FLOAT: row[names[c]] = .real(sqlite3_column_double(stmt, i))
                case SQLITE_TEXT: row[names[c]] = .text(String(cString: sqlite3_column_text(stmt, i)))
                case SQLITE_BLOB:
                    if let p = sqlite3_column_blob(stmt, i) {
                        let bytes = p.assumingMemoryBound(to: UInt8.self)
                        row[names[c]] = .blob(Array(UnsafeBufferPointer(start: bytes, count: Int(sqlite3_column_bytes(stmt, i)))))
                    }
                default: break
                }
            }
            out.append(row)
        }
    }

    func columns(_ table: String) -> Set<String> {
        Set((rows("SELECT name FROM pragma_table_info('\(table)')") ?? []).compactMap { $0.text("name") })
    }
}

/// `alias.name AS alias_name` for each wanted column the table has, NULL for one it lacks.
func pick(_ alias: String, _ have: Set<String>, _ names: [String]) -> String {
    names.map { have.contains($0) ? "\(alias).\($0) AS \(alias)_\($0)" : "NULL AS \(alias)_\($0)" }.joined(separator: ", ")
}

/// How a column becomes a field: an integer, a flag, a real, text, a CFAbsoluteTime as unix
/// milliseconds, a number only when positive, or an integer only when not zero (a default that says nothing).
enum Conv { case int, flag, real, text, cf, positive, nonzero }

func convert(_ row: SQLRow, _ column: String, _ conv: Conv) -> Any? {
    switch conv {
    case .int: return row.int(column)
    case .flag: return row.int(column).map { $0 != 0 }
    case .real: return row.real(column)
    case .text: return row.text(column)
    case .cf: return unixMs(cfSeconds: row.real(column))
    case .positive:
        guard let d = row.real(column), d > 0 else { return nil }
        return d == d.rounded() && d < 9e15 ? Int64(d) as Any : d as Any
    case .nonzero: return row.int(column).flatMap { $0 != 0 ? $0 : nil }
    }
}

typealias Spec = [(key: String, column: String, conv: Conv)]

func fill(_ out: inout [String: Any], _ row: SQLRow, _ spec: Spec) {
    for item in spec { if let v = convert(row, item.column, item.conv) { out[item.key] = v } }
}

func indexOf(_ hay: [UInt8], _ needle: [UInt8]) -> Int? {
    guard !needle.isEmpty, hay.count >= needle.count else { return nil }
    for i in 0...(hay.count - needle.count) where hay[i] == needle[0] && Array(hay[i..<(i + needle.count)]) == needle {
        return i
    }
    return nil
}

/// The text of an NSAttributedString archived as a typedstream (message.attributedBody): after the
/// class name "NSString" come a '+' (C string) tag, a length (one byte, or 0x81 / 0x82 and a
/// little-endian 16 / 32 bit count) and the UTF-8 bytes. Anything else is left undecoded, never guessed.
func typedstreamText(_ b: [UInt8]) -> String? {
    guard let m = indexOf(b, Array("NSString".utf8)) else { return nil }
    var i = m + 8
    let limit = min(b.count, i + 8)
    while i < limit, b[i] != 0x2B { i += 1 }
    guard i < limit else { return nil }
    i += 1
    guard i < b.count else { return nil }
    var len = Int(b[i]); i += 1
    if len == 0x81 {
        guard i + 2 <= b.count else { return nil }
        len = Int(b[i]) | Int(b[i + 1]) << 8; i += 2
    } else if len == 0x82 {
        guard i + 4 <= b.count else { return nil }
        len = Int(b[i]) | Int(b[i + 1]) << 8 | Int(b[i + 2]) << 16 | Int(b[i + 3]) << 24; i += 4
    } else if len >= 0x80 {
        return nil
    }
    guard i + len <= b.count else { return nil }
    return String(bytes: b[i..<(i + len)], encoding: .utf8)
}

/// The index an NSKeyedArchiver reference points at (its description reads "... {value = 4}").
func archiveIndex(_ x: Any?) -> Int? {
    guard let x = x, !(x is String), !(x is NSNumber) else { return nil }
    let d = "\(x)"
    guard d.contains("CFKeyedArchiverUID"), let r = d.range(of: "value = ") else { return nil }
    return Int(d[r.upperBound...].prefix { $0.isNumber })
}

/// The place a photo's reverse-geocoded location names (Photos' ZREVERSELOCATIONDATA, a keyed archive
/// of a postal address): the strings it holds, under their own names.
func placeOf(_ data: [UInt8]) -> [String: Any]? {
    guard let top = try? PropertyListSerialization.propertyList(from: Data(data), options: [], format: nil) as? [String: Any],
          let objects = top["$objects"] as? [Any] else { return nil }
    func object(_ x: Any?) -> Any? { archiveIndex(x).flatMap { $0 < objects.count ? objects[$0] : nil } ?? x }
    // "$null" is the archive's own marker for a value that is not there.
    func string(_ x: Any?) -> String? { (object(x) as? String).flatMap { $0.isEmpty || $0 == "$null" ? nil : $0 } }
    guard let root = object((top["$top"] as? [String: Any])?["root"]) as? [String: Any] else { return nil }
    var place: [String: Any] = [:]
    if let v = string(root["addressString"]) { place["address"] = v }
    if let v = string(root["countryCode"]) { place["country_code"] = v }
    if let v = object(root["isHome"]) as? NSNumber { place["is_home"] = v.boolValue }
    if let postal = object(root["postalAddress"]) as? [String: Any] {
        for (key, name) in [("_street", "street"), ("_subLocality", "sub_locality"), ("_city", "city"),
                            ("_subAdministrativeArea", "sub_administrative_area"), ("_state", "state"),
                            ("_postalCode", "postal_code"), ("_country", "country")] {
            if let v = string(postal[key]) { place[name] = v }
        }
    }
    return place.isEmpty ? nil : place
}

/// A read's outcome: the records, or why this database could not be read.
typealias LocalRead = (records: [[String: Any]], problem: String?)

func byTime(_ list: [[String: Any]]) -> [[String: Any]] {
    list.sorted { ($0["at_ms"] as! Int64) < ($1["at_ms"] as! Int64) }
}

let photoZ = ["Z_PK", "ZUUID", "ZKIND", "ZKINDSUBTYPE", "ZDATECREATED", "ZADDEDDATE", "ZMODIFICATIONDATE", "ZTRASHEDDATE",
              "ZADJUSTMENTTIMESTAMP", "ZLASTSHAREDDATE", "ZLATITUDE", "ZLONGITUDE", "ZWIDTH", "ZHEIGHT", "ZORIENTATION",
              "ZDURATION", "ZFAVORITE", "ZHIDDEN", "ZTRASHEDSTATE", "ZTRASHEDREASON", "ZVISIBILITYSTATE", "ZFILENAME",
              "ZDIRECTORY", "ZUNIFORMTYPEIDENTIFIER", "ZISDETECTEDSCREENSHOT", "ZPLAYBACKSTYLE", "ZPLAYBACKVARIATION",
              "ZAVALANCHEKIND", "ZAVALANCHEPICKTYPE", "ZAVALANCHEUUID", "ZSAVEDASSETTYPE", "ZDEPTHTYPE", "ZHDRTYPE",
              "ZSPATIALTYPE", "ZCLOUDASSETGUID", "ZMEDIAGROUPUUID", "ZMOMENT"]
let photoA = ["ZORIGINALFILENAME", "ZORIGINALFILESIZE", "ZORIGINALWIDTH", "ZORIGINALHEIGHT", "ZTITLE",
              "ZACCESSIBILITYDESCRIPTION", "ZTIMEZONENAME", "ZTIMEZONEOFFSET", "ZCAMERACAPTUREDEVICE",
              "ZIMPORTEDBYBUNDLEIDENTIFIER", "ZIMPORTEDBYDISPLAYNAME", "ZEXIFTIMESTAMPSTRING", "ZDATECREATEDSOURCE",
              "ZEDITORBUNDLEID", "ZGPSHORIZONTALACCURACY", "ZREVERSELOCATIONDATA"]
let photoE = ["ZCAMERAMAKE", "ZCAMERAMODEL", "ZLENSMODEL", "ZISO", "ZFOCALLENGTH", "ZFOCALLENGTHIN35MM", "ZAPERTURE",
              "ZSHUTTERSPEED", "ZEXPOSUREBIAS", "ZFLASHFIRED", "ZMETERINGMODE", "ZWHITEBALANCE", "ZCODEC", "ZFPS",
              "ZBITRATE", "ZDIGITALZOOMRATIO"]
let photoSpec: Spec = [
    ("asset_pk", "z_Z_PK", .int), ("subtype", "z_ZKINDSUBTYPE", .int),
    ("added_ms", "z_ZADDEDDATE", .cf), ("modified_ms", "z_ZMODIFICATIONDATE", .cf),
    ("trashed_ms", "z_ZTRASHEDDATE", .cf), ("adjusted_ms", "z_ZADJUSTMENTTIMESTAMP", .cf),
    ("shared_ms", "z_ZLASTSHAREDDATE", .cf),
    ("width", "z_ZWIDTH", .int), ("height", "z_ZHEIGHT", .int), ("orientation", "z_ZORIENTATION", .int),
    ("duration_s", "z_ZDURATION", .positive),
    ("favorite", "z_ZFAVORITE", .flag), ("hidden", "z_ZHIDDEN", .flag),
    ("trashed", "z_ZTRASHEDSTATE", .flag), ("trashed_reason", "z_ZTRASHEDREASON", .nonzero),
    ("visibility_state", "z_ZVISIBILITYSTATE", .int),
    ("filename", "z_ZFILENAME", .text), ("directory", "z_ZDIRECTORY", .text), ("uti", "z_ZUNIFORMTYPEIDENTIFIER", .text),
    ("screenshot", "z_ZISDETECTEDSCREENSHOT", .flag),
    ("playback_style", "z_ZPLAYBACKSTYLE", .int), ("playback_variation", "z_ZPLAYBACKVARIATION", .int),
    ("avalanche_kind", "z_ZAVALANCHEKIND", .int), ("avalanche_pick", "z_ZAVALANCHEPICKTYPE", .int),
    ("avalanche_uuid", "z_ZAVALANCHEUUID", .text),
    ("saved_asset_type", "z_ZSAVEDASSETTYPE", .int), ("depth_type", "z_ZDEPTHTYPE", .int),
    ("hdr_type", "z_ZHDRTYPE", .int), ("spatial_type", "z_ZSPATIALTYPE", .int),
    ("cloud_guid", "z_ZCLOUDASSETGUID", .text), ("media_group_uuid", "z_ZMEDIAGROUPUUID", .text),
    ("original_filename", "a_ZORIGINALFILENAME", .text), ("original_bytes", "a_ZORIGINALFILESIZE", .positive),
    ("original_width", "a_ZORIGINALWIDTH", .positive), ("original_height", "a_ZORIGINALHEIGHT", .positive),
    ("title", "a_ZTITLE", .text), ("description", "a_ZACCESSIBILITYDESCRIPTION", .text),
    ("timezone", "a_ZTIMEZONENAME", .text), ("timezone_offset_s", "a_ZTIMEZONEOFFSET", .int),
    ("capture_device", "a_ZCAMERACAPTUREDEVICE", .int),
    ("imported_by", "a_ZIMPORTEDBYBUNDLEIDENTIFIER", .text), ("imported_by_name", "a_ZIMPORTEDBYDISPLAYNAME", .text),
    ("exif_time", "a_ZEXIFTIMESTAMPSTRING", .text), ("created_source", "a_ZDATECREATEDSOURCE", .int),
    ("editor", "a_ZEDITORBUNDLEID", .text), ("gps_accuracy_m", "a_ZGPSHORIZONTALACCURACY", .positive),
    ("camera_make", "e_ZCAMERAMAKE", .text), ("camera_model", "e_ZCAMERAMODEL", .text),
    ("lens_model", "e_ZLENSMODEL", .text), ("iso", "e_ZISO", .positive),
    ("focal_length_mm", "e_ZFOCALLENGTH", .positive), ("focal_length_35mm", "e_ZFOCALLENGTHIN35MM", .positive),
    ("aperture", "e_ZAPERTURE", .positive), ("shutter_s", "e_ZSHUTTERSPEED", .positive),
    ("exposure_bias", "e_ZEXPOSUREBIAS", .real), ("flash_fired", "e_ZFLASHFIRED", .flag),
    ("metering_mode", "e_ZMETERINGMODE", .int), ("white_balance", "e_ZWHITEBALANCE", .int),
    ("codec", "e_ZCODEC", .text), ("fps", "e_ZFPS", .positive), ("bitrate", "e_ZBITRATE", .positive),
    ("digital_zoom", "e_ZDIGITALZOOMRATIO", .positive),
    ("moment_title", "m_ZTITLE", .text), ("moment_subtitle", "m_ZSUBTITLE", .text),
]

/// One record per asset in the Photos library: metadata only, never the image.
func readPhotos(sinceMs: Int64?) -> LocalRead {
    let db = ReadOnlyDB(home + "/Pictures/Photos Library.photoslibrary/database/Photos.sqlite")
    if let f = db.failure { return ([], f) }
    let z = db.columns("ZASSET"), a = db.columns("ZADDITIONALASSETATTRIBUTES")
    let e = db.columns("ZEXTENDEDATTRIBUTES"), m = db.columns("ZMOMENT")
    guard z.contains("ZUUID"), z.contains("ZDATECREATED") else { return ([], "unreadable") }
    let joinA = a.contains("ZASSET"), joinE = e.contains("ZASSET"), joinM = m.contains("Z_PK") && z.contains("ZMOMENT")
    // Touched = made, added, changed, trashed or adjusted: what a later read has to see again.
    let touched = "MAX(0, " + ["ZDATECREATED", "ZADDEDDATE", "ZMODIFICATIONDATE", "ZTRASHEDDATE", "ZADJUSTMENTTIMESTAMP"]
        .filter(z.contains).map { "COALESCE(z.\($0), 0)" }.joined(separator: ", ") + ")"
    var sql = "SELECT \(touched) AS touched, " + pick("z", z, photoZ) + ", " + pick("a", joinA ? a : [], photoA)
        + ", " + pick("e", joinE ? e : [], photoE) + ", " + pick("m", joinM ? m : [], ["ZTITLE", "ZSUBTITLE"]) + " FROM ZASSET z"
    if joinA { sql += " LEFT JOIN ZADDITIONALASSETATTRIBUTES a ON a.ZASSET = z.Z_PK" }
    if joinE { sql += " LEFT JOIN ZEXTENDEDATTRIBUTES e ON e.ZASSET = z.Z_PK" }
    if joinM { sql += " LEFT JOIN ZMOMENT m ON m.Z_PK = z.ZMOMENT" }
    if let s = sinceMs { sql += " WHERE \(touched) >= \(Double(s) / 1000 - 978_307_200)" }
    guard let found = db.rows(sql) else { return ([], "unreadable") }
    var out: [[String: Any]] = []
    for r in found {
        guard let uuid = r.text("z_ZUUID"), let created = unixMs(cfSeconds: r.real("z_ZDATECREATED")) else { continue }
        var rec: [String: Any] = ["uuid": uuid, "at_ms": created, "seen_ms": unixMs(cfSeconds: r.real("touched")) ?? created]
        rec["kind"] = r.int("z_ZKIND") == 0 ? "photo" : (r.int("z_ZKIND") == 1 ? "video" : "other")
        fill(&rec, r, photoSpec)
        // Photos marks "no location" with -180 in both; a real position lies inside the globe.
        if let lat = r.real("z_ZLATITUDE"), let lon = r.real("z_ZLONGITUDE"), abs(lat) <= 90, abs(lon) < 180 {
            rec["latitude"] = lat
            rec["longitude"] = lon
        }
        rec["live"] = r.int("z_ZPLAYBACKSTYLE") == 3  // PHAssetPlaybackStyle.livePhoto
        rec["burst"] = (r.int("z_ZAVALANCHEKIND") ?? 0) > 0 || r.text("z_ZAVALANCHEUUID") != nil
        if let place = r.blob("a_ZREVERSELOCATIONDATA").flatMap(placeOf) { rec["place"] = place }
        out.append(rec)
    }
    return (byTime(out), nil)
}

let messageColumns = ["ROWID", "guid", "text", "attributedBody", "service", "date", "date_read", "date_delivered",
                      "date_edited", "date_retracted", "is_from_me", "is_read", "is_delivered", "is_sent",
                      "is_system_message", "is_audio_message", "is_auto_reply", "was_downgraded", "error", "item_type",
                      "group_title", "group_action_type", "subject", "associated_message_guid", "associated_message_type",
                      "associated_message_emoji", "reply_to_guid", "thread_originator_guid", "thread_originator_part",
                      "balloon_bundle_id", "expressive_send_style_id", "part_count", "destination_caller_id", "is_spam"]
let messageSpec: Spec = [
    ("service", "m_service", .text), ("handle", "h_id", .text), ("from_me", "m_is_from_me", .flag),
    ("is_read", "m_is_read", .flag), ("is_delivered", "m_is_delivered", .flag), ("is_sent", "m_is_sent", .flag),
    ("is_system", "m_is_system_message", .flag), ("is_audio", "m_is_audio_message", .flag),
    ("is_auto_reply", "m_is_auto_reply", .flag), ("was_downgraded", "m_was_downgraded", .flag),
    ("is_spam", "m_is_spam", .flag),
    ("error", "m_error", .nonzero), ("item_type", "m_item_type", .nonzero),
    ("group_title", "m_group_title", .text), ("group_action_type", "m_group_action_type", .nonzero),
    ("subject", "m_subject", .text), ("associated_guid", "m_associated_message_guid", .text),
    ("associated_type", "m_associated_message_type", .nonzero), ("associated_emoji", "m_associated_message_emoji", .text),
    ("reply_to_guid", "m_reply_to_guid", .text), ("thread_originator_guid", "m_thread_originator_guid", .text),
    ("thread_originator_part", "m_thread_originator_part", .text),
    ("balloon_bundle_id", "m_balloon_bundle_id", .text), ("effect", "m_expressive_send_style_id", .text),
    ("part_count", "m_part_count", .positive), ("own_handle", "m_destination_caller_id", .text),
    ("chat_guid", "c_guid", .text), ("chat_identifier", "c_chat_identifier", .text),
    ("chat_display_name", "c_display_name", .text), ("chat_service", "c_service_name", .text),
]
let attachmentSpec: Spec = [
    ("guid", "guid", .text), ("filename", "filename", .text), ("transfer_name", "transfer_name", .text),
    ("mime_type", "mime_type", .text), ("uti", "uti", .text), ("bytes", "total_bytes", .positive),
    ("sticker", "is_sticker", .flag), ("outgoing", "is_outgoing", .flag), ("transfer_state", "transfer_state", .int),
]

/// One record per message of chat.db (iMessage, SMS, RCS), with its words and its attachments' metadata.
func readMessages(sinceMs: Int64?) -> LocalRead {
    let db = ReadOnlyDB(home + "/Library/Messages/chat.db")
    if let f = db.failure { return ([], f) }
    let m = db.columns("message"), c = db.columns("chat"), h = db.columns("handle"), j = db.columns("chat_message_join")
    guard m.contains("guid"), m.contains("date") else { return ([], "unreadable") }
    let joinH = m.contains("handle_id") && h.contains("ROWID") && h.contains("id")
    let joinC = j.contains("message_id") && j.contains("chat_id") && c.contains("ROWID")
    // date is nanoseconds since 2001 on current macOS, seconds on old ones.
    let top = db.rows("SELECT MAX(date) AS top FROM message")?.first?.real("top") ?? 0
    let perSecond: Double = top > 1e11 ? 1e9 : 1
    func ms(_ row: SQLRow, _ column: String) -> Int64? {
        guard let v = row.real(column), v > 0 else { return nil }
        return Int64(((v / perSecond + 978_307_200) * 1000).rounded())
    }
    let touched = "MAX(0, " + ["date", "date_read", "date_delivered", "date_edited", "date_retracted"].filter(m.contains)
        .map { "COALESCE(m.\($0), 0)" }.joined(separator: ", ") + ")"
    var sql = "SELECT " + pick("m", m, messageColumns) + ", "
        + pick("c", joinC ? c : [], ["ROWID", "guid", "chat_identifier", "display_name", "style", "service_name"]) + ", "
        + (joinH ? "h.id AS h_id" : "NULL AS h_id") + " FROM message m"
    if joinH { sql += " LEFT JOIN handle h ON h.ROWID = m.handle_id" }
    if joinC { sql += " LEFT JOIN chat_message_join j ON j.message_id = m.ROWID LEFT JOIN chat c ON c.ROWID = j.chat_id" }
    if let s = sinceMs { sql += " WHERE \(touched) >= \((Double(s) / 1000 - 978_307_200) * perSecond)" }
    guard let found = db.rows(sql) else { return ([], "unreadable") }
    // The members of a chat, and the attachments of a message (metadata only, never the files).
    var people: [Int64: [String]] = [:]
    let membersSQL = "SELECT j.chat_id AS chat_id, h.id AS id FROM chat_handle_join j JOIN handle h ON h.ROWID = j.handle_id ORDER BY h.id"
    for r in db.rows(membersSQL) ?? [] {
        if let chat = r.int("chat_id"), let id = r.text("id") { people[chat, default: []].append(id) }
    }
    var files: [Int64: [[String: Any]]] = [:]
    let attachmentSQL = "SELECT j.message_id AS message_id, a.guid AS guid, a.filename AS filename, a.transfer_name AS transfer_name, "
        + "a.mime_type AS mime_type, a.uti AS uti, a.total_bytes AS total_bytes, a.is_sticker AS is_sticker, "
        + "a.is_outgoing AS is_outgoing, a.transfer_state AS transfer_state "
        + "FROM message_attachment_join j JOIN attachment a ON a.ROWID = j.attachment_id ORDER BY a.ROWID"
    for r in db.rows(attachmentSQL) ?? [] {
        guard let message = r.int("message_id") else { continue }
        var one: [String: Any] = [:]
        fill(&one, r, attachmentSpec)
        files[message, default: []].append(one)
    }
    var out: [[String: Any]] = []
    var seen = Set<String>()
    for r in found {
        guard let guid = r.text("m_guid"), let at = ms(r, "m_date"), seen.insert(guid).inserted else { continue }
        let seenMs = ["m_date", "m_date_read", "m_date_delivered", "m_date_edited", "m_date_retracted"].compactMap { ms(r, $0) }.max() ?? at
        var rec: [String: Any] = ["guid": guid, "at_ms": at, "seen_ms": seenMs]
        if let v = ms(r, "m_date_read") { rec["read_ms"] = v }
        if let v = ms(r, "m_date_delivered") { rec["delivered_ms"] = v }
        if let v = ms(r, "m_date_edited") { rec["edited_ms"] = v }
        if let v = ms(r, "m_date_retracted") { rec["retracted_ms"] = v }
        // The words are `text`; modern macOS leaves it NULL and keeps them in attributedBody.
        if let t = r.text("m_text") {
            rec["text"] = t
            rec["text_source"] = "text"
        } else if let t = r.blob("m_attributedBody").flatMap(typedstreamText), !t.isEmpty {
            rec["text"] = t
            rec["text_source"] = "attributed_body"
        }
        fill(&rec, r, messageSpec)
        if let style = r.int("c_style") { rec["is_group"] = style == 43 }
        if let chat = r.int("c_ROWID"), let list = people[chat] { rec["participants"] = list }
        if let message = r.int("m_ROWID"), let list = files[message] { rec["attachments"] = list }
        out.append(rec)
    }
    return (byTime(out), nil)
}

let visitSpec: Spec = [
    ("visit_id", "v_id", .int), ("item_id", "v_history_item", .int), ("title", "v_title", .text),
    ("load_successful", "v_load_successful", .flag), ("http_non_get", "v_http_non_get", .flag),
    ("synthesized", "v_synthesized", .flag), ("redirect_source", "v_redirect_source", .int),
    ("redirect_destination", "v_redirect_destination", .int), ("origin", "v_origin", .int),
    ("generation", "v_generation", .int), ("attributes", "v_attributes", .int), ("score", "v_score", .int),
]

/// One record per visit in Safari's History.db: this Mac's own and those synced from his other devices.
func readSafari(sinceMs: Int64?) -> LocalRead {
    let db = ReadOnlyDB(home + "/Library/Safari/History.db")
    if let f = db.failure { return ([], f) }
    let v = db.columns("history_visits"), i = db.columns("history_items")
    guard v.contains("visit_time"), v.contains("history_item"), i.contains("url") else { return ([], "unreadable") }
    var sql = "SELECT " + pick("v", v, ["id", "history_item", "visit_time", "title", "load_successful", "http_non_get",
                                         "synthesized", "redirect_source", "redirect_destination", "origin", "generation",
                                         "attributes", "score"])
        + ", i.url AS i_url FROM history_visits v JOIN history_items i ON i.id = v.history_item"
    if let s = sinceMs { sql += " WHERE v.visit_time >= \(Double(s) / 1000 - 978_307_200)" }
    guard let found = db.rows(sql) else { return ([], "unreadable") }
    var out: [[String: Any]] = []
    for r in found {
        guard let url = r.text("i_url"), let at = unixMs(cfSeconds: r.real("v_visit_time")) else { continue }
        var rec: [String: Any] = ["url": url, "at_ms": at, "seen_ms": at]
        fill(&rec, r, visitSpec)
        out.append(rec)
    }
    return (byTime(out), nil)
}

// MARK: reading a stream's segment files

func remoteDevices(_ stream: String) -> [String] {
    let path = biome + "/streams/restricted/" + stream + "/remote"
    return ((try? FileManager.default.contentsOfDirectory(atPath: path)) ?? []).filter { !$0.hasPrefix(".") }
}

func segments(_ stream: String, _ id: String) -> [Segment] {
    let dir = biome + "/streams/restricted/" + stream + "/remote/" + id
    guard let files = try? FileManager.default.contentsOfDirectory(atPath: dir) else { return [] }
    var out: [Segment] = []
    for file in files.sorted() where !file.hasPrefix(".") && file != "tombstone" {
        let data: Data
        do { data = try Data(contentsOf: URL(fileURLWithPath: dir + "/" + file)) } catch {
            if (error as NSError).code == NSFileReadNoPermissionError { emit(["error": "no_access"]) }
            continue
        }
        out += segbRecords([UInt8](data))
    }
    return out
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
var ids: [String] = []
if let dir = opendir(remote) {
    while let entry = readdir(dir) {
        let name = withUnsafePointer(to: entry.pointee.d_name) {
            $0.withMemoryRebound(to: CChar.self, capacity: Int(entry.pointee.d_namlen) + 1) { String(cString: $0) }
        }
        if !name.hasPrefix(".") { ids.append(name) }
    }
    closedir(dir)
} else if !(errno == ENOENT && !wantedStreams.isEmpty) {  // other streams may exist without App.InFocus
    emit(["error": errno == ENOENT ? "no_remote_streams" : (errno == EPERM || errno == EACCES ? "no_access" : "open_failed")])
}
var streamNames: [String] = []
if wantedStreams.contains("now_playing") { streamNames.append("Media.NowPlaying") }
if wantedStreams.contains("bluetooth") { streamNames.append("Device.Wireless.Bluetooth") }
if wantedStreams.contains("interactions") { streamNames += siriStreams.keys }
for stream in streamNames { ids += remoteDevices(stream) }
ids = Array(Set(ids)).sorted()

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
            guard let f = parseFocus(record.data), f.atMs >= sinceMs else { continue }
            events.append(["device": id, "bundle_id": f.bundle, "focus": f.gained ? "in" : "out", "at_ms": f.atMs])
        }
    }
}
events.sort { ($0["at_ms"] as! Int64) < ($1["at_ms"] as! Int64) }
var answer: [String: Any] = ["devices": devices, "events": events]
if !wantedStreams.isEmpty {
    var streams: [String: [[String: Any]]] = ["now_playing": [], "bluetooth": [], "interactions": []]
    var unavailable: [String: String] = [:]
    for (name, read) in [("photos", readPhotos), ("messages", readMessages), ("safari", readSafari)] where wantedStreams.contains(name) {
        let (records, problem) = read(localSinceMs)
        streams[name] = records
        if let problem = problem { unavailable[name] = problem }
    }
    if !unavailable.isEmpty { answer["unavailable"] = unavailable }
    for id in ids {
        if wantedStreams.contains("now_playing") {
            streams["now_playing"]! += segments("Media.NowPlaying", id).compactMap { parseNowPlaying(id, $0) }
        }
        if wantedStreams.contains("bluetooth") {
            streams["bluetooth"]! += segments("Device.Wireless.Bluetooth", id).compactMap { parseBluetooth(id, $0) }
        }
        if wantedStreams.contains("interactions") {
            for (stream, source) in siriStreams {
                streams["interactions"]! += segments(stream, id).compactMap { parseSiri(id, source, $0) }
            }
        }
    }
    for name in ["now_playing", "bluetooth", "interactions"] {
        streams[name] = streams[name]!.filter { ($0["at_ms"] as! Int64) >= sinceMs }.sorted { ($0["at_ms"] as! Int64) < ($1["at_ms"] as! Int64) }
    }
    answer["streams"] = streams
}
emit(answer)
