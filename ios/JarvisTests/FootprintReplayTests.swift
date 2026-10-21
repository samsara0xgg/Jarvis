#if DEBUG
import XCTest
import CoreLocation
@testable import Jarvis

/// The footprint replay's pure model: an outing's drawn line indexed (`FootprintTrack`), the held bar's trip layout (`FootprintBarLayout`) and the ▶ schedule (`FootprintReplay`),
/// on the made-up day with its fixes every 10 s and with one every 30 s, and on small generated ones.
final class FootprintReplayTests: XCTestCase {
    private func fixture(sparse: Bool) -> FootprintModel {
        let cal = Cal.cur.cal
        return FootprintModel(reply: FootprintFixture.footprint(cal, sparse: sparse), day: FootprintFixture.day(cal, sparse: sparse))
    }

    // MARK: generated days, in metres east and north of a corner of the made-up plain

    private func ll(_ p: (x: Double, y: Double)) -> (lat: Double, lng: Double) { (-43.75 + p.y / 111_320, 172.02 + p.x / (111_320 * cos(-43.75 * .pi / 180))) }
    private func stay(_ id: String, _ p: (x: Double, y: Double), _ from: Int64, _ to: Int64?) -> FootprintStay {
        FootprintStay(id: id, start_ms: from, end_ms: to, lat: ll(p).lat, lng: ll(p).lng, place: id, kept_place: nil)
    }
    /// a fix at every vertex of `line`, `every` ms apart from `at`
    private func outing(_ id: String, at: Int64, _ line: [(x: Double, y: Double)], every: Int64 = 100_000, from: String? = nil, to: String? = nil, activity: String = "automotive", stops: [FootprintStay] = []) -> FootprintOuting {
        let pts = line.enumerated().map { FootprintPoint(at_ms: at + Int64($0) * every, lat: ll($1).lat, lng: ll($1).lng, speed_mps: 10) }
        return FootprintOuting(id: id, start_ms: at, end_ms: pts.last!.at_ms, from_stay: from, to_stay: to, activity: activity, distance_m: 0, moving_ms: 0, max_speed_mps: 10, stops: stops, gaps: [], heart_rate: nil, points: pts)
    }
    private func model(_ stays: [FootprintStay], _ outings: [FootprintOuting]) -> FootprintModel {
        FootprintModel(reply: FootprintReply(date: "2026-10-20", tz: "Pacific/Auckland", start_ms: 0, end_ms: 86_400_000, now_ms: 86_400_000, stays: stays, outings: outings), day: nil)
    }

    private func metres(_ a: CLLocationCoordinate2D, _ b: CLLocationCoordinate2D) -> Double { FootprintTrack.metres(a, b).distance }

    // MARK: the track

    func testDistanceAndTimeRoundTripAndTheStopsHoldTheDistanceStill() throws {
        for sparse in [false, true] {
            let m = fixture(sparse: sparse), o = m.reply.outings[0], t = try XCTUnwrap(m.tracks[o.id])
            XCTAssertEqual(t.length, o.distance_m, accuracy: 100, "the drawn line is about the outing's distance")
            var last = 0.0
            let timed = t.distance(atMs: t.endMs - 1)           // the join to the school is untimed: all of it is the outing's last moment
            XCTAssertGreaterThan(t.length - timed, 300)
            for i in 0...400 {
                let d = timed * Double(i) / 400
                XCTAssertEqual(t.distance(atMs: t.ms(atDistance: d)), d, accuracy: 0.2, "distance \(d)")
                let ms = t.startMs + (t.endMs - t.startMs) * Int64(i) / 400, back = t.distance(atMs: ms)
                XCTAssertGreaterThanOrEqual(back, last); last = back
                XCTAssertLessThanOrEqual(t.ms(atDistance: back), ms + 1)
            }
            XCTAssertEqual(t.distance(atMs: t.startMs - 5_000), 0); XCTAssertEqual(t.distance(atMs: t.endMs + 5_000), t.length, accuracy: 1e-9)
            for s in o.stops {
                let d = t.distance(atMs: s.start_ms)
                for f in [0.0, 0.3, 0.7, 1] { XCTAssertEqual(t.distance(atMs: s.start_ms + Int64(f * Double(s.end_ms! - s.start_ms))), d, accuracy: 1e-9, "still through \(s.id)") }
                XCTAssertEqual(t.ms(atDistance: d), s.start_ms, accuracy: 1, "a stop's distance is the moment he arrived")
                XCTAssertGreaterThan(t.distance(atMs: s.end_ms! + 20_000), d)
            }
        }
    }

    func testTheCoordinateStaysOnTheDrawnLine() throws {
        for sparse in [false, true] {
            let m = fixture(sparse: sparse), o = m.reply.outings[0], r = m.route(o), t = try XCTUnwrap(m.tracks[o.id])
            let segments = zip(r.points.map(\.coord), r.points.dropFirst().map(\.coord)).map { ($0, $1) } + r.gaps.map { ($0.from, $0.to) }
            func off(_ c: CLLocationCoordinate2D) -> Double {
                segments.map { a, b -> Double in
                    let p = FootprintTrack.metres(a, c), q = FootprintTrack.metres(a, b), len2 = q.east * q.east + q.north * q.north
                    let k = len2 > 0 ? min(1, max(0, (p.east * q.east + p.north * q.north) / len2)) : 0
                    return hypot(p.east - q.east * k, p.north - q.north * k)
                }.min()!
            }
            for i in 0...300 { XCTAssertLessThan(off(try XCTUnwrap(t.coord(atDistance: t.length * Double(i) / 300))), 0.05) }
            // the ends are the stays' pins
            XCTAssertEqual(metres(try XCTUnwrap(t.coord(atDistance: 0)), m.reply.stays[0].coord), 0, accuracy: 0.05)
            XCTAssertEqual(metres(try XCTUnwrap(t.coord(atDistance: t.length)), m.reply.stays[1].coord), 0, accuracy: 0.05)
        }
    }

    func testTheHeadingLooksAlongTheLineAheadAndHasNoneAtTheEnd() throws {
        let m = model([], [outing("a", at: 0, [(0, 0), (1000, 0), (1000, 1000)])]), t = try XCTUnwrap(m.tracks["a"])
        XCTAssertEqual(t.length, 2000, accuracy: 0.5)
        XCTAssertEqual(try XCTUnwrap(t.heading(atDistance: 0, ahead: 120)), 90, accuracy: 0.01)
        XCTAssertEqual(try XCTUnwrap(t.heading(atDistance: 940, ahead: 120)), 45, accuracy: 0.3, "half the look is round the corner")
        XCTAssertEqual(try XCTUnwrap(t.heading(atDistance: 1500, ahead: 120)), 0, accuracy: 0.01)
        XCTAssertEqual(try XCTUnwrap(t.heading(atDistance: 1000, ahead: 30)), 0, accuracy: 0.01, "from the corner on")
        XCTAssertEqual(try XCTUnwrap(t.heading(atDistance: 1950, ahead: 120)), 0, accuracy: 0.01, "the line's end is as far as it looks")
        XCTAssertNil(t.heading(atDistance: 2000, ahead: 120))
        let one = try XCTUnwrap(model([], [outing("p", at: 0, [(5, 5)])]).tracks["p"])
        XCTAssertNil(one.heading(atDistance: 0, ahead: 120)); XCTAssertEqual(one.length, 0)
        XCTAssertNotNil(one.coord(atDistance: 0))
    }

    func testAnOutingWithNothingDrawnHasNoCoordinate() throws {
        let o = FootprintOuting(id: "e", start_ms: 0, end_ms: 10, from_stay: nil, to_stay: nil, activity: nil, distance_m: 0, moving_ms: 0, max_speed_mps: nil, stops: [], gaps: [], heart_rate: nil, points: [])
        let t = try XCTUnwrap(model([], [o]).tracks["e"])
        XCTAssertNil(t.coord(atDistance: 0)); XCTAssertEqual(t.length, 0)
        XCTAssertNil(FootprintReplay(model: model([], [o]), scope: .day))
    }

    func testAScrubOverSeveralThousandPointsIsFarUnderAFrame() throws {
        let line = (0..<5000).map { (x: Double($0) * 8, y: 300 * sin(Double($0) / 120)) }
        let stops = [stay("s1", line[1000], 100_000_000, 100_300_000), stay("s2", line[3000], 300_000_000, 300_300_000)]
        let o = outing("big", at: 0, line, every: 1000, stops: stops)
        let m = model([], [o])
        let t0 = CFAbsoluteTimeGetCurrent()
        let t = try XCTUnwrap(m.tracks["big"])
        let built = CFAbsoluteTimeGetCurrent() - t0
        var sink = 0.0
        let t1 = CFAbsoluteTimeGetCurrent()
        for i in 0..<10_000 {
            let d = t.length * Double(i) / 10_000
            sink += t.coord(atDistance: d)!.latitude + Double(t.ms(atDistance: d)) + t.distance(atMs: Int64(Double(i) * 500)) + (t.heading(atDistance: d, ahead: 120) ?? 0)
        }
        let looked = CFAbsoluteTimeGetCurrent() - t1
        print("footprint track: build \(Int(built * 1000)) ms, 10 000 x 4 lookups \(Int(looked * 1000)) ms (\(sink > 0))")
        XCTAssertLessThan(built, 0.05, "built once per reply (8 ms in a debug build)")
        XCTAssertLessThan(looked, 0.1, "40 000 lookups, 25 ms in a debug build, so a frame needs a few of them")
    }

    // MARK: the bar layout

    func testTheLayoutFillsTheBarWithBeadsNotchesAndSegments() throws {
        for sparse in [false, true] {
            let m = fixture(sparse: sparse), l = FootprintBarLayout(model: m)
            XCTAssertEqual(l.items.map(\.kind), [.bead(stayId: "stay-1"), .segment(outingId: m.reply.outings[0].id), .bead(stayId: "stay-5"), .segment(outingId: m.reply.outings[1].id), .bead(stayId: "stay-6")])
            XCTAssertEqual(l.items.first?.x0, 0); XCTAssertEqual(l.items.last?.x1 ?? 0, 380, accuracy: 1e-9, "exactly the bar")
            for (a, b) in zip(l.items, l.items.dropFirst()) { XCTAssertEqual(b.x0 - a.x1, FootprintBarLayout.spacing, accuracy: 1e-9) }
            for it in l.items {
                switch it.kind {
                case .bead: XCTAssertEqual(it.x1 - it.x0, 24, accuracy: 1e-9)
                case .segment: XCTAssertEqual(it.notches.map { $0.x1 - $0.x0 }, it.notches.map { _ in 6 })
                }
            }
            let drive = l.items[1], walk = l.items[3]
            XCTAssertEqual(drive.notches.count, 2); XCTAssertEqual(walk.notches.count, 0)
            XCTAssertEqual((walk.x1 - walk.x0) / (drive.x1 - drive.x0 - 12), m.tracks[m.reply.outings[1].id]!.length / m.tracks[m.reply.outings[0].id]!.length, accuracy: 1e-9, "wide as it is long")
            XCTAssertGreaterThan(drive.x1 - drive.x0, 200, "the drive takes what is left")
            // the pieces of a segment fill it and share their ends
            XCTAssertEqual(drive.pieces.first?.x0, drive.x0); XCTAssertEqual(drive.pieces.last?.x1 ?? 0, drive.x1, accuracy: 1e-9)
            for (a, b) in zip(drive.pieces, drive.pieces.dropFirst()) { XCTAssertEqual(a.x1, b.x0, accuracy: 1e-9); XCTAssertLessThanOrEqual(a.ms1, b.ms0) }
            // the segment's width goes by distance: a run twice as long is twice as wide
            let runs = drive.pieces.compactMap { p -> (Double, Double)? in if case let .run(d0, d1) = p.kind { return (d1 - d0, p.x1 - p.x0) } else { return nil } }
            for r in runs where r.0 > 0 { XCTAssertEqual(r.1 / r.0, runs[0].1 / runs[0].0, accuracy: 1e-9) }
        }
    }

    func testAShortOutingKeepsTheFloorAndTheBarStillAddsUp() throws {
        let stays = [stay("a", (0, 0), -1_000_000, 0), stay("b", (20_000, 0), 1_000_000, 2_000_000), stay("c", (20_100, 0), 3_000_000, nil)]
        let m = model(stays, [outing("long", at: 0, [(0, 0), (20_000, 0)], every: 1_000_000, from: "a", to: "b"), outing("short", at: 2_000_000, [(20_000, 0), (20_100, 0)], every: 1_000_000, from: "b", to: "c", activity: "walking")])
        let l = FootprintBarLayout(model: m), short = l.items[3]
        XCTAssertEqual(short.x1 - short.x0, 28, accuracy: 1e-9); XCTAssertEqual(l.items.last?.x1 ?? 0, 380, accuracy: 1e-9)
        XCTAssertEqual(l.item(atX: (short.x0 + short.x1) / 2).kind, short.kind)
    }

    func testEveryBeadAndNotchCanBeLandedOn() throws {
        let m = fixture(sparse: false), l = FootprintBarLayout(model: m)
        for it in l.items {
            if case .bead = it.kind {
                let x = it.x0.rounded(.up)
                XCTAssertLessThanOrEqual(x, it.x1, "a whole point inside")
                XCTAssertEqual(l.item(atX: x).kind, it.kind)
                XCTAssertTrue((it.ms0...it.ms1).contains(l.ms(atX: x)))
            }
            for n in it.notches {
                let xs = stride(from: n.x0.rounded(.up), through: n.x1, by: 1).map { $0 }
                XCTAssertGreaterThanOrEqual(xs.count, 5, "a notch is several whole points wide")
                for x in xs { XCTAssertTrue((n.ms0...n.ms1).contains(l.ms(atX: x)), "x \(x) is in the stop's minutes") }
                XCTAssertEqual(l.ms(atX: n.x0 + 0.01), n.ms0, accuracy: 10_000); XCTAssertEqual(l.ms(atX: n.x1 - 0.01), n.ms1, accuracy: 10_000)
            }
        }
        XCTAssertEqual(l.item(atX: -20).kind, l.items.first?.kind); XCTAssertEqual(l.item(atX: 999).kind, l.items.last?.kind)
        // the stop minutes: the 6 and the 5 of the lesson
        XCTAssertEqual(l.items[1].notches.map { ($0.ms1 - $0.ms0) / 60_000 }, [6, 5])
    }

    func testMomentAndPositionAreInverseAndRiseAcrossTheBar() throws {
        for sparse in [false, true] {
            let m = fixture(sparse: sparse), l = FootprintBarLayout(model: m)
            var lastMs = Int64.min
            for i in 0...760 {
                let x = Double(i) / 2, ms = l.ms(atX: x)
                XCTAssertGreaterThanOrEqual(ms, lastMs, "x \(x)"); lastMs = ms
                XCTAssertEqual(l.ms(atX: l.x(atMs: ms)), ms, accuracy: 1, "x \(x)")
                // one to one wherever the moment moves on (the last 400 m into the school is untimed, the spacing between items is a halt)
                if l.ms(atX: x + 0.25) > ms { XCTAssertEqual(l.x(atMs: ms), x, accuracy: 0.01, "x \(x)") }
            }
            var lastX = -1.0
            let lo = l.items.first!.ms0, hi = l.items.last!.ms1
            for i in 0...2000 {
                let ms = lo + (hi - lo) * Int64(i) / 2000, x = l.x(atMs: ms)
                XCTAssertGreaterThanOrEqual(x, lastX, "ms \(ms)"); lastX = x
            }
        }
    }

    // MARK: the replay

    func testTheDayReplayRunsAboutThirtySecondsAndAnOutingFifteenToTwenty() throws {
        for sparse in [false, true] {
            let m = fixture(sparse: sparse), day = try XCTUnwrap(FootprintReplay(model: m, scope: .day))
            XCTAssertTrue((28...40).contains(day.duration), "\(day.duration)")
            for o in m.reply.outings {
                let r = try XCTUnwrap(FootprintReplay(model: m, scope: .outing(o.id)))
                XCTAssertTrue((15...20).contains(r.duration), "\(o.id) \(r.duration)")
            }
            XCTAssertTrue(FootprintReplay.heights.contains(day.height))
            // every outing gets three seconds of moving at least
            for o in m.reply.outings {
                var seconds = 0.0
                for i in 0..<Int(day.duration * 100) { let f = day.frame(at: Double(i) / 100); if f.beat == nil && f.outingId == o.id { seconds += 0.01 } }
                XCTAssertGreaterThanOrEqual(seconds, 2.95, o.id)
            }
        }
    }

    func testTheDayReplayShowsABeatForEveryLongStayAndStopWithItsWords() throws {
        let m = fixture(sparse: false), day = try XCTUnwrap(FootprintReplay(model: m, scope: .day))
        var beats: [FootprintReplay.Beat] = []
        for i in 0...Int(day.duration * 60) { if let b = day.frame(at: Double(i) / 60).beat, beats.last != b { beats.append(b) } }
        XCTAssertEqual(beats, [.stay(id: "stay-1", minutes: 787), .stop(id: "stay-2", number: 1, minutes: 6), .stop(id: "stay-3", number: 2, minutes: 5), .stay(id: "stay-5", minutes: Int((Double(m.reply.outings[1].start_ms - m.reply.stays[1].start_ms) / 60_000).rounded())),
                           .stay(id: "stay-6", minutes: Int((Double(m.reply.now_ms - m.reply.stays[2].start_ms) / 60_000).rounded()))])
        // an outing alone: its stops only
        let o = try XCTUnwrap(FootprintReplay(model: m, scope: .outing(m.reply.outings[0].id)))
        var stops: [FootprintReplay.Beat] = []
        for i in 0...Int(o.duration * 60) { if let b = o.frame(at: Double(i) / 60).beat, stops.last != b { stops.append(b) } }
        XCTAssertEqual(stops, [.stop(id: "stay-2", number: 1, minutes: 6), .stop(id: "stay-3", number: 2, minutes: 5)])
    }

    func testFramesRiseInTimeAndTheDotNeverJumpsMoreThanTheGroundSpeedAllows() throws {
        for sparse in [false, true] {
            let m = fixture(sparse: sparse)
            var scopes: [FootprintReplay.Scope] = [.day]
            scopes += m.reply.outings.map { .outing($0.id) }
            for scope in scopes {
                let r = try XCTUnwrap(FootprintReplay(model: m, scope: scope)), n = Int(r.duration * 60)
                var prev = r.frame(at: 0)
                let reach = FootprintReplay.heights.upperBound * FootprintReplay.screensPerSecond / 60
                for i in 1...n {
                    let f = r.frame(at: Double(i) / 60)
                    XCTAssertGreaterThanOrEqual(f.ms, prev.ms, "\(scope) frame \(i)")
                    if !f.flying && !prev.flying { XCTAssertLessThanOrEqual(metres(prev.coord, f.coord), reach + 0.01, "\(scope) frame \(i)") }
                    prev = f
                }
            }
        }
    }

    func testTheHeightIsClampedAndALongDayRunsLongerNotFaster() throws {
        let walk = model([], [outing("w", at: 0, [(0, 0), (300, 0)], activity: "walking")])
        let slow = try XCTUnwrap(FootprintReplay(model: walk, scope: .outing("w")))
        XCTAssertEqual(slow.height, 400); XCTAssertEqual(slow.duration, 18, accuracy: 1e-9)
        // 200 km would need 11 km/s over 18 s
        let far = model([], [outing("f", at: 0, [(0, 0), (200_000, 0)], every: 3_600_000)])
        let r = try XCTUnwrap(FootprintReplay(model: far, scope: .outing("f")))
        XCTAssertEqual(r.height, 3000); XCTAssertGreaterThan(r.duration, 100)
        var prev = r.frame(at: 0)
        for i in 1...Int(r.duration * 60) {
            let f = r.frame(at: Double(i) / 60)
            XCTAssertLessThanOrEqual(metres(prev.coord, f.coord), 1500 / 60 + 0.01); prev = f
        }
        // the made-up drive at a pace the camera can show, between the clamps
        let day = try XCTUnwrap(FootprintReplay(model: fixture(sparse: false), scope: .day))
        XCTAssertGreaterThan(day.height, 400); XCTAssertLessThan(day.height, 3000)
    }

    func testTheCameraFliesBetweenPlacesDuringABeatAndFacesTheWayOfTheLine() throws {
        let stays = [stay("home", (0, 0), -1_000_000, 0), stay("far", (1000, 0), 100_000, 200_000)]
        let a = outing("a", at: 0, [(0, 0), (500, 0), (1000, 0)], from: "home", to: "far", activity: "walking")
        let b = outing("b", at: 200_000, [(6000, 0), (6000, 800)], activity: "cycling")
        let r = try XCTUnwrap(FootprintReplay(model: model(stays, [a, b]), scope: .day)), n = Int(r.duration * 60)
        let flying = (0...n).map { r.frame(at: Double($0) / 60) }.filter(\.flying)
        XCTAssertGreaterThan(flying.count, 40, "a second of it")
        XCTAssertEqual(metres(flying.first!.coord, CLLocationCoordinate2D(latitude: ll((1000, 0)).lat, longitude: ll((1000, 0)).lng)), 0, accuracy: 30)
        XCTAssertEqual(metres(flying.last!.coord, CLLocationCoordinate2D(latitude: ll((6000, 0)).lat, longitude: ll((6000, 0)).lng)), 0, accuracy: 30)
        XCTAssertEqual(flying.last!.heading, 0, accuracy: 0.01, "it turns north as the next line goes")
        let first = r.frame(at: 1.0), still = r.frame(at: 0)
        XCTAssertEqual(first.heading, 90, accuracy: 0.5, "east along the walk")
        XCTAssertEqual(still.heading, 90, accuracy: 0.5, "the first beat faces the way the first line goes")
        // heights by activity, for a finger
        XCTAssertEqual([a, b, outing("c", at: 0, [(0, 0)], activity: "running"), outing("d", at: 0, [(0, 0)])].map(FootprintReplay.scrubHeight), [500, 800, 500, 1200])
        XCTAssertEqual([a, b].map(FootprintReplay.lookAhead), [30, 120])
    }

    func testTheHeadingIsKeptThroughAStopAndAtTheEnd() throws {
        let m = fixture(sparse: true), o = m.reply.outings[0], r = try XCTUnwrap(FootprintReplay(model: m, scope: .outing(o.id)))
        let frames = (0...Int(r.duration * 60)).map { r.frame(at: Double($0) / 60) }
        for i in frames.indices.dropFirst() where frames[i].beat != nil && frames[i - 1].beat != nil && frames[i].beat == frames[i - 1].beat {
            XCTAssertEqual(frames[i].heading, frames[i - 1].heading, accuracy: 1e-9, "still in a stop")
        }
        XCTAssertFalse(frames.contains { $0.heading.isNaN })
    }
}
#endif
