import CoreLocation
import XCTest
@testable import Jarvis

// ADR 0312: every fix kept as a track, the car's motion while he rides, and the two-step sleep. The brain's contract for the track is
// jarvis/surface/phone_events.py (`_fixes`, `_track_valid`); it is restated in `checkTrack`.

private let t0 = Date(timeIntervalSince1970: 1_792_000_000)  // 2026-10-20 (UTC), on a whole second

private func loc(_ dt: TimeInterval, north m: Double = 0, speed: Double = -1, accuracy: Double = 5, course: Double = -1) -> CLLocation {
    CLLocation(coordinate: .init(latitude: 49.28 + m / 111_320, longitude: -123.12), altitude: 70, horizontalAccuracy: accuracy,
               verticalAccuracy: 3, course: course, courseAccuracy: course >= 0 ? 5 : -1, speed: speed, speedAccuracy: speed >= 0 ? 0.5 : -1,
               timestamp: t0 + dt)
}

private func sample(_ dt: TimeInterval, _ a: Activity, still: Bool = false, _ c: Confidence = .high) -> MotionSample {
    MotionSample(start: t0 + dt, activity: a, confidence: c, still: still)
}

private func trackFix(_ dt: TimeInterval, lat: Double = 49.28, course: Double? = 90) -> TrackFix {
    TrackFix(at: t0 + dt, lat: lat, lng: -123.12, accuracyM: 4.7, altitudeM: 70, verticalAccuracyM: 3, speedMps: 13.4, speedAccuracyMps: 0.5,
             courseDeg: course, courseAccuracyDeg: 5)
}

/// What the brain checks of a track frame, restated.
private func checkTrack(_ frame: ReportFrame, file: StaticString = #filePath, line: UInt = #line) -> [[Any]] {
    guard let obj = try? JSONSerialization.jsonObject(with: frame.json) as? [String: Any], let payload = obj["payload"] as? [String: Any],
          let start = payload["started_at_ms"] as? Int64, let end = payload["ended_at_ms"] as? Int64, let fixes = payload["fixes"] as? [[Any]] else {
        XCTFail("not a track frame", file: file, line: line); return []
    }
    XCTAssertEqual(obj["event_type"] as? String, "phone.track_observed", file: file, line: line)
    XCTAssertEqual(Set(payload.keys), ["started_at_ms", "ended_at_ms", "fixes"], file: file, line: line)
    XCTAssertEqual(obj["ts_epoch_ms"] as? Int64, end, "the frame is at its last fix", file: file, line: line)
    XCTAssertTrue((1...300).contains(fixes.count), file: file, line: line)
    XCTAssertLessThanOrEqual(end - start, 600_000, file: file, line: line)
    var last: Int64 = 0
    for row in fixes {
        XCTAssertEqual(row.count, 11, file: file, line: line)
        guard let t = row[0] as? Int64 else { XCTFail("t_ms is an integer", file: file, line: line); continue }
        XCTAssertGreaterThanOrEqual(t, last, file: file, line: line)
        last = t
        if let c = row[8] as? Double { XCTAssertTrue((0..<360).contains(c), "course \(c)", file: file, line: line) }
        for i in [3, 5, 6, 7, 9] { if let v = row[i] as? Double { XCTAssertGreaterThanOrEqual(v, 0, file: file, line: line) } }
        XCTAssertTrue((0...3).contains(row[10] as? Int ?? -1), file: file, line: line)
    }
    XCTAssertEqual(fixes.first?[0] as? Int64, 0, file: file, line: line)
    XCTAssertEqual(fixes.last?[0] as? Int64, end - start, file: file, line: line)
    return fixes
}

final class TrackFrameTests: XCTestCase {
    func testATrackFrameCarriesEveryFixAsARowWithNullForWhatIOSDidNotMeasure() throws {
        var bare = trackFix(1)
        bare.altitudeM = nil; bare.verticalAccuracyM = nil; bare.speedMps = nil; bare.speedAccuracyMps = nil
        bare.courseDeg = nil; bare.courseAccuracyDeg = nil
        bare.simulated = true; bare.accessory = true
        let frame = try XCTUnwrap(ReportFrame(.track([trackFix(0, lat: 49.2812345678), bare, trackFix(2.5, course: 359.96)]), now: t0 + 60))

        let rows = checkTrack(frame)

        XCTAssertEqual(rows.count, 3)
        XCTAssertEqual(rows[0][1] as? Double, 49.2812346, "seven decimals: a centimetre")
        XCTAssertEqual(rows[0][2] as? Double, -123.12)
        XCTAssertTrue(rows[1][4...9].allSatisfy { $0 is NSNull }, "what iOS marked invalid travels as null")
        XCTAssertEqual(rows[1][10] as? Int, 3, "simulated and from an accessory")
        XCTAssertEqual(rows[2][0] as? Int64, 2500)
        XCTAssertEqual(rows[2][8] as? Double, 0, "359.96 rounds to 360, which is north again")
        XCTAssertFalse(String(decoding: frame.json, as: UTF8.self).contains("-0.0"))
    }

    func testTheSameFixesGiveTheSameFrameAndAFrameTheBrainWouldRefuseIsNotBuilt() {
        let fixes = (0..<60).map { trackFix(Double($0)) }
        XCTAssertEqual(ReportFrame(.track(fixes), now: t0 + 90)?.eventUid, ReportFrame(.track(fixes), now: t0 + 900)?.eventUid)
        XCTAssertNil(ReportFrame(.track([])))
        XCTAssertNil(ReportFrame(.track((0..<301).map { trackFix(Double($0) / 10) })), "more fixes than the brain takes in a frame")
        XCTAssertNil(ReportFrame(.track([trackFix(0), trackFix(601)])), "longer than ten minutes")
        XCTAssertNil(ReportFrame(.track([trackFix(0, lat: 91)])))
    }

    func testAFixFromCoreLocationKeepsWhatItMeasuredAndDropsWhatItMarkedInvalid() {
        let moving = TrackFix(loc(0, speed: 13.4, course: 271.5)), still = TrackFix(loc(1))
        XCTAssertEqual(moving.speedMps, 13.4); XCTAssertEqual(moving.courseDeg, 271.5); XCTAssertEqual(moving.altitudeM, 70)
        XCTAssertNil(still.speedMps); XCTAssertNil(still.speedAccuracyMps); XCTAssertNil(still.courseDeg); XCTAssertNil(still.courseAccuracyDeg)
    }
}

final class TrackChunkerTests: XCTestCase {
    func testARunIsCutIntoFramesOfLessThanAMinuteAndTheRestGoesWhenUpdatesSleep() throws {
        var c = TrackChunker()
        var frames: [[TrackFix]] = []
        for s in 0..<150 { if let f = c.add(trackFix(Double(s))) { frames.append(f) } }
        frames.append(try XCTUnwrap(c.close()))

        XCTAssertEqual(frames.map(\.count), [60, 60, 30])
        XCTAssertEqual(frames.flatMap { $0 }.map(\.at), (0..<150).map { t0 + Double($0) }, "every fix once, in order")
        for f in frames { _ = checkTrack(try XCTUnwrap(ReportFrame(.track(f), now: t0 + 200))) }
        XCTAssertNil(c.close())
    }

    func testAnOlderFixStartsAFrameOfItsOwnAndAFullFrameGoesAtOnce() throws {
        var c = TrackChunker()
        XCTAssertNil(c.add(trackFix(10)))
        XCTAssertEqual(c.add(trackFix(5))?.map(\.at), [t0 + 10])
        var full: [TrackFix]?
        for i in 1..<300 where full == nil { full = c.add(trackFix(5 + Double(i) / 10)) }
        XCTAssertEqual(full?.count, 300)
        XCTAssertNil(c.close())
    }
}

/// Drives replayed through the modes, second by second, as the collector would feed them.
@MainActor
final class LocationModesTests: XCTestCase {
    private var m = LocationModes()
    private var now = t0

    /// `seconds` of fixes, one a second, `speed` m/s northward from `north` metres; the clock follows, and ticks as the collector's would.
    @discardableResult
    private func drive(_ seconds: Int, speed: Double, from north: Double = 0) -> Double {
        var at = north
        for _ in 0..<seconds {
            now += 1
            at += speed
            m.fix(loc(now.timeIntervalSince(t0), north: at, speed: speed), now: now)
            m.tick(now)
        }
        return at
    }

    private func wait(_ seconds: TimeInterval) {
        let end = now + seconds
        while now < end { now = min(end, now + 1); m.tick(now) }
    }

    func testADriveKeepsGPSThroughARedLightAndTheMotionRecording() {
        m.moved(now)  // the rest fence: he left
        m.motion(sample(1, .automotive), now: now)
        XCTAssertEqual(m.mode, .moving); XCTAssertTrue(m.vehicle)
        XCTAssertEqual(m.plan, .init(updates: true, accuracy: kCLLocationAccuracyBest, activityType: .automotiveNavigation, liveMotion: true, recordMotion: true))
        let at = drive(120, speed: 12)
        // a red light of two minutes: the coprocessor says automotive and still
        m.motion(sample(now.timeIntervalSince(t0), .automotive, still: true), now: now)
        drive(120, speed: 0, from: at)
        XCTAssertEqual(m.mode, .moving); XCTAssertTrue(m.vehicle, "a red light is not a stop")
        drive(60, speed: 12, from: at)
        XCTAssertTrue(m.plan.recordMotion)
    }

    func testParkedForTenMinutesTheGPSSleepsLightlyAndDrivingOffWakesItAtOnce() {
        m.moved(now, activity: .automotive)
        let at = drive(60, speed: 12)
        drive(9 * 60 + 58, speed: 0, from: at)
        XCTAssertEqual(m.mode, .moving, "still within the ten minutes")
        drive(3, speed: 0, from: at)
        XCTAssertEqual(m.mode, .lightSleep)
        XCTAssertEqual(m.plan, .init(updates: true, accuracy: kCLLocationAccuracyThreeKilometers, activityType: .other, liveMotion: true, recordMotion: false))
        // driving off: live motion says automotive, moving
        m.motion(sample(now.timeIntervalSince(t0), .automotive), now: now)
        XCTAssertEqual(m.mode, .moving); XCTAssertTrue(m.vehicle); XCTAssertTrue(m.plan.recordMotion)
    }

    func testGettingOutAndWalkingEndsTheVehicleButNotTheTracking() {
        m.moved(now, activity: .automotive)
        drive(30, speed: 12)
        m.motion(sample(now.timeIntervalSince(t0), .walking), now: now)
        XCTAssertEqual(m.mode, .moving); XCTAssertFalse(m.vehicle)
        XCTAssertEqual(m.plan.accuracy, kCLLocationAccuracyNearestTenMeters); XCTAssertEqual(m.plan.activityType, .fitness)
        XCTAssertFalse(m.plan.recordMotion)
    }

    func testOnFootStillForNinetySecondsSleepsLightlyThenDeeplyAfterHalfAnHour() {
        m.moved(now, activity: .walking)
        drive(60, speed: 1.4)
        m.motion(sample(now.timeIntervalSince(t0), .stationary), now: now)
        wait(89)
        XCTAssertEqual(m.mode, .moving)
        wait(1)
        XCTAssertEqual(m.mode, .lightSleep)
        wait(30 * 60 - 1)
        XCTAssertEqual(m.mode, .lightSleep)
        wait(1)
        XCTAssertEqual(m.mode, .deepSleep)
        XCTAssertFalse(m.plan.updates); XCTAssertNil(m.deadline)
    }

    func testAStillnessTheHistoryReportsLongAgoSleepsAtOnce() {
        m.moved(now, activity: .walking)
        drive(10, speed: 1.4)
        m.motion(sample(now.timeIntervalSince(t0) - 600, .stationary), now: now)
        m.tick(now)
        XCTAssertEqual(m.mode, .lightSleep)
    }

    func testFiveMinutesWithin150MetresSleepsWithoutAnyMotion() {
        m.moved(now)
        let at = drive(100, speed: 2)
        drive(5 * 60, speed: 0.3, from: at)
        XCTAssertEqual(m.mode, .lightSleep)
    }

    func testTenSecondsAt25KmhIsAVehicleUnlessHeIsCycling() {
        m.moved(now)
        drive(9, speed: 8)
        XCTAssertFalse(m.vehicle)
        drive(2, speed: 8)
        XCTAssertTrue(m.vehicle)

        m = LocationModes()
        m.moved(now, activity: .cycling)
        drive(30, speed: 8)
        XCTAssertFalse(m.vehicle)
    }

    func testInALightSleepACoarseFixFarAwayWakesGPS() {
        m.moved(now, activity: .walking)
        drive(30, speed: 1.4)
        m.sleep(now)
        m.fix(loc(now.timeIntervalSince(t0), north: 400, accuracy: 300), now: now)
        XCTAssertEqual(m.mode, .lightSleep, "400 m away but within both accuracies and 150 m")
        m.fix(loc(now.timeIntervalSince(t0), north: 800, accuracy: 300), now: now)
        XCTAssertEqual(m.mode, .moving)
    }

    func testOnlyAMoveWakesTheDeepSleepAndSaysSo() {
        XCTAssertFalse(m.motion(sample(0, .stationary), now: now))
        XCTAssertFalse(m.motion(sample(0, .walking, .low), now: now))
        XCTAssertEqual(m.mode, .deepSleep)
        XCTAssertTrue(m.motion(sample(0, .walking), now: now))
        XCTAssertFalse(m.moved(now))
    }
}

final class DriveMotionTests: XCTestCase {
    func testGzipIsWhatAnyToolReads() throws {
        XCTAssertEqual(Gzip.crc32(Data("123456789".utf8)), 0xCBF4_3926)
        let text = Data(String(repeating: "0,0.0012,-0.0300,0.0101\n", count: 500).utf8)
        let gz = try XCTUnwrap(Gzip.compress(text))

        XCTAssertEqual([UInt8](gz.prefix(4)), [0x1f, 0x8b, 0x08, 0x00])
        let body = gz.subdata(in: 10..<(gz.count - 8)), trailer = [UInt8](gz.suffix(8))
        XCTAssertEqual(try (body as NSData).decompressed(using: .zlib) as Data, text)
        let crc = UInt32(trailer[0]) | UInt32(trailer[1]) << 8 | UInt32(trailer[2]) << 16 | UInt32(trailer[3]) << 24
        let size = UInt32(trailer[4]) | UInt32(trailer[5]) << 8 | UInt32(trailer[6]) << 16 | UInt32(trailer[7]) << 24
        XCTAssertEqual(crc, Gzip.crc32(text)); XCTAssertEqual(size, UInt32(text.count))
        XCTAssertLessThan(gz.count, text.count / 5)
    }

    func testARowIsTheRawValuesInTheColumnsOrder() {
        let row = DriveMotion.row(t: 40, values: [0.00123, -0.03, 0.0101, 0, 0, -1, 0.5, -0.25, 1e-5], quaternion: [1, 0, 0, 0], heading: 271.54)
        XCTAssertEqual(row, "40,0.0012,-0.0300,0.0101,0.0000,0.0000,-1.0000,0.5000,-0.2500,0.0000,1.00000,0.00000,0.00000,0.00000,271.5\n")
        XCTAssertEqual(row.split(separator: ",").count, DriveMotion.columns.split(separator: ",").count)
        XCTAssertTrue(DriveMotion.row(t: 0, values: Array(repeating: 0, count: 9), quaternion: [1, 0, 0, 0], heading: -1).hasSuffix(",\n"),
                      "no heading without a magnetic frame")
    }

    func testAPieceLeftOpenByAKillEndsAtItsLastWholeRow() throws {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("\(UUID().uuidString).csv")
        defer { try? FileManager.default.removeItem(at: url) }
        try Data("# jarvis motion v1 frame=x rate_hz=25\n\(DriveMotion.columns)\n0,0.1\n40,0.1\n80,0.1\n120,0.".utf8).write(to: url)

        XCTAssertEqual(DriveMotion.recoveredDuration(url), 120)
        XCTAssertTrue(try String(contentsOf: url, encoding: .utf8).hasSuffix("80,0.1\n"), "the cut row is gone")
    }

    func testAPieceKnowsItsRouteAndAPieceFromBeforeMotionIsAudio() throws {
        let old = try JSONDecoder().decode(LifelogPiece.self, from: Data(#"{"id":"a","startedMs":1,"durationMs":2,"ext":"ogg","attempts":0,"nextTryMs":0}"#.utf8))
        XCTAssertEqual(old.route, "audio")
        let motion = LifelogPiece(id: "b", startedMs: 1, durationMs: 2, ext: "csv.gz", kind: "motion")
        XCTAssertEqual(motion.route, "motion")
        XCTAssertEqual(motion.file.lastPathComponent, "b.csv.gz")
    }
}
