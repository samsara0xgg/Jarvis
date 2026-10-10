import Foundation

// Every frame the phone and the host exchange, and nothing else knows the byte layouts. ADR 0209 is not final: change the codecs here.
//
// Binary (one WebSocket message = u8 type + little-endian payload; main.swift's layouts without the u32 length prefix):
//   host -> phone   2 ACTIVE i64 generation (-1: none) | 3 DISCARD u64 seq | 4 GAIN f32 target, u32 ramp_samples
//                   5 HOLD i64 generation (-1: none)   | 6 PCM16 i64 generation, i64 start_cursor, i16[] at 32 kHz
//   phone -> host   0x81 READY | 0x82 REPORT | 0x83 STATUS | 0x84 DISCARD_ACK (ADR 0129)
// Cursors are in 32 kHz source samples. Times are the phone's CLOCK_UPTIME_RAW in nanoseconds.

let kWireRate = 32_000

struct ByteWriter {
    private(set) var data = Data()
    init(type: UInt8) { data.append(type) }
    mutating func put<T: FixedWidthInteger>(_ v: T) { withUnsafeBytes(of: v.littleEndian) { data.append(contentsOf: $0) } }
    mutating func put(_ v: Float) { put(v.bitPattern) }
    mutating func put(_ v: Double) { put(v.bitPattern) }
    mutating func put(bytes: UnsafeRawBufferPointer) { data.append(contentsOf: bytes) }
}

struct ByteReader {
    let data: Data
    private(set) var at: Int
    init(_ data: Data) { self.data = data; at = data.startIndex }
    var rest: Int { data.endIndex - at }
    mutating func get<T: FixedWidthInteger>(_: T.Type = T.self) -> T? {
        let n = MemoryLayout<T>.size
        guard rest >= n else { return nil }
        var v: T = 0
        withUnsafeMutableBytes(of: &v) { $0.copyBytes(from: data[at..<at + n]) }
        at += n
        return T(littleEndian: v)
    }
    mutating func getFloat() -> Float? { get(UInt32.self).map { Float(bitPattern: $0) } }
    mutating func getDouble() -> Double? { get(UInt64.self).map { Double(bitPattern: $0) } }
}

/// host -> phone
enum PlayerIn: Equatable, Sendable {
    case active(Int64)
    case discard(UInt64)
    case gain(target: Float, ramp: UInt32)
    case hold(Int64)
    case pcm(generation: Int64, start: Int64, samples: [Int16])

    var bytes: Data {
        switch self {
        case .active(let g): var w = ByteWriter(type: 2); w.put(g); return w.data
        case .discard(let s): var w = ByteWriter(type: 3); w.put(s); return w.data
        case .gain(let t, let r): var w = ByteWriter(type: 4); w.put(t); w.put(r); return w.data
        case .hold(let g): var w = ByteWriter(type: 5); w.put(g); return w.data
        case .pcm(let g, let s, let x):
            var w = ByteWriter(type: 6); w.put(g); w.put(s)
            x.withUnsafeBytes { w.put(bytes: $0) }       // little-endian host
            return w.data
        }
    }

    init?(_ data: Data) {
        guard let type = data.first else { return nil }
        var r = ByteReader(data.dropFirst())
        switch type {
        case 2: guard let g: Int64 = r.get() else { return nil }; self = .active(g)
        case 3: guard let s: UInt64 = r.get() else { return nil }; self = .discard(s)
        case 4: guard let t = r.getFloat(), let n: UInt32 = r.get() else { return nil }; self = .gain(target: t, ramp: n)
        case 5: guard let g: Int64 = r.get() else { return nil }; self = .hold(g)
        case 6:
            guard let g: Int64 = r.get(), let s: Int64 = r.get() else { return nil }
            let n = r.rest / 2
            var x = [Int16](repeating: 0, count: n)
            x.withUnsafeMutableBytes { $0.copyBytes(from: r.data[r.at..<r.at + n * 2]) }
            self = .pcm(generation: g, start: s, samples: x)
        default: return nil
        }
    }
}

struct PlayerReport: Equatable, Sendable {
    var generation: Int64, start: Int64, end: Int64
    var audibility: UInt8                // 0 normal, 1 attenuated, 2 muted, 3 unknown
    var callbackNs: Int64, presentationDelayNs: Int64
    var first: Bool
}

struct PlayerStatus: Equatable, Sendable {
    var readIdx: Int64, gain: Double, callbacks: UInt64, overloads: UInt64, starvationGaps: UInt64
    var tailRampSamples: UInt32, playedSamples: UInt64
}

/// phone -> host
enum PlayerOut: Equatable, Sendable {
    case ready(sampleRate: UInt32, bufferFrames: UInt32, deviceLatencyNs: Int64, clockNs: Int64)
    case report(PlayerReport)
    case status(PlayerStatus)
    case discardAck(UInt64)

    var bytes: Data {
        switch self {
        case .ready(let rate, let frames, let lat, let clock):
            var w = ByteWriter(type: 0x81); w.put(rate); w.put(frames); w.put(lat); w.put(clock); return w.data
        case .report(let p):
            var w = ByteWriter(type: 0x82)
            w.put(p.generation); w.put(p.start); w.put(p.end); w.put(p.audibility)
            w.put(p.callbackNs); w.put(p.presentationDelayNs); w.put(UInt8(p.first ? 1 : 0))
            return w.data
        case .status(let s):
            var w = ByteWriter(type: 0x83)
            w.put(s.readIdx); w.put(s.gain); w.put(s.callbacks); w.put(s.overloads); w.put(s.starvationGaps)
            w.put(s.tailRampSamples); w.put(s.playedSamples)
            return w.data
        case .discardAck(let seq): var w = ByteWriter(type: 0x84); w.put(seq); return w.data
        }
    }

    init?(_ data: Data) {
        guard let type = data.first else { return nil }
        var r = ByteReader(data.dropFirst())
        switch type {
        case 0x81:
            guard let a: UInt32 = r.get(), let b: UInt32 = r.get(), let c: Int64 = r.get(), let d: Int64 = r.get() else { return nil }
            self = .ready(sampleRate: a, bufferFrames: b, deviceLatencyNs: c, clockNs: d)
        case 0x82:
            guard let g: Int64 = r.get(), let s: Int64 = r.get(), let e: Int64 = r.get(), let a: UInt8 = r.get(),
                  let cb: Int64 = r.get(), let d: Int64 = r.get(), let f: UInt8 = r.get() else { return nil }
            self = .report(PlayerReport(generation: g, start: s, end: e, audibility: a, callbackNs: cb, presentationDelayNs: d, first: f != 0))
        case 0x83:
            guard let ri: Int64 = r.get(), let g = r.getDouble(), let cb: UInt64 = r.get(), let o: UInt64 = r.get(),
                  let sg: UInt64 = r.get(), let t: UInt32 = r.get(), let p: UInt64 = r.get() else { return nil }
            self = .status(PlayerStatus(readIdx: ri, gain: g, callbacks: cb, overloads: o, starvationGaps: sg, tailRampSamples: t, playedSamples: p))
        case 0x84: guard let s: UInt64 = r.get() else { return nil }; self = .discardAck(s)
        default: return nil
        }
    }
}

// MARK: JSON text frames

/// A row of the conversation as the host sends it; `text` is her words, if the row carries any.
/// TODO(ADR 0209): confirm the row shape (keys, whether a row is a segment or the whole answer so far).
struct RowFrame: @unchecked Sendable, Equatable {
    let json: [String: Any]
    var turnId: String? { json["turn_id"] as? String }
    var role: String? { json["role"] as? String }
    var text: String? { json["text"] as? String }
    static func == (a: RowFrame, b: RowFrame) -> Bool { NSDictionary(dictionary: a.json).isEqual(to: b.json) }
}

/// phone -> host
enum PhoneFrame: Equatable, Sendable {
    case hello
    case say(utteranceId: String, text: String, language: String?, spoken: Bool)
    case barge
    case cancel
    case ping(tNs: Int64)

    var json: Data {
        var o: [String: Any]
        switch self {
        case .hello: o = ["type": "hello", "voice": true]
        case .say(let id, let text, let lang, let spoken):
            o = ["type": "say", "utterance_id": id, "text": text, "spoken": spoken]
            if let lang { o["language"] = lang }
        case .barge: o = ["type": "barge"]
        case .cancel: o = ["type": "cancel"]
        case .ping(let t): o = ["type": "ping", "t_ns": t]
        }
        return (try? JSONSerialization.data(withJSONObject: o, options: [.sortedKeys])) ?? Data()
    }

    init?(_ data: Data) {
        guard let o = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any], let type = o["type"] as? String else { return nil }
        switch type {
        case "hello": self = .hello
        case "say":
            guard let id = o["utterance_id"] as? String, let text = o["text"] as? String, let spoken = o["spoken"] as? Bool else { return nil }
            self = .say(utteranceId: id, text: text, language: o["language"] as? String, spoken: spoken)
        case "barge": self = .barge
        case "cancel": self = .cancel
        case "ping": guard let t = (o["t_ns"] as? NSNumber)?.int64Value else { return nil }; self = .ping(tNs: t)
        default: return nil
        }
    }
}

/// host -> phone
enum HostFrame: Equatable, Sendable {
    case ready(device: String, voice: String, sampleRate: Int)
    case said(utteranceId: String, turnId: String)
    case row(RowFrame)
    case pong(tNs: Int64?)
    case error(String)

    var json: Data {                                    // the host's side; here for the round-trip check
        var o: [String: Any]
        switch self {
        case .ready(let d, let v, let r): o = ["type": "ready", "device": d, "voice": v, "sample_rate": r]
        case .said(let u, let t): o = ["type": "said", "utterance_id": u, "turn_id": t]
        case .row(let r): o = r.json; o["type"] = "row"
        case .pong(let t): o = ["type": "pong"]; if let t { o["t_ns"] = t }
        case .error(let m): o = ["type": "error", "message": m]
        }
        return (try? JSONSerialization.data(withJSONObject: o, options: [.sortedKeys])) ?? Data()
    }

    init?(_ data: Data) {
        guard var o = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any], let type = o["type"] as? String else { return nil }
        switch type {
        case "ready":
            guard let d = o["device"] as? String, let v = o["voice"] as? String, let r = (o["sample_rate"] as? NSNumber)?.intValue else { return nil }
            self = .ready(device: d, voice: v, sampleRate: r)
        case "said":
            guard let u = o["utterance_id"] as? String, let t = o["turn_id"] as? String else { return nil }
            self = .said(utteranceId: u, turnId: t)
        case "row": o["type"] = nil; self = .row(RowFrame(json: o))
        case "pong": self = .pong(tNs: (o["t_ns"] as? NSNumber)?.int64Value)
        case "error": self = .error(o["message"] as? String ?? "error")
        default: return nil
        }
    }
}

#if DEBUG || VOICE_HARNESS
func demoVoiceWire() {
    let ins: [PlayerIn] = [.active(7), .active(-1), .discard(42), .gain(target: 0.25, ramp: 1920), .hold(-1), .hold(3),
                           .pcm(generation: 5, start: 96_000, samples: [0, 1, -1, .max, .min, 1234]), .pcm(generation: 1, start: 0, samples: [])]
    for f in ins { assert(PlayerIn(f.bytes) == f, "\(f)") }
    // the PCM16 layout itself: type 6, i64 generation, i64 start cursor, i16 samples, all little-endian
    let b = [UInt8](PlayerIn.pcm(generation: 0x0102, start: 0x0304, samples: [-2, 0x0506]).bytes)
    assert(b == [6, 2, 1, 0, 0, 0, 0, 0, 0, 4, 3, 0, 0, 0, 0, 0, 0, 0xFE, 0xFF, 6, 5], "\(b)")
    let outs: [PlayerOut] = [
        .ready(sampleRate: 48_000, bufferFrames: 1024, deviceLatencyNs: 12_000_000, clockNs: 123_456_789_012),
        .report(PlayerReport(generation: 9, start: 100, end: 740, audibility: 1, callbackNs: 55, presentationDelayNs: 66, first: true)),
        .report(PlayerReport(generation: -1, start: 0, end: 0, audibility: 3, callbackNs: 0, presentationDelayNs: 0, first: false)),
        .status(PlayerStatus(readIdx: 77, gain: 0.5, callbacks: 3, overloads: 2, starvationGaps: 1, tailRampSamples: 128, playedSamples: 99)),
        .discardAck(UInt64.max)]
    for f in outs { assert(PlayerOut(f.bytes) == f, "\(f)") }
    assert([UInt8](PlayerOut.discardAck(1).bytes) == [0x84, 1, 0, 0, 0, 0, 0, 0, 0])
    assert(PlayerIn(Data([6, 1, 2])) == nil && PlayerIn(Data()) == nil && PlayerOut(Data([0x82, 1])) == nil)
    let phone: [PhoneFrame] = [.hello, .say(utteranceId: "u1", text: "你好 Jarvis", language: "zh", spoken: true),
                               .say(utteranceId: "u2", text: "hi", language: nil, spoken: false), .barge, .cancel, .ping(tNs: 1_234_567_890_123)]
    for f in phone { assert(PhoneFrame(f.json) == f, "\(f)") }
    let host: [HostFrame] = [.ready(device: "mac", voice: "ember", sampleRate: 32_000), .said(utteranceId: "u1", turnId: "t9"),
                             .row(RowFrame(json: ["turn_id": "t9", "role": "assistant", "text": "好的"])), .pong(tNs: 5), .pong(tNs: nil), .error("nope")]
    for f in host { assert(HostFrame(f.json) == f, "\(f)") }
}
#endif
