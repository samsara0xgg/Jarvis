// jarvis-screen-time: iPhone/iPad app focus events that macOS syncs into Biome, written as one JSON object.
// Usage: jarvis-screen-time <outfile> [--since-ms N] [--root <biome dir>] [--streams a,b,c]
// --streams adds a "streams" object with typed records of the other synced streams: now_playing
// (Media.NowPlaying), bluetooth (Device.Wireless.Bluetooth) and interactions (the four
// Siri.Remembers.* streams); without it the output is only devices and events.
// Must run as the .app bundle (see Info.plist): Full Disk Access is granted to the app, not to Python.
//
// Reads <biome>/streams/restricted/App.InFocus/remote/<device id>/<segment> (SEGB files, v1 or v2,
// each record one protobuf AppInFocus event) and <biome>/sync/sync.db for device names.
// Record format: docs/adr/0304-*.md and 0305-*.md. Remote devices only; this Mac's own "local" stream
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
    for (name, list) in streams {
        streams[name] = list.filter { ($0["at_ms"] as! Int64) >= sinceMs }.sorted { ($0["at_ms"] as! Int64) < ($1["at_ms"] as! Int64) }
    }
    answer["streams"] = streams
}
emit(answer)
