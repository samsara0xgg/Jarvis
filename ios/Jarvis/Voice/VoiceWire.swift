import Foundation

// Every frame the phone and the host exchange, and nothing else knows the byte layouts (ADR 0209; jarvis/surface/phone_link.py and phone_player.py on the host).
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

/// One event-log row the host sends for this device's turns: surface.response_open | response_chunk | response_emitted, response.cancelled | failed.
/// Every one carries payload.turn_id; chunks and emitted carry payload.text; open, chunk and emitted may carry payload.phase ("commentary" | "final").
struct RowFrame: @unchecked Sendable, Equatable {
    let eventType: String, eventUid: String, payload: [String: Any], tsEpochMs: Int64
    var turnId: String? { payload["turn_id"] as? String }
    var phase: String? { payload["phase"] as? String }
    var text: String? { payload["text"] as? String }
    var reason: String? { payload["reason"] as? String }
    static func == (a: RowFrame, b: RowFrame) -> Bool {
        a.eventType == b.eventType && a.eventUid == b.eventUid && a.tsEpochMs == b.tsEpochMs && NSDictionary(dictionary: a.payload).isEqual(to: b.payload)
    }
}

/// One tool call of a turn this device opened, voice or typed (ADR 0228). `label` is the host's own display text, never built here; the same `step` comes
/// again with `done` or `failed` and `endedMs` when the tool ends. The times are the host's clock: a duration between two of them is good, elapsed here is not.
struct StepFrame: Equatable, Sendable {
    enum State: String, Sendable { case running, done, failed }
    let turnId: String, step: Int, tool: String, label: String, state: State, startedMs: Int64, endedMs: Int64?
}

/// phone -> host
enum PhoneFrame: Equatable, Sendable {
    case hello
    case say(utteranceId: String, text: String, language: String?, confidence: Double?, spoken: Bool, overHer: Bool = false, conversation: Bool = false,
             about: Data? = nil,                        // the item he has open (ADR 0214), as JSON
             attachments: [String] = [])                // ids from POST /inherent/attachments; typed words only
    case barge
    case cancel
    case ping(tNs: Int64)

    var json: Data {
        var o: [String: Any]
        switch self {
        case .hello: o = ["type": "hello", "voice": true]
        case .say(let id, let text, let lang, let conf, let spoken, let overHer, let conversation, let about, let attachments):
            o = ["type": "say", "utterance_id": id, "text": text, "spoken": spoken]
            if !attachments.isEmpty { o["attachments"] = attachments }
            if let lang { o["language"] = lang }
            if let conf { o["confidence"] = conf }
            if overHer { o["over_her"] = true }                 // only meaningful with spoken
            if conversation { o["conversation"] = true }
            if let about, let a = try? JSONSerialization.jsonObject(with: about) { o["about"] = a }
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
            self = .say(utteranceId: id, text: text, language: o["language"] as? String, confidence: (o["confidence"] as? NSNumber)?.doubleValue, spoken: spoken,
                         overHer: o["over_her"] as? Bool ?? false, conversation: o["conversation"] as? Bool ?? false,
                         about: (o["about"] as? [String: Any]).flatMap { try? JSONSerialization.data(withJSONObject: $0, options: [.sortedKeys]) },
                         attachments: o["attachments"] as? [String] ?? [])
        case "barge": self = .barge
        case "cancel": self = .cancel
        case "ping": guard let t = (o["t_ns"] as? NSNumber)?.int64Value else { return nil }; self = .ping(tNs: t)
        default: return nil
        }
    }
}

/// host -> phone
enum HostFrame: Equatable, Sendable {
    /// judges: the host judges over-her and follow-up says (a `verdict` on every `said`); old hosts omit it
    case ready(device: String, voice: Bool, sampleRate: Int, judges: Bool)
    /// verdict: turn | backchannel | unclear | echo | stop | wait | dismissed | quiet:<level>; turnId is nil unless it is a turn
    case said(utteranceId: String, turnId: String?, verdict: String)
    case row(RowFrame)
    case step(StepFrame)
    case pong(tNs: Int64, hostNs: Int64)
    /// codes: bad_say, bad_ping, bad_frame, no_voice, retry (resend the same say), interrupt_failed; attachment_refused (the message is the store's sentence)
    /// and no_attachments (this host takes none) for a typed say with files
    case error(code: String, message: String)

    var json: Data {                                    // the host's side; here for the round-trip check
        var o: [String: Any]
        switch self {
        case .ready(let d, let v, let r, let j): o = ["type": "ready", "device": d, "voice": v, "sample_rate": r, "judges": j]
        case .said(let u, let t, let v): o = ["type": "said", "utterance_id": u, "turn_id": t ?? NSNull(), "verdict": v]
        case .row(let r): o = ["type": "row", "event_type": r.eventType, "event_uid": r.eventUid, "payload": r.payload, "ts_epoch_ms": r.tsEpochMs]
        case .step(let f):
            o = ["type": "step", "turn_id": f.turnId, "step": f.step, "tool": f.tool, "label": f.label, "state": f.state.rawValue, "started_ms": f.startedMs,
                 "ended_ms": f.endedMs.map { $0 as Any } ?? NSNull()]
        case .pong(let t, let h): o = ["type": "pong", "t_ns": t, "host_ns": h]
        case .error(let c, let m): o = ["type": "error", "code": c, "message": m]
        }
        return (try? JSONSerialization.data(withJSONObject: o, options: [.sortedKeys])) ?? Data()
    }

    init?(_ data: Data) {
        guard let o = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any], let type = o["type"] as? String else { return nil }
        switch type {
        case "ready":
            guard let d = o["device"] as? String, let v = o["voice"] as? Bool, let r = (o["sample_rate"] as? NSNumber)?.intValue else { return nil }
            self = .ready(device: d, voice: v, sampleRate: r, judges: o["judges"] as? Bool ?? false)
        case "said":
            guard let u = o["utterance_id"] as? String else { return nil }
            self = .said(utteranceId: u, turnId: o["turn_id"] as? String, verdict: o["verdict"] as? String ?? "turn")
        case "row":
            guard let t = o["event_type"] as? String, let u = o["event_uid"] as? String, let p = o["payload"] as? [String: Any],
                  let ts = (o["ts_epoch_ms"] as? NSNumber)?.int64Value else { return nil }
            self = .row(RowFrame(eventType: t, eventUid: u, payload: p, tsEpochMs: ts))
        case "step":
            guard let turn = o["turn_id"] as? String, let n = (o["step"] as? NSNumber)?.intValue, let label = o["label"] as? String,
                  let state = (o["state"] as? String).flatMap(StepFrame.State.init(rawValue:)) else { return nil }
            self = .step(StepFrame(turnId: turn, step: n, tool: o["tool"] as? String ?? "", label: label, state: state,
                                   startedMs: (o["started_ms"] as? NSNumber)?.int64Value ?? 0, endedMs: (o["ended_ms"] as? NSNumber)?.int64Value))
        case "pong":
            guard let t = (o["t_ns"] as? NSNumber)?.int64Value, let h = (o["host_ns"] as? NSNumber)?.int64Value else { return nil }
            self = .pong(tNs: t, hostNs: h)
        case "error": self = .error(code: o["code"] as? String ?? "", message: o["message"] as? String ?? "error")
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
    let phone: [PhoneFrame] = [.hello, .say(utteranceId: "u1", text: "你好 Jarvis", language: "zh", confidence: 0.93, spoken: true),
                               .say(utteranceId: "u2", text: "hi", language: nil, confidence: nil, spoken: false),
                               .say(utteranceId: "u3", text: "等一下", language: "zh", confidence: 0.8, spoken: true, overHer: true, conversation: true),
                               .say(utteranceId: "u4", text: "这个几点", language: "zh", confidence: 0.9, spoken: true, about: Data(#"{"id":"r1","kind":"reminder","start_ms":1,"title":"t"}"#.utf8)),
                               .say(utteranceId: "u5", text: "", language: nil, confidence: nil, spoken: false, attachments: ["a1", "b2"]), .barge, .cancel, .ping(tNs: 1_234_567_890_123)]
    for f in phone { assert(PhoneFrame(f.json) == f, "\(f)") }
    // the flags go on the wire only when true
    let plain = (try? JSONSerialization.jsonObject(with: PhoneFrame.say(utteranceId: "u", text: "x", language: nil, confidence: nil, spoken: true).json)) as? [String: Any]
    assert(plain?["over_her"] == nil && plain?["conversation"] == nil)
    assert(plain?["attachments"] == nil, "no files: no key, so an older host sees the say it knows")
    let flagged = (try? JSONSerialization.jsonObject(with: phone[3].json)) as? [String: Any]
    assert(flagged?["over_her"] as? Bool == true && flagged?["conversation"] as? Bool == true)
    let row = RowFrame(eventType: "surface.response_chunk", eventUid: String(repeating: "a", count: 32),
                       payload: ["turn_id": "T1", "text": "好的", "sequence": 2, "phase": "final"], tsEpochMs: 1_700_000_000_000)
    assert(row.turnId == "T1" && row.text == "好的" && row.phase == "final")
    let host: [HostFrame] = [.ready(device: "phone", voice: true, sampleRate: 32_000, judges: true), .ready(device: "phone", voice: false, sampleRate: 32_000, judges: false),
                             .said(utteranceId: "u1", turnId: "T1", verdict: "turn"), .said(utteranceId: "u4", turnId: nil, verdict: "quiet:night"), .row(row), .pong(tNs: 5, hostNs: 9), .error(code: "retry", message: "again"),
                             .step(StepFrame(turnId: "T1", step: 1, tool: "transit", label: "查公交 · 去 UVic", state: .running, startedMs: 1_700_000_000_000, endedMs: nil)),
                             .step(StepFrame(turnId: "T1", step: 1, tool: "transit", label: "查公交 · 去 UVic", state: .failed, startedMs: 1_700_000_000_000, endedMs: 1_700_000_004_000))]
    for f in host { assert(HostFrame(f.json) == f, "\(f)") }
    // an old host: no judges, no verdict
    assert(HostFrame(Data(#"{"type":"ready","device":"phone","voice":true,"sample_rate":32000}"#.utf8)) == .ready(device: "phone", voice: true, sampleRate: 32_000, judges: false))
    assert(HostFrame(Data(#"{"type":"said","utterance_id":"u1","turn_id":"T1"}"#.utf8)) == .said(utteranceId: "u1", turnId: "T1", verdict: "turn"))
    assert(HostFrame(Data(#"{"type":"said","utterance_id":"u1","turn_id":null,"verdict":"echo"}"#.utf8)) == .said(utteranceId: "u1", turnId: nil, verdict: "echo"))
    // a step the phone cannot read is dropped, not guessed
    assert(HostFrame(Data(#"{"type":"step","turn_id":"T1","step":1,"label":"x","state":"paused"}"#.utf8)) == nil)
}
#endif
