import CryptoKit
import Foundation

// What the phone senses, as the event frames the brain accepts (ADR 0197).
// The enums, field names and ranges below are the brain's checked ones (jarvis/surface/phone_events.py);
// anything outside them is refused there as `bad_event`, so the phone never builds it.

enum Activity: String { case stationary, walking, running, cycling, automotive, unknown }
enum Confidence: String { case low, medium, high }

/// Only the metrics the app collects; the brain also accepts distance_m, active_kcal and exercise_min.
enum HealthMetric: String {
    case steps
    case workoutMin = "workout_min"
    case sleepMin = "sleep_min"
}

/// A state change; `signal` and `value` only come in the pairs the brain accepts.
enum StateChange {
    case focus(on: Bool)
    case alarm(snoozed: Bool)
    case power(connected: Bool)
    case headphones(connected: Bool)
    case call(started: Bool)

    var signal: String {
        switch self {
        case .focus: "focus"
        case .alarm: "alarm"
        case .power: "power"
        case .headphones: "headphones"
        case .call: "call"
        }
    }

    var value: String {
        switch self {
        case .focus(let on): on ? "on" : "off"
        case .alarm(let snoozed): snoozed ? "snoozed" : "stopped"
        case .power(let connected), .headphones(let connected): connected ? "connected" : "disconnected"
        case .call(let started): started ? "started" : "ended"
        }
    }
}

enum Sensed {
    case visit(lat: Double, lng: Double, accuracyM: Double, arrivedAt: Date, departedAt: Date?, place: String?)
    /// `requestId`: the brain asked for this fix (a silent push, Map/LocatePush); it comes back on the frame so the brain knows which request it answers
    case location(lat: Double, lng: Double, accuracyM: Double, at: Date, place: String?, speedMps: Double?, requestId: String? = nil)
    case motion(activity: Activity, confidence: Confidence, startedAt: Date, endedAt: Date?)
    case health(metric: HealthMetric, start: Date, end: Date, value: Double, detail: String?)
    case state(StateChange, at: Date, detail: String?)
}

/// One encoded event frame. The bytes are what the queue stores and the batch sends, so a resend is the same
/// frame with the same `event_uid`.
struct ReportFrame: Equatable {
    let eventUid: String
    let json: Data
}

// Optionals stay nil-and-omitted: the synthesized encoder skips them, so no frame ever carries a null.
// Keys are snake_cased by the encoder (arrivedAtMs -> arrived_at_ms).
private struct VisitPayload: Encodable {
    let lat, lng, accuracyM: Double
    let arrivedAtMs: Int64
    let departedAtMs: Int64?
    let place: String?
}

private struct LocationPayload: Encodable {
    let lat, lng, accuracyM: Double
    let place: String?
    let speedMps: Double?
    let requestId: String?
}

private struct MotionPayload: Encodable {
    let activity, confidence: String
    let startedAtMs: Int64
    let endedAtMs: Int64?
}

private struct HealthPayload: Encodable {
    let metric: String
    let startMs, endMs: Int64
    let value: Double
    let detail: String?
}

private struct StatePayload: Encodable {
    let signal, value: String
    let detail: String?
}

extension ReportFrame {
    static let maxPlaceChars = 200
    static let maxDetailChars = 64
    static let maxRequestIdChars = 64

    private static let encoder: JSONEncoder = {
        let e = JSONEncoder()
        e.keyEncodingStrategy = .convertToSnakeCase
        e.outputFormatting = .sortedKeys  // the same observation encodes to the same bytes
        return e
    }()

    private static func ms(_ d: Date) -> Int64 { Int64((d.timeIntervalSince1970 * 1000).rounded()) }

    /// The brain refuses a time more than five minutes ahead of its clock; a clock that far off is clamped to now.
    private static func at(_ d: Date, _ now: Date) -> Int64 { ms(min(d, now)) }

    private static func text(_ s: String?, _ limit: Int) -> String? {
        guard let s = s?.trimmingCharacters(in: .whitespacesAndNewlines), !s.isEmpty else { return nil }
        return String(s.prefix(limit))
    }

    private static func coord(_ v: Double) -> String { String(format: "%.6f", v) }

    /// The frame for an observation, or nil when it is outside what the brain accepts (it would be refused).
    /// `event_uid` is a hash of the observation's own identity, never of the clock at encoding time, so seeing the
    /// same thing again gives the same uid and the brain stores it once. It leaves out a motion segment's
    /// confidence (a re-read of the open segment may judge it differently). It includes a visit's place name and
    /// a Health value: a named visit and a corrected total are new rows with the same span, and the brain keeps
    /// the later row for that span instead of storing the first one forever.
    init?(_ observation: Sensed, now: Date = Date()) {
        let type: String, key: String, ts: Int64, payload: Data
        do {
            switch observation {
            case let .visit(lat, lng, acc, arrived, departed, place):
                guard Self.validPlace(lat, lng, acc) else { return nil }
                let a = Self.at(arrived, now)
                let d = departed.map { max(Self.at($0, now), a) }
                let name = Self.text(place, Self.maxPlaceChars)
                type = "phone.visit_observed"
                key = "\(Self.coord(lat)),\(Self.coord(lng)),\(a),\(d.map(String.init) ?? "open"),\(name ?? "")"
                ts = d ?? a
                payload = try Self.encoder.encode(VisitPayload(
                    lat: lat, lng: lng, accuracyM: acc, arrivedAtMs: a, departedAtMs: d, place: name))
            case let .location(lat, lng, acc, when, place, speed, requestId):
                guard Self.validPlace(lat, lng, acc) else { return nil }
                let t = Self.at(when, now)
                type = "phone.location_observed"
                key = "\(Self.coord(lat)),\(Self.coord(lng)),\(t)" + (requestId.map { ",\($0)" } ?? "")
                ts = t
                payload = try Self.encoder.encode(LocationPayload(
                    lat: lat, lng: lng, accuracyM: acc, place: Self.text(place, Self.maxPlaceChars),
                    speedMps: speed.flatMap { $0.isFinite && $0 >= 0 ? $0 : nil }, requestId: Self.text(requestId, Self.maxRequestIdChars)))
            case let .motion(activity, confidence, started, ended):
                let s = Self.at(started, now)
                let e = ended.map { max(Self.at($0, now), s) }
                type = "phone.motion_observed"
                key = "\(activity.rawValue),\(s),\(e.map(String.init) ?? "open")"
                ts = e ?? s
                payload = try Self.encoder.encode(MotionPayload(
                    activity: activity.rawValue, confidence: confidence.rawValue, startedAtMs: s, endedAtMs: e))
            case let .health(metric, start, end, value, detail):
                guard value.isFinite, value >= 0 else { return nil }
                let s = Self.at(start, now)
                let e = max(Self.at(end, now), s)
                type = "phone.health_observed"
                key = "\(metric.rawValue),\(s),\(e),\(String(format: "%.2f", value))"
                ts = e  // the end of the sensed span, never the upload time
                payload = try Self.encoder.encode(HealthPayload(
                    metric: metric.rawValue, startMs: s, endMs: e, value: value,
                    detail: Self.text(detail, Self.maxDetailChars)))
            case let .state(change, when, detail):
                let t = Self.at(when, now)
                type = "phone.state_observed"
                key = "\(change.signal),\(change.value),\(t)"
                ts = t
                payload = try Self.encoder.encode(StatePayload(
                    signal: change.signal, value: change.value, detail: Self.text(detail, Self.maxDetailChars)))
            }
        } catch {
            return nil
        }
        let uid = Self.uid(type: type, key: key)
        // The envelope is assembled by hand so the payload bytes are exactly the ones encoded above.
        var out = Data(#"{"event_uid":""#.utf8)
        out.append(Data(uid.utf8))
        out.append(Data(#"","event_type":""#.utf8))
        out.append(Data(type.utf8))
        out.append(Data(#"","ts_epoch_ms":"#.utf8))
        out.append(Data(String(ts).utf8))
        out.append(Data(#","payload":"#.utf8))
        out.append(payload)
        out.append(Data("}".utf8))
        self.init(eventUid: uid, json: out)
    }

    private static func validPlace(_ lat: Double, _ lng: Double, _ accuracy: Double) -> Bool {
        lat.isFinite && lng.isFinite && accuracy.isFinite && (-90...90).contains(lat) && (-180...180).contains(lng)
            && accuracy >= 0
    }

    /// 32 lowercase hex characters.
    static func uid(type: String, key: String) -> String {
        SHA256.hash(data: Data("\(type)|\(key)".utf8)).prefix(16).map { String(format: "%02x", $0) }.joined()
    }
}

/// The brain's answer for one frame (the `ack` of ADR 0197): `ok`, or refused with a `code`.
struct FrameAck: Decodable, Equatable {
    var eventUid: String?
    var ok: Bool
    var code: String?

    /// Only a database error on that frame is worth sending again; every other answer is final.
    var retry: Bool { !ok && code == "retry" }
}

/// Batch building and the ack rule. The limits are the brain's (`MAX_BATCH_FRAMES`, `MAX_BATCH_BYTES`).
enum ReportBatch {
    static let maxFrames = 200
    static let maxBytes = 1024 * 1024

    private static let head = Data(#"{"events":["#.utf8)
    private static let tail = Data("]}".utf8)

    /// `{"events":[frame,...]}`
    static func body(_ frames: [ReportFrame]) -> Data {
        var out = head
        for (i, f) in frames.enumerated() {
            if i > 0 { out.append(0x2C) }
            out.append(f.json)
        }
        out.append(tail)
        return out
    }

    /// The longest prefix of `frames` that fits both limits (at least one frame, so a flush always progresses).
    static func prefix(_ frames: [ReportFrame], maxFrames: Int = maxFrames, maxBytes: Int = maxBytes) -> [ReportFrame] {
        var size = head.count + tail.count
        var n = 0
        for f in frames.prefix(maxFrames) {
            let add = f.json.count + (n > 0 ? 1 : 0)
            if n > 0 && size + add > maxBytes { break }
            size += add
            n += 1
        }
        return Array(frames.prefix(n))
    }

    /// The uids to drop after a batch was answered: every frame acked ok or refused. A frame acked `retry`,
    /// or without an answer, stays. Acks come one per frame in request order; a short answer is matched by uid.
    static func settled(_ frames: [ReportFrame], _ acks: [FrameAck]) -> [String] {
        if acks.count == frames.count {
            return zip(frames, acks).filter { !$0.1.retry }.map(\.0.eventUid)
        }
        let answered = Dictionary(acks.compactMap { a in a.eventUid.map { ($0, a) } }, uniquingKeysWith: { $1 })
        return frames.filter { answered[$0.eventUid].map { !$0.retry } ?? false }.map(\.eventUid)
    }
}
