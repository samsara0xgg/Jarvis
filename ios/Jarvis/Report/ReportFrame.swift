import CryptoKit
import Foundation

// What the phone senses, as the event frames the brain accepts (ADR 0197).
// The enums, field names and ranges below are the brain's checked ones (jarvis/surface/phone_events.py);
// anything outside them is refused there as `bad_event`, so the phone never builds it.

enum Activity: String { case stationary, walking, running, cycling, automotive, unknown }
enum Confidence: String { case low, medium, high }

/// What `phone.health_observed` carries (ADR 0293): hourly totals of cumulative quantities, and spans exactly as Health recorded them, one per sample.
enum HealthMetric: String, CaseIterable {
    // hourly totals: Health merges iPhone and Watch only in statistics, so the raw samples of these are never summed
    case steps
    case activeEnergy = "active_energy"
    case basalEnergy = "basal_energy"
    case exerciseTime = "exercise_time"
    case standTime = "stand_time"
    case distanceWalkingRunning = "distance_walking_running"
    case flightsClimbed = "flights_climbed"
    case timeInDaylight = "time_in_daylight"
    // samples
    case sleep, workout
    case mindfulSession = "mindful_session"
    case standHour = "stand_hour"

    var isTotal: Bool {
        switch self {
        case .sleep, .workout, .mindfulSession, .standHour: false
        default: true
        }
    }
}

/// What `phone.measure_observed` carries: a measurement of the body, one frame per Health sample.
enum MeasureMetric: String, CaseIterable {
    case heartRate = "heart_rate"
    case restingHeartRate = "resting_heart_rate"
    case walkingHeartRateAverage = "walking_heart_rate_average"
    case heartRateVariabilitySDNN = "heart_rate_variability_sdnn"
    case heartbeatSeries = "heartbeat_series"
    case oxygenSaturation = "oxygen_saturation"
    case respiratoryRate = "respiratory_rate"
    case sleepingWristTemperature = "sleeping_wrist_temperature"
    case vo2Max = "vo2_max"
    case environmentalAudioExposure = "environmental_audio_exposure"
    case headphoneAudioExposure = "headphone_audio_exposure"
}

/// A value of `meta`: the source's own words, as text, a number or a flag. Nothing nested, nothing null.
enum MetaValue: Equatable, Encodable {
    case text(String)
    case number(Double)
    case flag(Bool)

    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .text(let s): try c.encode(s)
        case .number(let d): try c.encode(d)
        case .flag(let b): try c.encode(b)
        }
    }
}

/// What the source recorded about a Health sample beyond its span and value. All optional; an hourly total has none of it.
struct HealthExtra: Equatable {
    var unit: String?
    /// the app or device that wrote the sample
    var source: String?
    var device: String?
    /// Health's own UUID of the sample: the identity of the frame
    var uuid: String?
    var meta: [String: MetaValue] = [:]
    static let none = HealthExtra()
}

/// One beat of a heartbeat series: when it came after the series began, and whether Health marks a gap before it.
struct HeartBeat: Equatable {
    var offsetMs: Int64
    var gap: Bool
}

struct MeasureRecord: Equatable {
    var metric: MeasureMetric
    var start: Date
    var end: Date
    var value: Double
    var unit: String
    var uuid: String
    var source: String?
    var device: String?
    var meta: [String: MetaValue] = [:]
    /// a heartbeat series: its beats (`value` is their count); at most `ReportFrame.maxBeats` in one frame
    var beats: [HeartBeat] = []
    /// the n-th frame of a series too long for one
    var part: Int?
}

/// A state change; `signal` and `value` only come in the pairs the brain accepts.
enum StateChange {
    case focus(on: Bool)
    case alarm(snoozed: Bool)
    case power(connected: Bool)
    case headphones(connected: Bool)
    case call(started: Bool)
    /// The charge in whole percent; the charging state goes along as the frame's detail (`charging`, `full`, `unplugged`)
    case battery(percent: Int)

    var signal: String {
        switch self {
        case .focus: "focus"
        case .alarm: "alarm"
        case .power: "power"
        case .headphones: "headphones"
        case .call: "call"
        case .battery: "battery"
        }
    }

    var value: String {
        switch self {
        case .focus(let on): on ? "on" : "off"
        case .alarm(let snoozed): snoozed ? "snoozed" : "stopped"
        case .power(let connected), .headphones(let connected): connected ? "connected" : "disconnected"
        case .call(let started): started ? "started" : "ended"
        case .battery(let percent): String(percent)
        }
    }
}

enum Sensed {
    case visit(lat: Double, lng: Double, accuracyM: Double, arrivedAt: Date, departedAt: Date?, place: String?)
    /// `requestId`: the brain asked for this fix (a silent push, Map/LocatePush); it comes back on the frame so the brain knows which request it answers
    case location(lat: Double, lng: Double, accuracyM: Double, at: Date, place: String?, speedMps: Double?, requestId: String? = nil)
    case motion(activity: Activity, confidence: Confidence, startedAt: Date, endedAt: Date?)
    case health(metric: HealthMetric, start: Date, end: Date, value: Double, detail: String?, extra: HealthExtra = .none)
    case measure(MeasureRecord)
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
    let unit, source, device, sampleUuid: String?
    let meta: [String: MetaValue]?
}

private struct MeasurePayload: Encodable {
    let metric: String
    let startMs, endMs: Int64
    let value: Double
    let unit, sampleUuid: String
    let source, device: String?
    let meta: [String: MetaValue]?
    let beats: [[Int64]]?
    let part: Int?
}

private struct StatePayload: Encodable {
    let signal, value: String
    let detail: String?
}

extension ReportFrame {
    static let maxPlaceChars = 200
    static let maxDetailChars = 64
    static let maxRequestIdChars = 64
    static let maxUnitChars = 16
    static let maxSourceChars = 128
    static let maxMetaKeys = 12
    static let maxMetaChars = 128
    /// Beats in one frame (the brain's cap); a longer series is cut into parts
    static let maxBeats = 4000
    static let sleepStages: Set = ["in_bed", "awake", "unspecified", "core", "deep", "rem"]
    static let standStates: Set = ["stood", "idle"]

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

    private static func validUUID(_ s: String?) -> Bool {
        guard let s else { return true }
        return s.range(of: "^[0-9A-Fa-f]{8}(-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}$", options: .regularExpression) != nil
    }

    /// The `meta` the brain takes: lowercase word keys, text, finite numbers and flags, at most `maxMetaKeys` (the first by key); nil when empty.
    static func meta(_ m: [String: MetaValue]) -> [String: MetaValue]? {
        var out: [String: MetaValue] = [:]
        for key in m.keys.sorted() where out.count < maxMetaKeys {
            guard key.range(of: "^[a-z][a-z0-9_]{0,63}$", options: .regularExpression) != nil else { continue }
            switch m[key]! {
            case .text(let t): if let t = text(t, maxMetaChars) { out[key] = .text(t) }
            case .number(let d): if d.isFinite { out[key] = .number(d) }
            case .flag(let b): out[key] = .flag(b)
            }
        }
        return out.isEmpty ? nil : out
    }

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
            case let .health(metric, start, end, value, detail, extra):
                guard value.isFinite, value >= 0, Self.validUUID(extra.uuid) else { return nil }
                let name = Self.text(detail, Self.maxDetailChars)
                if metric == .sleep && !Self.sleepStages.contains(name ?? "") { return nil }
                if metric == .standHour && !Self.standStates.contains(name ?? "") { return nil }
                let s = Self.at(start, now)
                let e = max(Self.at(end, now), s)
                type = "phone.health_observed"
                // a sample is its Health UUID; an hourly total is its span and value (a corrected total is a new frame for the same span)
                key = extra.uuid ?? "\(metric.rawValue),\(s),\(e),\(String(format: "%.2f", value))"
                ts = e  // the end of the sensed span, never the upload time
                payload = try Self.encoder.encode(HealthPayload(
                    metric: metric.rawValue, startMs: s, endMs: e, value: value, detail: name,
                    unit: Self.text(extra.unit, Self.maxUnitChars), source: Self.text(extra.source, Self.maxSourceChars),
                    device: Self.text(extra.device, Self.maxSourceChars), sampleUuid: extra.uuid, meta: Self.meta(extra.meta)))
            case let .measure(m):
                guard m.value.isFinite, Self.validUUID(m.uuid), let unit = Self.text(m.unit, Self.maxUnitChars) else { return nil }
                let isSeries = m.metric == .heartbeatSeries
                guard m.beats.count <= Self.maxBeats, isSeries == !m.beats.isEmpty, isSeries || m.part == nil,
                      m.part.map({ (0..<1000).contains($0) }) ?? true else { return nil }
                let s = Self.at(m.start, now)
                let e = max(Self.at(m.end, now), s)
                type = "phone.measure_observed"
                key = m.uuid + (m.part.map { ",\($0)" } ?? "")
                ts = e
                payload = try Self.encoder.encode(MeasurePayload(
                    metric: m.metric.rawValue, startMs: s, endMs: e, value: isSeries ? Double(m.beats.count) : m.value, unit: unit,
                    sampleUuid: m.uuid, source: Self.text(m.source, Self.maxSourceChars), device: Self.text(m.device, Self.maxSourceChars),
                    meta: Self.meta(m.meta), beats: isSeries ? m.beats.map { [max($0.offsetMs, 0), $0.gap ? 1 : 0] } : nil, part: m.part))
            case let .state(change, when, detail):
                if case .battery(let percent) = change, !(0...100).contains(percent) { return nil }
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
    static func settled(_ frames: [ReportFrame], _ acks: [FrameAck]) -> [String] { settled(uids: frames.map(\.eventUid), acks) }

    /// The same for a batch known only by its uids in request order (one the background session delivered).
    static func settled(uids: [String], _ acks: [FrameAck]) -> [String] {
        if acks.count == uids.count {
            return zip(uids, acks).filter { !$0.1.retry }.map(\.0)
        }
        let answered = Dictionary(acks.compactMap { a in a.eventUid.map { ($0, a) } }, uniquingKeysWith: { $1 })
        return uids.filter { answered[$0].map { !$0.retry } ?? false }
    }

    /// The uids of the frames in a request body written by `body`, in order; empty if it is not one.
    static func uids(inBody body: Data) -> [String] {
        guard let obj = try? JSONSerialization.jsonObject(with: body) as? [String: Any], let events = obj["events"] as? [[String: Any]] else { return [] }
        return events.compactMap { $0["event_uid"] as? String }
    }
}
