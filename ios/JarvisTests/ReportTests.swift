import CoreLocation
import XCTest
@testable import Jarvis

/// The brain's checked contract (jarvis/surface/phone_events.py, ADR 0197), restated here: required and
/// optional payload keys per type, and the enums. A frame the app builds must satisfy all of it.
private enum Contract {
    static let keys: [String: (required: Set<String>, optional: Set<String>)] = [
        "phone.visit_observed": (["lat", "lng", "accuracy_m", "arrived_at_ms"], ["departed_at_ms", "place"]),
        "phone.location_observed": (["lat", "lng", "accuracy_m"], ["place", "speed_mps"]),
        "phone.motion_observed": (["activity", "confidence", "started_at_ms"], ["ended_at_ms"]),
        "phone.health_observed": (["metric", "start_ms", "end_ms", "value"], ["detail"]),
        "phone.state_observed": (["signal", "value"], ["detail"]),
    ]
    static let activities: Set = ["stationary", "walking", "running", "cycling", "automotive", "unknown"]
    static let confidences: Set = ["low", "medium", "high"]
    static let metrics: Set = ["steps", "distance_m", "active_kcal", "exercise_min", "sleep_min", "workout_min"]
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
        for k in ["accuracy_m", "speed_mps", "value"] { if let d = payload[k] as? Double { XCTAssertGreaterThanOrEqual(d, 0, file: file, line: line) } }
        switch type {
        case "phone.motion_observed":
            XCTAssertTrue(activities.contains(payload["activity"] as! String), file: file, line: line)
            XCTAssertTrue(confidences.contains(payload["confidence"] as! String), file: file, line: line)
        case "phone.health_observed":
            XCTAssertTrue(metrics.contains(payload["metric"] as! String), file: file, line: line)
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
        .health(metric: .steps, start: t0, end: t0 + 3600, value: 523, detail: nil),
        .health(metric: .workoutMin, start: t0, end: t0 + 2400, value: 40, detail: "running"),
        .health(metric: .sleepMin, start: t0, end: t0 + 25_000, value: 410.5, detail: nil),
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
        XCTAssertNotEqual(h.eventUid, frame(.health(metric: .sleepMin, start: t0, end: t0 + 3600, value: 100, detail: nil)).eventUid)
        XCTAssertEqual(Set(samples().map { frame($0).eventUid }).count, samples().count, "no two samples share a uid")
    }

    func testWhatTheBrainWouldRefuseIsNeverBuilt() {
        XCTAssertNil(ReportFrame(.location(lat: 91, lng: 0, accuracyM: 1, at: t0, place: nil, speedMps: nil), now: now))
        XCTAssertNil(ReportFrame(.location(lat: 0, lng: 181, accuracyM: 1, at: t0, place: nil, speedMps: nil), now: now))
        XCTAssertNil(ReportFrame(.location(lat: 0, lng: 0, accuracyM: -1, at: t0, place: nil, speedMps: nil), now: now))
        XCTAssertNil(ReportFrame(.location(lat: .nan, lng: 0, accuracyM: 1, at: t0, place: nil, speedMps: nil), now: now))
        XCTAssertNil(ReportFrame(.health(metric: .steps, start: t0, end: t0, value: -1, detail: nil), now: now))
        XCTAssertNil(ReportFrame(.health(metric: .steps, start: t0, end: t0, value: .infinity, detail: nil), now: now))
    }

    func testTextIsCappedAndTimesAreClamped() throws {
        let long = String(repeating: "长", count: 500)
        let v = frame(.visit(lat: 1, lng: 2, accuracyM: 5, arrivedAt: t0, departedAt: nil, place: long))
        let h = frame(.health(metric: .workoutMin, start: t0, end: t0 + 60, value: 1, detail: long))
        Contract.check(v)
        Contract.check(h)
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

    func testStepsAreSentAgainOnlyWhenTheHourlyTotalChanges() {
        func hour(_ h: Double, _ n: Double) -> (start: Date, end: Date, steps: Double) { (t0 + h * 3600, t0 + (h + 1) * 3600, n) }
        var sent: [String: Double] = [:]
        func steps(_ o: [Sensed]) -> [Double] { o.compactMap { if case let .health(.steps, _, _, v, _) = $0 { return v } else { return nil } } }
        XCTAssertEqual(steps(HealthCollector.newSteps([hour(0, 120.4), hour(1, 0), hour(2, 800)], sent: &sent)), [120, 800], "zero hours are skipped")
        XCTAssertEqual(HealthCollector.newSteps([hour(0, 120.4), hour(2, 800)], sent: &sent).count, 0, "unchanged: nothing")
        XCTAssertEqual(steps(HealthCollector.newSteps([hour(0, 300), hour(2, 800)], sent: &sent)), [300], "the Watch added steps late")
        XCTAssertEqual(steps(HealthCollector.newSteps([hour(1, 40)], sent: &sent)), [40])
    }

    func testSleepOverlapsCountOnceAndGapsMakeBlocks() {
        func d(_ h: Double) -> Date { t0 + h * 3600 }
        let blocks = HealthCollector.sleepBlocks([
            (d(0), d(2)), (d(1), d(3)),          // Watch and phone overlap: 3 h, not 4
            (d(3.2), d(4)),                      // a 12 min wake: same block
            (d(6), d(7)),                        // a later nap: its own block
        ], now: d(20))
        guard case let .health(m1, s1, e1, v1, _) = blocks[0], case let .health(_, _, _, v2, _) = blocks[1] else { return XCTFail() }
        XCTAssertEqual(blocks.count, 2)
        XCTAssertEqual(m1, .sleepMin); XCTAssertEqual(s1, d(0)); XCTAssertEqual(e1, d(4))
        XCTAssertEqual(v1, 3 * 60 + 48); XCTAssertEqual(v2, 60)
        // A block that ended less than 45 minutes ago is not settled and waits.
        XCTAssertEqual(HealthCollector.sleepBlocks([(d(0), d(1))], now: d(1.5)).count, 0)
        XCTAssertEqual(HealthCollector.sleepBlocks([(d(0), d(1))], now: d(1.8)).count, 1)
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
