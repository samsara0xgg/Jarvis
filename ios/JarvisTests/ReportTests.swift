import CoreLocation
import HealthKit
import XCTest
@testable import Jarvis

/// The brain's checked contract (jarvis/surface/phone_events.py, ADR 0197), restated here: required and
/// optional payload keys per type, and the enums. A frame the app builds must satisfy all of it.
private enum Contract {
    static let keys: [String: (required: Set<String>, optional: Set<String>)] = [
        "phone.visit_observed": (["lat", "lng", "accuracy_m", "arrived_at_ms"], ["departed_at_ms", "place"]),
        "phone.location_observed": (["lat", "lng", "accuracy_m"], ["place", "speed_mps"]),
        "phone.motion_observed": (["activity", "confidence", "started_at_ms"], ["ended_at_ms"]),
        "phone.health_observed": (["metric", "start_ms", "end_ms", "value"], ["detail", "unit", "source", "device", "sample_uuid", "meta"]),
        "phone.state_observed": (["signal", "value"], ["detail"]),
        "phone.measure_observed": (["metric", "start_ms", "end_ms", "value", "unit", "sample_uuid"], ["source", "device", "meta", "beats", "part"]),
    ]
    static let activities: Set = ["stationary", "walking", "running", "cycling", "automotive", "unknown"]
    static let confidences: Set = ["low", "medium", "high"]
    static let metrics: Set = [
        "steps", "active_energy", "basal_energy", "exercise_time", "stand_time", "distance_walking_running", "flights_climbed", "time_in_daylight",
        "sleep", "workout", "mindful_session", "stand_hour",
    ]
    static let measures: Set = [
        "heart_rate", "resting_heart_rate", "walking_heart_rate_average", "heart_rate_variability_sdnn", "heartbeat_series", "oxygen_saturation",
        "respiratory_rate", "sleeping_wrist_temperature", "vo2_max", "environmental_audio_exposure", "headphone_audio_exposure",
    ]
    static let sleepStages: Set = ["in_bed", "awake", "unspecified", "core", "deep", "rem"]
    static let signals: [String: Set<String>] = [
        "focus": ["on", "off"], "alarm": ["stopped", "snoozed"], "power": ["connected", "disconnected"],
        "headphones": ["connected", "disconnected"], "call": ["started", "ended"],
    ]
    static let msKeys: Set = ["ts_epoch_ms", "arrived_at_ms", "departed_at_ms", "started_at_ms", "ended_at_ms", "start_ms", "end_ms"]

    /// Everything phone_events.payload_valid and frame_shaped check, minus the brain's clock.
    static func check(_ frame: ReportFrame, file: StaticString = #filePath, line: UInt = #line) {
        let text = String(decoding: frame.json, as: UTF8.self)
        XCTAssertFalse(text.contains("null"), "no null on the wire", file: file, line: line)
        guard let obj = try? JSONSerialization.jsonObject(with: frame.json) as? [String: Any],
              let payload = obj["payload"] as? [String: Any], let type = obj["event_type"] as? String else {
            return XCTFail("not a frame", file: file, line: line)
        }
        XCTAssertEqual(Set(obj.keys), ["event_uid", "event_type", "ts_epoch_ms", "payload"], file: file, line: line)
        XCTAssertEqual(obj["event_uid"] as? String, frame.eventUid, file: file, line: line)
        XCTAssertNotNil(frame.eventUid.range(of: "^[0-9a-f]{32}$", options: .regularExpression), file: file, line: line)
        guard let spec = keys[type] else { return XCTFail("unknown type \(type)", file: file, line: line) }
        let present = Set(payload.keys)
        XCTAssertTrue(spec.required.isSubset(of: present), "required fields of \(type)", file: file, line: line)
        XCTAssertTrue(present.isSubset(of: spec.required.union(spec.optional)), "unknown key in \(type)", file: file, line: line)
        for (k, v) in (obj as [String: Any]).merging(payload, uniquingKeysWith: { $1 }) where msKeys.contains(k) {
            let n = v as? NSNumber
            XCTAssertNotNil(n, file: file, line: line)
            XCTAssertFalse(n.map { CFNumberIsFloatType($0) } ?? true, "\(k) is an integer", file: file, line: line)
        }
        for (k, v) in payload {
            if let s = v as? String { XCTAssertLessThanOrEqual(s.count, k == "place" ? 200 : 64 + 200, file: file, line: line) }
            if let n = v as? NSNumber, !(v is Bool) { XCTAssertTrue(n.doubleValue.isFinite, file: file, line: line) }
        }
        if let lat = payload["lat"] as? Double { XCTAssertTrue((-90...90).contains(lat), file: file, line: line) }
        if let lng = payload["lng"] as? Double { XCTAssertTrue((-180...180).contains(lng), file: file, line: line) }
        for k in ["accuracy_m", "speed_mps"] { if let d = payload[k] as? Double { XCTAssertGreaterThanOrEqual(d, 0, file: file, line: line) } }
        if type != "phone.measure_observed", let d = payload["value"] as? Double { XCTAssertGreaterThanOrEqual(d, 0, file: file, line: line) }
        if let u = payload["unit"] as? String { XCTAssertLessThanOrEqual(u.count, 16, file: file, line: line) }
        for k in ["source", "device"] { if let t = payload[k] as? String { XCTAssertLessThanOrEqual(t.count, 128, file: file, line: line) } }
        if let id = payload["sample_uuid"] as? String {
            XCTAssertNotNil(id.range(of: "^[0-9A-Fa-f]{8}(-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}$", options: .regularExpression), file: file, line: line)
        }
        if let meta = payload["meta"] as? [String: Any] {
            XCTAssertTrue((1...12).contains(meta.count), file: file, line: line)
            for (k, v) in meta {
                XCTAssertNotNil(k.range(of: "^[a-z][a-z0-9_]{0,63}$", options: .regularExpression), "meta key \(k)", file: file, line: line)
                XCTAssertTrue(v is String || v is NSNumber, "nothing nested or null in meta", file: file, line: line)
                if let t = v as? String { XCTAssertLessThanOrEqual(t.count, 128, file: file, line: line) }
            }
        }
        switch type {
        case "phone.motion_observed":
            XCTAssertTrue(activities.contains(payload["activity"] as! String), file: file, line: line)
            XCTAssertTrue(confidences.contains(payload["confidence"] as! String), file: file, line: line)
        case "phone.health_observed":
            let metric = payload["metric"] as! String
            XCTAssertTrue(metrics.contains(metric), file: file, line: line)
            if metric == "sleep" { XCTAssertTrue(sleepStages.contains(payload["detail"] as? String ?? ""), "a sleep span names its stage", file: file, line: line) }
            if metric == "stand_hour" { XCTAssertTrue(["stood", "idle"].contains(payload["detail"] as? String ?? ""), file: file, line: line) }
        case "phone.measure_observed":
            let metric = payload["metric"] as! String
            XCTAssertTrue(measures.contains(metric), file: file, line: line)
            let beats = payload["beats"] as? [[Int]]
            if metric == "heartbeat_series" {
                XCTAssertEqual(beats?.count, (payload["value"] as? NSNumber)?.intValue, "value counts the beats", file: file, line: line)
                XCTAssertTrue((1...4000).contains(beats?.count ?? 0), file: file, line: line)
                XCTAssertTrue(beats?.allSatisfy { $0.count == 2 && $0[0] >= 0 && [0, 1].contains($0[1]) } ?? false, file: file, line: line)
            } else {
                XCTAssertNil(beats, file: file, line: line)
                XCTAssertNil(payload["part"], file: file, line: line)
            }
        case "phone.state_observed":
            let signal = payload["signal"] as! String, value = payload["value"] as! String
            if signal == "battery" {
                XCTAssertEqual(Int(value).map(String.init), value, "a whole percent without leading zeros", file: file, line: line)
                XCTAssertTrue((0...100).contains(Int(value) ?? -1), file: file, line: line)
                XCTAssertTrue(["charging", "full", "unplugged"].contains(payload["detail"] as? String ?? "unplugged"), file: file, line: line)
            } else {
                XCTAssertTrue(signals[signal]?.contains(value) == true, file: file, line: line)
            }
        default: break
        }
        for (s, e) in [("arrived_at_ms", "departed_at_ms"), ("started_at_ms", "ended_at_ms"), ("start_ms", "end_ms")] {
            if let a = payload[s] as? Int64, let b = payload[e] as? Int64 { XCTAssertGreaterThanOrEqual(b, a, file: file, line: line) }
        }
    }
}

private let t0 = Date(timeIntervalSince1970: 1_800_000_000)
private let now = t0 + 3 * 3600

private func frame(_ s: Sensed, now: Date = now) -> ReportFrame { ReportFrame(s, now: now)! }

private func samples() -> [Sensed] {
    [
        .visit(lat: 31.2304, lng: 121.4737, accuracyM: 65, arrivedAt: t0, departedAt: nil, place: nil),
        .visit(lat: 31.2304, lng: 121.4737, accuracyM: 65, arrivedAt: t0, departedAt: t0 + 600, place: "Library, Xuhui"),
        .location(lat: 31.2, lng: 121.5, accuracyM: 30, at: t0 + 60, place: nil, speedMps: nil),
        .location(lat: 31.2, lng: 121.5, accuracyM: 30, at: t0 + 90, place: "x", speedMps: 4.5),
        .motion(activity: .walking, confidence: .high, startedAt: t0, endedAt: nil),
        .motion(activity: .automotive, confidence: .low, startedAt: t0, endedAt: t0 + 300),
        .health(metric: .steps, start: t0, end: t0 + 3600, value: 523, detail: nil, extra: HealthExtra(unit: "count")),
        .health(metric: .activeEnergy, start: t0, end: t0 + 3600, value: 41.25, detail: nil, extra: HealthExtra(unit: "kcal")),
        .health(metric: .workout, start: t0, end: t0 + 2400, value: 2400, detail: "running",
                extra: HealthExtra(unit: "s", source: "Allen's Apple Watch", device: "Watch7,1", uuid: "3F2A9C1E-7B3D-4C55-8E21-0A1B2C3D4E5F",
                                   meta: ["indoor": .flag(false), "avg_heart_rate": .number(151.5), "time_zone": .text("America/Vancouver")])),
        .health(metric: .sleep, start: t0, end: t0 + 3600, value: 3, detail: "core", extra: HealthExtra(unit: "category", uuid: "0A1B2C3D-0000-4000-8000-000000000001")),
        .health(metric: .sleep, start: t0, end: t0 + 3600, value: 2, detail: "awake", extra: HealthExtra(unit: "category", uuid: "0A1B2C3D-0000-4000-8000-000000000002")),
        .health(metric: .standHour, start: t0, end: t0 + 3600, value: 0, detail: "stood", extra: HealthExtra(unit: "category", uuid: "0A1B2C3D-0000-4000-8000-000000000003")),
        .health(metric: .mindfulSession, start: t0, end: t0 + 600, value: 0, detail: nil, extra: HealthExtra(unit: "category", uuid: "0A1B2C3D-0000-4000-8000-000000000004")),
        .measure(MeasureRecord(metric: .heartRate, start: t0, end: t0, value: 64, unit: "count/min", uuid: "0A1B2C3D-0000-4000-8000-000000000005",
                               source: "Allen's Apple Watch", device: "Watch7,1", meta: ["motion_context": .text("sedentary")])),
        .measure(MeasureRecord(metric: .sleepingWristTemperature, start: t0, end: t0 + 60, value: -0.4, unit: "degC", uuid: "0A1B2C3D-0000-4000-8000-000000000006")),
        .measure(MeasureRecord(metric: .heartbeatSeries, start: t0, end: t0 + 60, value: 3, unit: "count", uuid: "0A1B2C3D-0000-4000-8000-000000000007",
                               beats: [HeartBeat(offsetMs: 0, gap: true), HeartBeat(offsetMs: 850, gap: false), HeartBeat(offsetMs: 1710, gap: false)])),
        .measure(MeasureRecord(metric: .heartbeatSeries, start: t0, end: t0 + 60, value: 1, unit: "count", uuid: "0A1B2C3D-0000-4000-8000-000000000007",
                               beats: [HeartBeat(offsetMs: 2500, gap: false)], part: 1)),
        .state(.focus(on: true), at: t0, detail: nil),
        .state(.alarm(snoozed: true), at: t0, detail: nil),
        .state(.power(connected: false), at: t0, detail: nil),
        .state(.headphones(connected: true), at: t0, detail: "AirPods Pro"),
        .state(.call(started: true), at: t0, detail: "incoming"),
        .state(.battery(percent: 57), at: t0, detail: "charging"),
        .state(.battery(percent: 100), at: t0, detail: "full"),
        .state(.battery(percent: 0), at: t0, detail: nil),
    ]
}

final class ReportFrameTests: XCTestCase {
    func testEveryShapeMeetsTheBrainsContract() {
        for s in samples() { Contract.check(frame(s)) }
    }

    func testOptionalsAreOmittedNotNull() throws {
        let open = frame(samples()[0])
        let payload = try XCTUnwrap((try JSONSerialization.jsonObject(with: open.json) as? [String: Any])?["payload"] as? [String: Any])
        XCTAssertNil(payload["departed_at_ms"])
        XCTAssertNil(payload["place"])
        XCTAssertNotNil(payload["arrived_at_ms"])
    }

    func testSpeedMinusOneIsOmittedAndARealSpeedIsKept() throws {
        func speed(_ v: Double?) throws -> Any? {
            let f = frame(.location(lat: 1, lng: 2, accuracyM: 5, at: t0, place: nil, speedMps: v))
            return ((try JSONSerialization.jsonObject(with: f.json) as? [String: Any])?["payload"] as? [String: Any])?["speed_mps"]
        }
        XCTAssertNil(try speed(-1))
        XCTAssertNil(try speed(nil))
        XCTAssertEqual(try speed(4.5) as? Double, 4.5)
    }

    func testUidIsStablePerObservationAndNewForANamedVisitOrACorrectedTotal() {
        let a = frame(.visit(lat: 1, lng: 2, accuracyM: 5, arrivedAt: t0, departedAt: nil, place: nil))
        XCTAssertNotEqual(a.eventUid, frame(.visit(lat: 1, lng: 2, accuracyM: 5, arrivedAt: t0, departedAt: nil, place: "Cafe")).eventUid,
                          "the named visit is a second row; the brain keeps the later row of a visit")
        XCTAssertEqual(a, frame(.visit(lat: 1, lng: 2, accuracyM: 5, arrivedAt: t0, departedAt: nil, place: nil)))
        XCTAssertNotEqual(a.eventUid, frame(.visit(lat: 1, lng: 2, accuracyM: 5, arrivedAt: t0, departedAt: t0 + 60, place: nil)).eventUid)
        XCTAssertNotEqual(a.eventUid, frame(.visit(lat: 1, lng: 2.5, accuracyM: 5, arrivedAt: t0, departedAt: nil, place: nil)).eventUid)
        let m1 = frame(.motion(activity: .walking, confidence: .low, startedAt: t0, endedAt: nil))
        XCTAssertEqual(m1.eventUid, frame(.motion(activity: .walking, confidence: .high, startedAt: t0, endedAt: nil)).eventUid)
        XCTAssertNotEqual(m1.eventUid, frame(.motion(activity: .walking, confidence: .low, startedAt: t0, endedAt: t0 + 100)).eventUid)
        let h = frame(.health(metric: .steps, start: t0, end: t0 + 3600, value: 100, detail: nil))
        XCTAssertEqual(h, frame(.health(metric: .steps, start: t0, end: t0 + 3600, value: 100, detail: nil)))
        XCTAssertNotEqual(h.eventUid, frame(.health(metric: .steps, start: t0, end: t0 + 3600, value: 140, detail: nil)).eventUid,
                          "a corrected hourly total is a new frame for the same span, which the brain lets replace the first")
        XCTAssertNotEqual(h.eventUid, frame(.health(metric: .activeEnergy, start: t0, end: t0 + 3600, value: 100, detail: nil)).eventUid)
        // a sample is its Health UUID: the same sample read again is the same frame, whatever else changed
        let uuid = "0A1B2C3D-0000-4000-8000-0000000000AA"
        func sample(_ value: Double, source: String? = nil) -> ReportFrame {
            frame(.health(metric: .workout, start: t0, end: t0 + 60, value: value, detail: "running", extra: HealthExtra(unit: "s", source: source, uuid: uuid)))
        }
        XCTAssertEqual(sample(60).eventUid, sample(61, source: "Watch").eventUid)
        XCTAssertNotEqual(sample(60).eventUid, frame(.health(metric: .workout, start: t0, end: t0 + 60, value: 60, detail: "running",
                                                              extra: HealthExtra(unit: "s", uuid: "0A1B2C3D-0000-4000-8000-0000000000AB"))).eventUid)
        func part(_ n: Int?) -> ReportFrame {
            frame(.measure(MeasureRecord(metric: .heartbeatSeries, start: t0, end: t0 + 60, value: 1, unit: "count", uuid: uuid, beats: [HeartBeat(offsetMs: 1, gap: false)], part: n)))
        }
        XCTAssertNotEqual(part(0).eventUid, part(1).eventUid, "the parts of one series are frames of their own")
        XCTAssertEqual(part(1).eventUid, part(1).eventUid)
        XCTAssertEqual(Set(samples().map { frame($0).eventUid }).count, samples().count, "no two samples share a uid")
    }

    func testWhatTheBrainWouldRefuseIsNeverBuilt() {
        XCTAssertNil(ReportFrame(.location(lat: 91, lng: 0, accuracyM: 1, at: t0, place: nil, speedMps: nil), now: now))
        XCTAssertNil(ReportFrame(.location(lat: 0, lng: 181, accuracyM: 1, at: t0, place: nil, speedMps: nil), now: now))
        XCTAssertNil(ReportFrame(.location(lat: 0, lng: 0, accuracyM: -1, at: t0, place: nil, speedMps: nil), now: now))
        XCTAssertNil(ReportFrame(.location(lat: .nan, lng: 0, accuracyM: 1, at: t0, place: nil, speedMps: nil), now: now))
        XCTAssertNil(ReportFrame(.health(metric: .steps, start: t0, end: t0, value: -1, detail: nil), now: now))
        XCTAssertNil(ReportFrame(.health(metric: .steps, start: t0, end: t0, value: .infinity, detail: nil), now: now))
        // a sleep span without its stage, a stand hour without its state, a sample with a UUID that is not one
        XCTAssertNil(ReportFrame(.health(metric: .sleep, start: t0, end: t0, value: 3, detail: nil), now: now))
        XCTAssertNil(ReportFrame(.health(metric: .sleep, start: t0, end: t0, value: 3, detail: "light"), now: now))
        XCTAssertNil(ReportFrame(.health(metric: .standHour, start: t0, end: t0, value: 0, detail: nil), now: now))
        XCTAssertNil(ReportFrame(.health(metric: .workout, start: t0, end: t0, value: 1, detail: "x", extra: HealthExtra(uuid: "not-a-uuid")), now: now))
        func measure(_ m: MeasureMetric, value: Double = 1, unit: String = "u", uuid: String = "0A1B2C3D-0000-4000-8000-0000000000AC", beats: [HeartBeat] = [], part: Int? = nil) -> ReportFrame? {
            ReportFrame(.measure(MeasureRecord(metric: m, start: t0, end: t0, value: value, unit: unit, uuid: uuid, beats: beats, part: part)), now: now)
        }
        XCTAssertNotNil(measure(.heartRate))
        XCTAssertNil(measure(.heartRate, value: .nan))
        XCTAssertNil(measure(.heartRate, uuid: "nope"))
        XCTAssertNil(measure(.heartRate, unit: " "), "a measure always has its unit")
        XCTAssertNil(measure(.heartRate, beats: [HeartBeat(offsetMs: 0, gap: false)]), "only a series has beats")
        XCTAssertNil(measure(.heartRate, part: 0))
        XCTAssertNil(measure(.heartbeatSeries), "a series without beats")
        XCTAssertNil(measure(.heartbeatSeries, beats: (0...4000).map { HeartBeat(offsetMs: Int64($0), gap: false) }), "more than the brain's 4000")
        XCTAssertNotNil(measure(.heartbeatSeries, beats: (0..<4000).map { HeartBeat(offsetMs: Int64($0), gap: false) }))
    }

    func testTextIsCappedAndTimesAreClamped() throws {
        let long = String(repeating: "长", count: 500)
        let v = frame(.visit(lat: 1, lng: 2, accuracyM: 5, arrivedAt: t0, departedAt: nil, place: long))
        let h = frame(.health(metric: .workout, start: t0, end: t0 + 60, value: 1, detail: long,
                              extra: HealthExtra(unit: long, source: long, device: long, meta: ["note": .text(long), "BadKey": .number(1), "nan": .number(.nan)])))
        Contract.check(v)
        Contract.check(h)
        let payload = try XCTUnwrap((try JSONSerialization.jsonObject(with: h.json) as? [String: Any])?["payload"] as? [String: Any])
        XCTAssertEqual((payload["meta"] as? [String: Any])?.keys.sorted(), ["note"], "a key that is not a lowercase word and a number that is not finite are left out")
        // An end before the start, and times ahead of the clock, are corrected rather than refused.
        let back = frame(.motion(activity: .walking, confidence: .high, startedAt: t0 + 100, endedAt: t0))
        Contract.check(back)
        let future = frame(.state(.focus(on: true), at: now + 3600, detail: nil))
        let ts = try XCTUnwrap((try JSONSerialization.jsonObject(with: future.json) as? [String: Any])?["ts_epoch_ms"] as? Int64)
        XCTAssertEqual(ts, Int64(now.timeIntervalSince1970 * 1000))
    }

    func testEveryStateChangeIsAPairTheBrainAccepts() {
        let all: [StateChange] = [
            .focus(on: true), .focus(on: false), .alarm(snoozed: true), .alarm(snoozed: false),
            .power(connected: true), .power(connected: false), .headphones(connected: true),
            .headphones(connected: false), .call(started: true), .call(started: false),
        ]
        for c in all { XCTAssertTrue(Contract.signals[c.signal]?.contains(c.value) == true, "\(c.signal) \(c.value)") }
        for w in PhoneSignalChange.allCases {
            XCTAssertTrue(Contract.signals[w.change.signal]?.contains(w.change.value) == true)
        }
    }
}

final class BatteryTests: XCTestCase {
    func testTheLevelAndStateAreWhatTheBrainTakes() {
        let r = PowerCollector.reading(level: 0.574, state: .unplugged)
        XCTAssertEqual(r?.percent, 57)
        XCTAssertEqual(r?.detail, "unplugged")
        XCTAssertEqual(PowerCollector.reading(level: 0.996, state: .charging)?.percent, 100)
        XCTAssertEqual(PowerCollector.reading(level: 1, state: .full)?.detail, "full")
        XCTAssertNil(PowerCollector.reading(level: -1, state: .unplugged), "level unknown (a simulator)")
        XCTAssertNil(PowerCollector.reading(level: 0.5, state: .unknown))
    }

    func testAFrameCarriesThePercentAsTextAndTheStateAsDetail() throws {
        let f = frame(.state(.battery(percent: 57), at: t0, detail: "charging"))
        let obj = try XCTUnwrap(try JSONSerialization.jsonObject(with: f.json) as? [String: Any])
        let payload = try XCTUnwrap(obj["payload"] as? [String: Any])
        XCTAssertEqual(payload["signal"] as? String, "battery")
        XCTAssertEqual(payload["value"] as? String, "57")
        XCTAssertEqual(payload["detail"] as? String, "charging")
        XCTAssertNotEqual(f.eventUid, frame(.state(.battery(percent: 56), at: t0, detail: "charging")).eventUid)
        XCTAssertNil(ReportFrame(.state(.battery(percent: 101), at: t0, detail: nil), now: now))
        XCTAssertNil(ReportFrame(.state(.battery(percent: -1), at: t0, detail: nil), now: now))
    }
}

final class ReportBatchTests: XCTestCase {
    private func frames(_ n: Int) -> [ReportFrame] {
        (0..<n).map { frame(.state(.focus(on: $0 % 2 == 0), at: t0 + Double($0), detail: nil)) }
    }

    func testAtMostTwoHundredFramesAreTaken() {
        XCTAssertEqual(ReportBatch.maxFrames, 200)
        XCTAssertEqual(ReportBatch.maxBytes, 1 << 20)
        XCTAssertEqual(ReportBatch.prefix(frames(450)).count, 200)
        XCTAssertEqual(ReportBatch.prefix(frames(7)).count, 7)
    }

    func testTheByteLimitCutsTheBatchAndTheBodyNeverExceedsIt() throws {
        let all = frames(50)
        let limit = 1500
        let batch = ReportBatch.prefix(all, maxBytes: limit)
        XCTAssertGreaterThan(batch.count, 1)
        XCTAssertLessThan(batch.count, 50)
        XCTAssertLessThanOrEqual(ReportBatch.body(batch).count, limit)
        XCTAssertGreaterThan(ReportBatch.body(Array(all.prefix(batch.count + 1))).count, limit)
        XCTAssertEqual(ReportBatch.prefix(all, maxBytes: 10).count, 1, "always progresses")
    }

    func testBodyIsTheEventsObject() throws {
        let body = ReportBatch.body(frames(3))
        let obj = try XCTUnwrap(JSONSerialization.jsonObject(with: body) as? [String: Any])
        XCTAssertEqual(Set(obj.keys), ["events"])
        XCTAssertEqual((obj["events"] as? [Any])?.count, 3)
        XCTAssertEqual(String(decoding: ReportBatch.body([]), as: UTF8.self), #"{"events":[]}"#)
    }

    func testOnlyRetryFramesStay() {
        let f = frames(5)
        let acks = [
            FrameAck(eventUid: f[0].eventUid, ok: true, code: nil),
            FrameAck(eventUid: f[1].eventUid, ok: false, code: "bad_event"),
            FrameAck(eventUid: f[2].eventUid, ok: false, code: "retry"),
            FrameAck(eventUid: f[3].eventUid, ok: false, code: "event_type_not_allowed"),
            FrameAck(eventUid: f[4].eventUid, ok: false, code: "retry"),
        ]
        XCTAssertEqual(ReportBatch.settled(f, acks), [f[0], f[1], f[3]].map(\.eventUid))
        // A short answer is matched by uid; a frame with no answer stays.
        XCTAssertEqual(ReportBatch.settled(f, [acks[4], acks[0], acks[1]]), [f[0], f[1]].map(\.eventUid))
    }

    func testAckDecodesTheBrainsShape() throws {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        let ok = try d.decode(FrameAck.self, from: Data(#"{"type":"ack","event_uid":"ab","ok":true}"#.utf8))
        XCTAssertEqual(ok, FrameAck(eventUid: "ab", ok: true, code: nil))
        let retry = try d.decode(FrameAck.self, from: Data(#"{"type":"ack","event_uid":"cd","ok":false,"code":"retry"}"#.utf8))
        XCTAssertTrue(retry.retry)
        let bad = try d.decode(FrameAck.self, from: Data(#"{"type":"ack","event_uid":null,"ok":false,"code":"bad_event"}"#.utf8))
        XCTAssertFalse(bad.retry)
        XCTAssertNil(bad.eventUid)
    }
}

final class ReportQueueTests: XCTestCase {
    private var dir: URL!

    override func setUp() {
        dir = FileManager.default.temporaryDirectory.appending(path: "report-test-\(UUID().uuidString)")
    }

    override func tearDown() { try? FileManager.default.removeItem(at: dir) }

    private func fr(_ i: Int) -> ReportFrame { frame(.state(.focus(on: true), at: t0 + Double(i), detail: nil)) }

    func testAppendIsOnceAndOldestFirst() {
        let q = ReportQueue(url: dir.appending(path: "q.sqlite"))
        q.append([fr(1), fr(2), fr(3)])
        q.append([fr(2), fr(4)])
        XCTAssertEqual(q.count, 4)
        XCTAssertEqual(q.head().map(\.eventUid), [1, 2, 3, 4].map { fr($0).eventUid })
        XCTAssertEqual(q.head(maxFrames: 2).count, 2)
        q.remove([fr(1).eventUid, fr(3).eventUid])
        XCTAssertEqual(q.head().map(\.eventUid), [2, 4].map { fr($0).eventUid })
    }

    func testPastTheCapTheOldestAreDropped() {
        let q = ReportQueue(url: dir.appending(path: "q.sqlite"), cap: 10)
        q.append((0..<25).map(fr))
        XCTAssertEqual(q.count, 10)
        XCTAssertEqual(q.head().map(\.eventUid), (15..<25).map { fr($0).eventUid })
    }

    func testSurvivesReopeningAndDropsASwitchesFrames() {
        let url = dir.appending(path: "q.sqlite")
        var q: ReportQueue? = ReportQueue(url: url)
        q?.append([fr(1), fr(2)], sense: "location")
        q?.append([fr(3)], sense: "motion")
        q = nil
        let again = ReportQueue(url: url)
        XCTAssertEqual(again.count, 3)
        again.remove(sense: "location")
        XCTAssertEqual(again.head().map(\.eventUid), [fr(3).eventUid])
        XCTAssertEqual(again.head().first?.json, fr(3).json)
    }
}

/// A transport that plays a script of answers and records what it was sent.
private final class ScriptedTransport: ReportTransport {
    var sent: [[String]] = []
    var answer: ([ReportFrame]) throws -> [FrameAck]
    init(_ answer: @escaping ([ReportFrame]) throws -> [FrameAck]) { self.answer = answer }
    func send(_ frames: [ReportFrame]) async throws -> [FrameAck] {
        sent.append(frames.map(\.eventUid))
        return try answer(frames)
    }
}

@MainActor
final class ReporterFlushTests: XCTestCase {
    private var dir: URL!

    override func setUp() async throws {
        dir = FileManager.default.temporaryDirectory.appending(path: "report-test-\(UUID().uuidString)")
    }

    override func tearDown() async throws { try? FileManager.default.removeItem(at: dir) }

    private func reporter() -> Reporter {
        let r = Reporter(queue: ReportQueue(url: dir.appending(path: "q.sqlite")))
        r.autoFlush = false
        return r
    }

    private func events(_ n: Int) -> [Sensed] { (0..<n).map { .state(.focus(on: true), at: Date() - Double($0 + 1), detail: nil) } }

    func testRetryFramesAreSentAgainAndOnlyThose() async {
        let r = reporter()
        r.emit(events(5))
        let uids = r.queue.head().map(\.eventUid)
        let t = ScriptedTransport { frames in
            frames.enumerated().map { i, f in
                switch i {
                case 2, 4: FrameAck(eventUid: f.eventUid, ok: false, code: "retry")
                case 1: FrameAck(eventUid: f.eventUid, ok: false, code: "bad_event")
                default: FrameAck(eventUid: f.eventUid, ok: true, code: nil)
                }
            }
        }
        r.transport = t
        await r.flush()
        XCTAssertEqual(t.sent, [uids])
        XCTAssertEqual(r.queue.head().map(\.eventUid), [uids[2], uids[4]])
        XCTAssertNotNil(r.status.lastSentAt)

        t.answer = { frames in frames.map { FrameAck(eventUid: $0.eventUid, ok: true, code: nil) } }
        await r.flush()
        XCTAssertEqual(t.sent.last, [uids[2], uids[4]], "only the retry frames were resent")
        XCTAssertEqual(r.queue.count, 0)
    }

    func testNothingIsLostWithoutABrainOrOnAFailure() async {
        let r = reporter()
        r.emit(events(3))
        await r.flush()  // no transport yet
        XCTAssertEqual(r.queue.count, 3)
        r.transport = ScriptedTransport { _ in throw URLError(.notConnectedToInternet) }
        await r.flush()
        XCTAssertEqual(r.queue.count, 3)
        XCTAssertEqual(r.status.problem, .offline)
        r.transport = ScriptedTransport { _ in throw ReportTransportError.unauthorized }
        await r.flush()
        XCTAssertEqual(r.queue.count, 3)
        XCTAssertEqual(r.status.problem, .unauthorized)
        r.transport = ScriptedTransport { _ in throw ReportTransportError.notConfigured }
        await r.flush()
        XCTAssertEqual(r.queue.count, 3)
    }

    func testAShortcutsChangeIsSentOnlyWhileItsSwitchIsOn() async throws {
        let was = ReportMemory.isOn(.focusAlarm)
        defer { ReportMemory.set(.focusAlarm, on: was) }
        let r = reporter()
        let t = ScriptedTransport { frames in frames.map { FrameAck(eventUid: $0.eventUid, ok: true, code: nil) } }
        r.transport = t

        ReportMemory.set(.focusAlarm, on: false)
        await r.tell(PhoneSignalChange.alarmSnoozed.change)
        XCTAssertEqual(t.sent.count, 0, "switch off: nothing is queued or sent")
        XCTAssertEqual(r.queue.count, 0)

        ReportMemory.set(.focusAlarm, on: true)
        await r.tell(PhoneSignalChange.alarmSnoozed.change)
        XCTAssertEqual(t.sent.count, 1)
        XCTAssertEqual(r.queue.count, 0)
        // Turning the switch off drops what it had queued but not yet sent.
        r.transport = nil
        await r.tell(PhoneSignalChange.chargerConnected.change)
        XCTAssertEqual(r.queue.count, 1)
        r.queue.remove(sense: ReportSense.focusAlarm.rawValue)
        XCTAssertEqual(r.queue.count, 0)
    }

    func testABatchTheBrainRejectsAsAWholeIsDropped() async {
        let r = reporter()
        r.emit(events(3))
        r.transport = ScriptedTransport { _ in throw ReportTransportError.rejected(413) }
        await r.flush()
        XCTAssertEqual(r.queue.count, 0)
        XCTAssertEqual(r.status.problem, .rejected)
    }

    func testBatchesOfTwoHundredAndEmitDropsWhatTheBrainWouldRefuse() async {
        let r = reporter()
        r.emit(events(450))
        r.emit([.location(lat: 200, lng: 0, accuracyM: 1, at: Date(), place: nil, speedMps: nil)])
        XCTAssertEqual(r.queue.count, 450)
        let t = ScriptedTransport { frames in frames.map { FrameAck(eventUid: $0.eventUid, ok: true, code: nil) } }
        r.transport = t
        await r.flush()
        XCTAssertEqual(t.sent.map(\.count), [200, 200, 50])
        XCTAssertEqual(r.queue.count, 0)
    }
}

/// A transport that always fails the same way and can say what it would send.
private final class FailingTransport: ReportTransport {
    let error: Error
    init(_ error: Error) { self.error = error }
    func send(_ frames: [ReportFrame]) async throws -> [FrameAck] { throw error }
    func backgroundRequest(for frames: [ReportFrame]) -> URLRequest? { URLRequest(url: URL(string: "http://brain.example.ts.net:8765/inherent/device/events")!) }
}

@MainActor
private final class RecordingUploads: ReportUploading {
    var batches: [[String]] = []
    func submit(_ frames: [ReportFrame], request: URLRequest) { batches.append(frames.map(\.eventUid)) }
}

/// Reports reach the brain as soon as the network returns: a batch refused for want of it goes to the background session, whose answer settles
/// the queue exactly as a flush does.
@MainActor
final class BackgroundUploadTests: XCTestCase {
    private var dir: URL!

    override func setUp() async throws {
        dir = FileManager.default.temporaryDirectory.appending(path: "report-test-\(UUID().uuidString)")
    }

    override func tearDown() async throws { try? FileManager.default.removeItem(at: dir) }

    private func reporter() -> (Reporter, RecordingUploads) {
        let r = Reporter(queue: ReportQueue(url: dir.appending(path: "q.sqlite")))
        r.autoFlush = false
        let u = RecordingUploads()
        r.uploads = u
        return (r, u)
    }

    private func events(_ n: Int) -> [Sensed] { (0..<n).map { .state(.focus(on: true), at: Date() - Double($0 + 1), detail: nil) } }

    private func answer(_ acks: [(String, String?)]) -> Data {
        let items = acks.map { uid, code in
            code.map { #"{"type":"ack","event_uid":"\#(uid)","ok":false,"code":"\#($0)"}"# } ?? #"{"type":"ack","event_uid":"\#(uid)","ok":true}"#
        }
        return Data(#"{"acks":[\#(items.joined(separator: ","))]}"#.utf8)
    }

    func testABatchRefusedForWantOfANetworkIsHandedOverAndStaysQueued() async {
        let (r, u) = reporter()
        r.emit(events(3))
        let uids = r.queue.head().map(\.eventUid)
        r.transport = FailingTransport(URLError(.notConnectedToInternet))
        await r.flush()
        XCTAssertEqual(u.batches, [uids])
        XCTAssertEqual(r.queue.count, 3, "the frames leave the queue only with the brain's answer")
        XCTAssertEqual(r.status.problem, .offline)
    }

    func testOnlyAConnectivityFailureIsHandedOver() async {
        let errors: [Error] = [ReportTransportError.server(502), ReportTransportError.unauthorized, ReportTransportError.rejected(413), ReportTransportError.notConfigured]
        for error in errors {
            let (r, u) = reporter()
            r.emit(events(2))
            r.transport = FailingTransport(error)
            await r.flush()
            XCTAssertTrue(u.batches.isEmpty, "\(error)")
        }
    }

    func testNoUploaderMeansTheOldBehaviour() async {
        let (r, _) = reporter()
        r.uploads = nil
        r.emit(events(2))
        r.transport = FailingTransport(URLError(.timedOut))
        await r.flush()
        XCTAssertEqual(r.queue.count, 2)
    }

    func testTheBackgroundAnswerSettlesTheQueueAsAFlushWould() {
        let (r, _) = reporter()
        r.emit(events(5))
        let uids = r.queue.head().map(\.eventUid)
        r.settleBackground(uids: uids, status: 200, body: answer([(uids[0], nil), (uids[1], "bad_event"), (uids[2], "retry"), (uids[3], "event_type_not_allowed"), (uids[4], "retry")]))
        XCTAssertEqual(r.queue.head().map(\.eventUid), [uids[2], uids[4]], "only the retry frames stay")
        XCTAssertNotNil(r.status.lastSentAt)
        XCTAssertNil(r.status.problem)
    }

    func testAShortBackgroundAnswerIsMatchedByUid() {
        let (r, _) = reporter()
        r.emit(events(3))
        let uids = r.queue.head().map(\.eventUid)
        r.settleBackground(uids: uids, status: 200, body: answer([(uids[2], nil), (uids[0], "retry")]))
        XCTAssertEqual(r.queue.head().map(\.eventUid), [uids[0], uids[1]], "a frame with no answer stays")
    }

    func testABackgroundAnswerThatIsNotAnAckKeepsEverything() {
        let (r, _) = reporter()
        r.emit(events(3))
        let uids = r.queue.head().map(\.eventUid)
        r.settleBackground(uids: uids, status: nil, body: Data())                      // the task failed
        r.settleBackground(uids: uids, status: 502, body: Data("{}".utf8))
        r.settleBackground(uids: uids, status: 200, body: Data("not json".utf8))
        XCTAssertEqual(r.queue.count, 3)
        r.settleBackground(uids: uids, status: 401, body: Data())
        XCTAssertEqual(r.queue.count, 3)
        XCTAssertEqual(r.status.problem, .unauthorized)
        r.settleBackground(uids: uids, status: 413, body: Data())
        XCTAssertEqual(r.queue.count, 0, "a batch the brain refuses as a whole is dropped, as a flush drops it")
        XCTAssertEqual(r.status.problem, .rejected)
    }

    func testTheUploadFileCarriesTheBatchAndItsAnswerSettlesItThenTheFileGoes() throws {
        let (r, _) = reporter()
        r.emit(events(4))
        let batch = r.queue.head()
        let file = try XCTUnwrap(ReportUploads.write(ReportBatch.body(batch), id: "b1", in: dir))
        XCTAssertEqual(try Data(contentsOf: file), ReportBatch.body(batch))
        XCTAssertEqual(ReportBatch.uids(inBody: try Data(contentsOf: file)), batch.map(\.eventUid))

        let uploads = ReportUploads()
        uploads.reporter = { r }
        let uids = batch.map(\.eventUid)
        uploads.finished(id: "b1", status: 200, body: answer(uids.map { ($0, nil) }), in: dir)
        XCTAssertEqual(r.queue.count, 0)
        XCTAssertFalse(FileManager.default.fileExists(atPath: file.path))
        // an answer heard twice (a relaunch replays it) finds no file and changes nothing
        r.emit(events(1))
        uploads.finished(id: "b1", status: 200, body: answer(uids.map { ($0, nil) }), in: dir)
        XCTAssertEqual(r.queue.count, 1)
    }

    func testAKilledAppLosesNothing() throws {
        // the app dies after the handover and before any answer: the queue file still holds every frame, and a new process reads them back
        let url = dir.appending(path: "q.sqlite")
        var first: Reporter? = Reporter(queue: ReportQueue(url: url))
        first?.autoFlush = false
        first?.emit(events(3))
        let uids = first!.queue.head().map(\.eventUid)
        first = nil
        let second = Reporter(queue: ReportQueue(url: url))
        XCTAssertEqual(second.queue.head().map(\.eventUid), uids)
        // and the answer the system delivers to the relaunched app settles them
        second.settleBackground(uids: uids, status: 200, body: answer(uids.map { ($0, nil) }))
        XCTAssertEqual(second.queue.count, 0)
    }

    func testOldUploadFilesAreSweptAndNewOnesKept() throws {
        let old = try XCTUnwrap(ReportUploads.write(Data("{}".utf8), id: "old", in: dir))
        let new = try XCTUnwrap(ReportUploads.write(Data("{}".utf8), id: "new", in: dir))
        ReportUploads.sweep(in: dir, olderThan: 3600, now: Date().addingTimeInterval(7200))
        XCTAssertFalse(FileManager.default.fileExists(atPath: old.path))
        XCTAssertFalse(FileManager.default.fileExists(atPath: new.path), "both are older than an hour two hours on")
        let kept = try XCTUnwrap(ReportUploads.write(Data("{}".utf8), id: "kept", in: dir))
        ReportUploads.sweep(in: dir, olderThan: 3600, now: Date())
        XCTAssertTrue(FileManager.default.fileExists(atPath: kept.path))
    }

    func testTheRequestForTheBackgroundIsThePostWithoutABody() throws {
        let t = HTTPReportTransport(endpoint: { (URL(string: "http://brain.example.ts.net:8765")!, "test-token") })
        let req = try XCTUnwrap(t.backgroundRequest(for: [frame(samples()[4])]))
        XCTAssertEqual(req.url?.absoluteString, "http://brain.example.ts.net:8765/inherent/device/events")
        XCTAssertEqual(req.httpMethod, "POST")
        XCTAssertEqual(req.value(forHTTPHeaderField: "Authorization"), "Bearer test-token")
        XCTAssertNil(req.httpBody)
        XCTAssertNil(HTTPReportTransport(endpoint: { nil }).backgroundRequest(for: [frame(samples()[4])]), "unpaired: nothing to hand over")
    }

    func testTheNetworkComingBackFlushesWhatWaited() async throws {
        let (r, _) = reporter()
        r.networkBack()                                         // nothing queued: nothing happens
        r.emit(events(2))
        let t = ScriptedTransport { frames in frames.map { FrameAck(eventUid: $0.eventUid, ok: true, code: nil) } }
        r.transport = t
        r.networkBack()
        for _ in 0..<60 where r.queue.count > 0 { try await Task.sleep(for: .milliseconds(100)) }
        XCTAssertEqual(r.queue.count, 0)
        XCTAssertEqual(t.sent.count, 1)
    }
}

final class HTTPTransportTests: XCTestCase {
    final class Stub: URLProtocol {
        nonisolated(unsafe) static var handler: ((URLRequest, Data) -> (Int, Data))?
        override class func canInit(with request: URLRequest) -> Bool { true }
        override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
        override func startLoading() {
            var body = Data()
            if let s = request.httpBodyStream {
                s.open()
                var buf = [UInt8](repeating: 0, count: 4096)
                while s.hasBytesAvailable { let n = s.read(&buf, maxLength: buf.count); if n <= 0 { break }; body.append(buf, count: n) }
                s.close()
            } else { body = request.httpBody ?? Data() }
            let (status, data) = Stub.handler!(request, body)
            client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        }
        override func stopLoading() {}
    }

    private func transport(endpoint: (() -> HTTPReportTransport.Endpoint?)? = nil) -> HTTPReportTransport {
        let c = URLSessionConfiguration.ephemeral
        c.protocolClasses = [Stub.self]
        return HTTPReportTransport(session: URLSession(configuration: c),
                                   endpoint: endpoint ?? { (URL(string: "http://brain.example.ts.net:8765")!, "test-token") })
    }

    func testRequestShapeAndAcks() async throws {
        let sent = [frame(samples()[4]), frame(samples()[9])]
        nonisolated(unsafe) var seen: (URLRequest, Data)?
        Stub.handler = { req, body in
            seen = (req, body)
            return (200, Data(#"{"acks":[{"type":"ack","event_uid":"\#(sent[0].eventUid)","ok":true},{"type":"ack","event_uid":"\#(sent[1].eventUid)","ok":false,"code":"retry"}]}"#.utf8))
        }
        let acks = try await transport().send(sent)
        let (req, body) = try XCTUnwrap(seen)
        XCTAssertEqual(req.url?.absoluteString, "http://brain.example.ts.net:8765/inherent/device/events")
        XCTAssertEqual(req.httpMethod, "POST")
        XCTAssertEqual(req.value(forHTTPHeaderField: "Authorization"), "Bearer test-token")
        XCTAssertEqual(req.value(forHTTPHeaderField: "Content-Type"), "application/json")
        XCTAssertEqual(body, ReportBatch.body(sent))
        XCTAssertEqual(acks.map(\.ok), [true, false])
        XCTAssertEqual(acks.map(\.retry), [false, true])
    }

    func testStatusesMapToErrors() async {
        func status(_ code: Int) async -> ReportTransportError? {
            Stub.handler = { _, _ in (code, Data("{}".utf8)) }
            do { _ = try await transport().send([frame(samples()[4])]); return nil } catch { return error as? ReportTransportError }
        }
        for c in [401, 403] { let e = await status(c); if case .unauthorized? = e {} else { XCTFail("\(c)") } }
        for c in [400, 413] { let e = await status(c); if case .rejected(c)? = e {} else { XCTFail("\(c)") } }
        let e = await status(502)
        if case .server(502)? = e {} else { XCTFail("502") }
    }

    func testNoEndpointIsNotConfigured() async {
        do { _ = try await transport(endpoint: { nil }).send([]); XCTFail() } catch {
            if case ReportTransportError.notConfigured = error {} else { XCTFail() }
        }
    }
}

final class CollectorLogicTests: XCTestCase {
    private func s(_ min: Double, _ a: Activity, _ c: Confidence = .high) -> MotionSample {
        MotionSample(start: t0 + min * 60, activity: a, confidence: c)
    }

    private func segs(_ o: [Sensed]) -> [(Activity, Confidence, Double, Double?)] {
        o.compactMap {
            if case let .motion(a, c, st, en) = $0 { return (a, c, st.timeIntervalSince(t0) / 60, en.map { $0.timeIntervalSince(t0) / 60 }) }
            return nil
        }
    }

    func testMotionSamplesCollapseIntoSegmentsAndTheLastStaysOpen() {
        let o = MotionCollector.observations([
            s(0, .stationary), s(10, .walking, .low), s(11, .walking, .high), s(20, .walking, .medium),
            s(30, .stationary), s(30.3, .walking), s(31, .stationary),
        ], now: t0 + 40 * 60)
        let r = segs(o)
        XCTAssertEqual(r.count, 3, "the 18 s and 42 s blips are dropped, the rest kept")
        XCTAssertEqual(r[0].0, .stationary); XCTAssertEqual(r[0].3, 10)
        XCTAssertEqual(r[1].0, .walking); XCTAssertEqual(r[1].2, 10); XCTAssertEqual(r[1].3, 30)
        XCTAssertEqual(r[1].1, .medium, "medium covered most of the walk (10 min against 9 high and 1 low)")
        XCTAssertEqual(r[2].0, .stationary); XCTAssertEqual(r[2].2, 31); XCTAssertNil(r[2].3)
        for x in o { Contract.check(frame(x, now: t0 + 40 * 60)) }
    }

    func testCurrentActivityFollowsTheNewestSureSample() {
        XCTAssertEqual(MotionCollector.current([s(0, .stationary), s(5, .automotive)]), .automotive)
        XCTAssertEqual(MotionCollector.current([s(0, .walking), s(5, .stationary)]), .stationary)
        XCTAssertNil(MotionCollector.current([s(0, .walking), s(5, .walking, .low)]))
        XCTAssertNil(MotionCollector.current([s(0, .walking), s(5, .unknown)]))
        XCTAssertNil(MotionCollector.current([]))
    }

    func testActivityTypeFollowsWhatHeIsDoing() {
        XCTAssertEqual(LocationTuning.activityType(for: .automotive), .automotiveNavigation)
        XCTAssertEqual(LocationTuning.activityType(for: .walking), .fitness)
        XCTAssertEqual(LocationTuning.activityType(for: .cycling), .fitness)
        XCTAssertEqual(LocationTuning.activityType(for: nil), .otherNavigation)
    }

    func testATotalIsSentAgainOnlyWhenTheHourlyTotalChanges() {
        func hour(_ h: Double, _ n: Double) -> (start: Date, end: Date, value: Double) { (t0 + h * 3600, t0 + (h + 1) * 3600, n) }
        var sent: [String: Double] = [:]
        func values(_ o: [Sensed]) -> [Double] { o.compactMap { if case let .health(.steps, _, _, v, _, _) = $0 { return v } else { return nil } } }
        func steps(_ b: [(start: Date, end: Date, value: Double)]) -> [Sensed] { HealthCollector.newTotals(.steps, label: "count", b, sent: &sent) }
        XCTAssertEqual(values(steps([hour(0, 120.4), hour(1, 0), hour(2, 800)])), [120, 800], "zero hours are skipped")
        XCTAssertEqual(steps([hour(0, 120.4), hour(2, 800)]).count, 0, "unchanged: nothing")
        XCTAssertEqual(values(steps([hour(0, 300), hour(2, 800)])), [300], "the Watch added steps late")
        XCTAssertEqual(values(steps([hour(1, 40)])), [40])
    }

    func testEnergyKeepsHundredthsAndCarriesItsUnit() throws {
        var sent: [String: Double] = [:]
        let out = HealthCollector.newTotals(.activeEnergy, label: "kcal", [(t0, t0 + 3600, 41.2549)], sent: &sent)
        guard case let .health(.activeEnergy, _, _, v, _, extra)? = out.first else { return XCTFail() }
        XCTAssertEqual(v, 41.25)
        XCTAssertEqual(extra.unit, "kcal")
        XCTAssertNil(extra.uuid, "an hourly total is not a Health sample")
        XCTAssertEqual(HealthCollector.totals.count, 8)
        XCTAssertEqual(Set(HealthCollector.totals.map(\.id)).count, 8)
    }

    func testEveryHealthTypeIsReadAndEveryMetricHasItsStream() {
        let sent = Set(HealthCollector.totals.compactMap { if case .total(let m, _, _) = $0.shape { m.rawValue } else { nil } })
        XCTAssertEqual(sent, Set(HealthMetric.allCases.filter(\.isTotal).map(\.rawValue)))
        var measures: Set<String> = []
        var spans: Set<String> = []
        for s in HealthCollector.records {
            switch s.shape {
            case .quantity(let m, _, _): measures.insert(m.rawValue)
            case .heartbeats: measures.insert(MeasureMetric.heartbeatSeries.rawValue)
            case .sleep: spans.insert("sleep")
            case .workout: spans.insert("workout")
            case .mindful: spans.insert("mindful_session")
            case .standHour: spans.insert("stand_hour")
            case .total: XCTFail("a total among the samples")
            }
        }
        XCTAssertEqual(measures, Set(MeasureMetric.allCases.map(\.rawValue)))
        XCTAssertEqual(spans, Set(HealthMetric.allCases.filter { !$0.isTotal }.map(\.rawValue)))
        let ids = (HealthCollector.totals + HealthCollector.records).map(\.id)
        XCTAssertEqual(Set(ids).count, ids.count, "an anchor and a start are kept per id")
    }

    func testSleepStagesAreNamedAsRecordedAndAnUnknownOneIsNotInvented() {
        XCTAssertEqual([0, 1, 2, 3, 4, 5].map(HealthCollector.sleepStage), ["in_bed", "unspecified", "awake", "core", "deep", "rem"])
        XCTAssertNil(HealthCollector.sleepStage(9))
    }

    func testMetadataKeepsWhatMeansSomething() {
        let md: [String: Any] = [
            HKMetadataKeyTimeZone: "America/Vancouver", HKMetadataKeyWasUserEntered: true, HKMetadataKeyIndoorWorkout: NSNumber(value: false),
            HKMetadataKeyHeartRateMotionContext: NSNumber(value: HKHeartRateMotionContext.active.rawValue), "HKSomethingElse": "left behind",
        ]
        XCTAssertEqual(HealthCollector.meta(md), ["time_zone": .text("America/Vancouver"), "user_entered": .flag(true), "indoor": .flag(false), "motion_context": .text("active")])
        XCTAssertEqual(HealthCollector.meta([HKMetadataKeyHeartRateMotionContext: NSNumber(value: 0)])["motion_context"], .text("not_set"))
        XCTAssertEqual(HealthCollector.meta(nil), [:])
    }

    func testALongSeriesIsCutIntoPartsThatEachFitAFrame() throws {
        let beats = (0..<7000).map { HeartBeat(offsetMs: Int64($0) * 800, gap: $0 % 500 == 0) }
        let frames = HealthCollector.seriesFrames(uuid: "0A1B2C3D-0000-4000-8000-0000000000AD", start: t0, end: t0 + 5600, beats, source: "Watch", device: "Watch7,1", meta: [:])
        XCTAssertEqual(frames.count, 3)
        var joined: [HeartBeat] = []
        var uids: Set<String> = []
        for (i, f) in frames.enumerated() {
            guard case let .measure(m) = f else { return XCTFail() }
            XCTAssertEqual(m.part, i)
            XCTAssertLessThanOrEqual(m.beats.count, ReportFrame.maxBeats)
            joined += m.beats
            let r = try XCTUnwrap(ReportFrame(f, now: now))
            Contract.check(r)
            XCTAssertLessThan(r.json.count, 64 * 1024, "a frame is under the brain's 64 KiB")
            uids.insert(r.eventUid)
        }
        XCTAssertEqual(joined, beats, "offsets are from the series' start, in order, nothing lost")
        XCTAssertEqual(uids.count, 3)
        let one = HealthCollector.seriesFrames(uuid: "0A1B2C3D-0000-4000-8000-0000000000AE", start: t0, end: t0 + 60, Array(beats.prefix(50)), source: nil, device: nil, meta: [:])
        guard case let .measure(single)? = one.first else { return XCTFail() }
        XCTAssertEqual(one.count, 1)
        XCTAssertNil(single.part)
        XCTAssertTrue(HealthCollector.seriesFrames(uuid: "0A1B2C3D-0000-4000-8000-0000000000AF", start: t0, end: t0, [], source: nil, device: nil, meta: [:]).isEmpty)
    }

    func testTheQueueHoldsADaysOfHeartRate() {
        XCTAssertGreaterThanOrEqual(ReportQueue.defaultCap, 50_000)
    }

    func testThinningKeepsAFixEvery30sAnd50m() {
        let a = CLLocation(coordinate: .init(latitude: 31, longitude: 121), altitude: 0, horizontalAccuracy: 50, verticalAccuracy: -1, timestamp: t0)
        func fix(dLat: Double, dt: TimeInterval) -> CLLocation {
            CLLocation(coordinate: .init(latitude: 31 + dLat, longitude: 121), altitude: 0, horizontalAccuracy: 50, verticalAccuracy: -1, timestamp: t0 + dt)
        }
        XCTAssertTrue(LocationTuning.passesThinning(a, after: nil))
        XCTAssertFalse(LocationTuning.passesThinning(fix(dLat: 0.001, dt: 10), after: a), "111 m but too soon")
        XCTAssertFalse(LocationTuning.passesThinning(fix(dLat: 0.0002, dt: 60), after: a), "late enough but only 22 m")
        XCTAssertTrue(LocationTuning.passesThinning(fix(dLat: 0.001, dt: 31), after: a))
    }

    func testSettingsTitlesMapToSenses() {
        XCTAssertEqual(ReportSense.allCases.compactMap { ReportSense(title: $0.title) }, ReportSense.allCases)
        XCTAssertNil(ReportSense(title: "分享给 Jarvis"))
    }

    func testStateChangedSkipsQuietFirstLookAndRepeats() {
        let key = "test.\(UUID().uuidString)"
        XCTAssertFalse(ReportMemory.stateChanged(key, to: "off", quiet: "off"), "first look at the quiet value is not news")
        XCTAssertFalse(ReportMemory.stateChanged(key, to: "off", quiet: "off"))
        XCTAssertTrue(ReportMemory.stateChanged(key, to: "on", quiet: "off"))
        XCTAssertFalse(ReportMemory.stateChanged(key, to: "on", quiet: "off"))
        XCTAssertTrue(ReportMemory.stateChanged(key, to: "off", quiet: "off"))
        let other = "test.\(UUID().uuidString)"
        XCTAssertTrue(ReportMemory.stateChanged(other, to: "on", quiet: "off"), "first look at an active value is")
    }
}
