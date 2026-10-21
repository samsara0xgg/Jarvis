import CoreLocation
import XCTest
@testable import Jarvis

/// The footprint page's data: the reply as the brain writes it, the route, the store, the model of a moment of the day and the made-up day the simulator shows.
final class FootprintTests: XCTestCase {
    private typealias Stub = DayActionsTests.Stub
    private let pi = URL(string: "http://pi.test:8765")!
    private var savedSession: URLSession!, savedSlow: URLSession!

    override func setUp() {
        let c = URLSessionConfiguration.ephemeral
        c.protocolClasses = [Stub.self]
        savedSession = BrainClient.session; savedSlow = BrainClient.slowSession
        BrainClient.session = URLSession(configuration: c); BrainClient.slowSession = BrainClient.session
        Stub.seen = []
    }
    override func tearDown() { BrainClient.session = savedSession; BrainClient.slowSession = savedSlow; Stub.handler = nil }

    // MARK: a small day: a stay, a drive with a stop, a gap and a heart, a stay still going

    private let b: Int64 = 1_800_000_000_000
    private func json(stayB: String = #""place": "Park", "kept_place": {"id": "p", "name": "Home", "role": "home"}"#, outingEnd: Int64? = 1_800_000, toStay: String = #""stay-b""#,
                      now: Int64? = nil, extra: String = "") -> String {
        let nowMs = now ?? b + 2_000_000
        return #"""
        {"date": "2026-10-20", "tz": "Pacific/Auckland", "start_ms": \#(b), "end_ms": \#(b + 86_400_000), "now_ms": \#(nowMs), "future": 1,
         "stays": [
           {"id": "stay-a", "start_ms": \#(b), "end_ms": \#(b + 600_000), "lat": -10.0, "lng": 20.0, "place": null, "kept_place": null},
           {"id": "stay-b", "start_ms": \#(b + 1_800_000), "end_ms": null, "lat": -10.1, "lng": 20.1, \#(stayB)},
           {"id": "broken", "start_ms": 1, "end_ms": null, "lng": 20.1}\#(extra)],
         "outings": [
           {"id": "outing-1", "start_ms": \#(b + 600_000), "end_ms": \#(outingEnd.map { String(b + $0) } ?? "null"), "from_stay": "stay-a", "to_stay": \#(toStay), "activity": "automotive",
            "distance_m": 1000, "moving_ms": 1140000, "max_speed_mps": 30.0,
            "stops": [{"id": "stay-s", "start_ms": \#(b + 1_000_000), "end_ms": \#(b + 1_060_000), "lat": -10.05, "lng": 20.05, "place": null, "kept_place": null}],
            "gaps": [{"start_ms": \#(b + 1_300_000), "end_ms": \#(b + 1_500_000)}],
            "heart_rate": {"avg_bpm": 90, "max_bpm": 100, "home_avg_bpm": null, "samples": [[\#(b + 700_000), 80], [\#(b + 900_000), 100]]},
            "points": [[\#(b + 600_000), -10.0, 20.0, 0.0], [\#(b + 700_000), -10.02, 20.02, null], [\#(b + 900_000), -10.04, 20.04, 10.0], ["x", 1, 2, 3],
                       [\#(b + 1_300_000), -10.06, 20.06, 20.0], [\#(b + 1_500_000), -10.08, 20.08, 30.0], [\#(b + 1_800_000), -10.1, 20.1, 10.0]]},
           {"id": "outing-2", "start_ms": \#(b + 3_000_000), "end_ms": null, "distance_m": 5, "moving_ms": 0, "points": []}]}
        """#
    }
    private func reply(_ text: String) throws -> FootprintReply { try JSONDecoder().decode(FootprintReply.self, from: Data(text.utf8)) }
    private func dayJSON(_ items: String) throws -> DayReply {
        let t = #"{"date": "2026-10-20", "tz": "Pacific/Auckland", "start_ms": \#(b), "end_ms": \#(b + 86_400_000), "now_ms": \#(b + 2_000_000), "missing": [], "items": [\#(items)]}"#
        return try JSONDecoder().decode(DayReply.self, from: Data(t.utf8))
    }
    private func model(_ text: String? = nil, day: DayReply? = nil) throws -> FootprintModel { FootprintModel(reply: try reply(text ?? json()), day: day) }

    /// a drive from stay-a to stay-b (which starts 30 s before the drive's end) whose fixes begin and end a few hundred metres out of the stays
    private func joinedJSON(points: Bool = true) -> String {
        let stop = #"{"id": "stay-s", "start_ms": \#(b + 1_000_000), "end_ms": \#(b + 1_300_000), "lat": -10.05, "lng": 20.05, "place": null, "kept_place": null}"#
        let gaps = #"{"start_ms": \#(b + 900_000), "end_ms": \#(b + 1_000_000)}, {"start_ms": \#(b + 1_300_000), "end_ms": \#(b + 1_500_000)}"#
        let rows = "[\(b + 700_000), -10.02, 20.02, 5], [\(b + 900_000), -10.04, 20.04, 5], [\(b + 1_500_000), -10.08, 20.08, 5], [\(b + 2_030_000), -10.098, 20.098, 5]"
        return #"""
        {"date": "2026-10-20", "tz": "UTC", "start_ms": \#(b), "end_ms": \#(b + 86_400_000), "now_ms": \#(b + 3_000_000),
         "stays": [{"id": "stay-a", "start_ms": \#(b), "end_ms": \#(b + 600_000), "lat": -10.0, "lng": 20.0, "place": null, "kept_place": null},
                   {"id": "stay-b", "start_ms": \#(b + 2_000_000), "end_ms": null, "lat": -10.1, "lng": 20.1, "place": null, "kept_place": null}],
         "outings": [{"id": "outing-1", "start_ms": \#(b + 600_000), "end_ms": \#(b + 2_030_000), "from_stay": "stay-a", "to_stay": "stay-b", "activity": "automotive",
           "distance_m": 1, "moving_ms": 1, "stops": [\#(points ? stop : "")], "gaps": [\#(points ? gaps : "")], "heart_rate": null, "points": [\#(points ? rows : "")]}]}
        """#
    }

    // MARK: decoding

    func testTheReplyIsReadWithANullSpeedNoHeartAndOneGarbledPointAndStayLeftOut() throws {
        let r = try reply(json())
        XCTAssertEqual(r.stays.map(\.id), ["stay-a", "stay-b"], "the stay without a place on earth costs itself only")
        XCTAssertEqual(r.stays[1].kept_place, FootprintKeptPlace(id: "p", name: "Home", role: "home"))
        XCTAssertNil(r.stays[1].end_ms)
        XCTAssertEqual(r.outings.map(\.id), ["outing-1", "outing-2"])
        let o = r.outings[0]
        XCTAssertEqual(o.points.count, 6, "seven rows, one of them garbled")
        XCTAssertNil(o.points[1].speed_mps)
        XCTAssertEqual(o.points[0].speed_mps, 0)
        XCTAssertEqual(o.points[2], FootprintPoint(at_ms: b + 900_000, lat: -10.04, lng: 20.04, speed_mps: 10))
        XCTAssertEqual(o.heart_rate?.samples, [FootprintBeat(at_ms: b + 700_000, bpm: 80), FootprintBeat(at_ms: b + 900_000, bpm: 100)])
        XCTAssertNil(o.heart_rate?.home_avg_bpm)
        XCTAssertEqual(o.stops.map(\.id), ["stay-s"])
        XCTAssertEqual(o.gaps, [FootprintGap(start_ms: b + 1_300_000, end_ms: b + 1_500_000)])
        let late = r.outings[1]
        XCTAssertNil(late.end_ms, "still out")
        XCTAssertNil(late.heart_rate, "no heart_rate is none")
        XCTAssertEqual(late.gaps, [])
        XCTAssertNil(late.max_speed_mps)
    }

    func testAThreeElementRowIsAPointWithNoSpeedAndAMissingRequiredFieldIsNotAReply() throws {
        let p = try JSONDecoder().decode(FootprintPoint.self, from: Data("[5, 1.5, 2.5]".utf8))
        XCTAssertEqual(p, FootprintPoint(at_ms: 5, lat: 1.5, lng: 2.5, speed_mps: nil))
        XCTAssertThrowsError(try reply(#"{"date": "2026-10-20", "start_ms": 1, "end_ms": 2, "now_ms": 1, "stays": [], "outings": []}"#), "no tz")
        XCTAssertThrowsError(try reply(#"{"date": "2026-10-20", "tz": "UTC", "start_ms": 1, "end_ms": 2, "now_ms": 1, "stays": []}"#), "no outings")
    }

    // MARK: the route

    func testTheRouteIsAskedForTheDayUnderTheTokenAndAnOlderBrainsMissingRouteIsNil() async throws {
        var asked: String?
        Stub.handler = { req in asked = req.url?.query; return (200, self.json()) }
        let ok = try await BrainClient.footprint("2026-10-20", bases: [pi], token: "tok")
        XCTAssertEqual(ok.reply?.outings.count, 2)
        XCTAssertEqual(asked, "date=2026-10-20")
        XCTAssertEqual(Stub.seen.map(\.path), ["/inherent/footprint"])
        XCTAssertEqual(Stub.seen.map(\.auth), ["Bearer tok"])

        Stub.handler = { _ in (404, #"{"detail": "Not Found"}"#) }
        let old = try await BrainClient.footprint("2026-10-20", bases: [pi], token: "tok")
        XCTAssertNil(old.reply, "the framework's 404 is a brain without the route")

        Stub.handler = { _ in (404, #"{"detail": "no such day"}"#) }
        do { _ = try await BrainClient.footprint("2026-10-20", bases: [pi], token: "tok"); XCTFail("a route's own 404 is a refusal") } catch { XCTAssertEqual(error as? BrainError, .refused) }

        Stub.handler = { _ in (200, #"{"hello": 1}"#) }
        do { _ = try await BrainClient.footprint("2026-10-20", bases: [pi], token: "tok"); XCTFail("not the shape") } catch { XCTAssertEqual(error as? BrainError, .garbled) }
    }

    // MARK: the store

    @MainActor private func until(_ done: () -> Bool) async { for _ in 0..<200 where !done() { try? await Task.sleep(nanoseconds: 10_000_000) } }

    @MainActor func testTheStoreAsksOnceAndAgainOnlyWhenTheAppWasOpenedAndAFailureIsAskedAgain() async throws {
        let r = try reply(json()), day = "2026-10-20"
        let store = FootprintStore()
        var asked = 0
        var answer: FootprintAnswer? = .reply(r)
        store.fetch = { _ in asked += 1; return answer }
        XCTAssertNil(store.reply(day))
        store.ensure(day); store.ensure(day)
        await until { store.reply(day) != nil }
        XCTAssertEqual(store.reply(day), r)
        XCTAssertEqual(asked, 1, "the second ask met the first in flight")
        store.ensure(day)
        await until { asked > 1 }
        XCTAssertEqual(asked, 1, "served since the app was opened")

        BrainLink.shared.opened()
        store.ensure(day)
        await until { asked > 1 }
        XCTAssertEqual(asked, 2)
        XCTAssertEqual(store.reply(day), r, "what was on show stays")

        BrainLink.shared.opened()
        answer = nil
        store.ensure(day)
        await until { asked > 2 }
        store.ensure(day)
        await until { asked > 3 }
        XCTAssertEqual(asked, 4, "a failure leaves the day to be asked again")

        answer = .missing
        BrainLink.shared.opened()
        store.ensure("2026-10-19")
        await until { store.isMissing("2026-10-19") }
        XCTAssertTrue(store.isMissing("2026-10-19"))
        XCTAssertNil(store.reply("2026-10-19"))
        XCTAssertFalse(store.isMissing(day))
    }

    #if DEBUG
    @MainActor func testTheFixtureIsReadFirstAndNothingIsAsked() throws {
        let store = FootprintStore(), r = try reply(json())
        store.fetch = { _ in XCTFail("asked"); return nil }
        store.fixture = ["2026-10-20": r]
        XCTAssertEqual(store.reply("2026-10-20"), r)
        store.ensure("2026-10-20")
        XCTAssertNil(store.reply("2026-10-21"))
    }
    #endif

    // MARK: the clock

    func testAMinuteBecomesTheInstantTheWallClockShowsItAtAndBack() {
        func clock(_ zone: String, _ y: Int, _ m: Int, _ d: Int) -> TLClock {
            var c = Calendar(identifier: .gregorian); c.timeZone = TimeZone(identifier: zone)!
            return TLClock(tz: zone, startMs: Int64(c.date(from: DateComponents(year: y, month: m, day: d, hour: 12))!.timeIntervalSince1970 * 1000))
        }
        let van = "America/Los_Angeles"
        for (c, skipped) in [(clock("Asia/Tokyo", 2026, 10, 19), 0..<0), (clock(van, 2026, 11, 1), 0..<0), (clock(van, 2026, 3, 8), 120..<180)] {
            for m in [0, 30.25, 59.5, 90, 119, 180, 540, 600.5, 1000, 1439.5, 1440, -30] as [Double] where !skipped.contains(Int(m)) {
                XCTAssertEqual(c.minute(ms: c.ms(minute: m)), m, accuracy: 1e-6, "\(c.cal.timeZone.identifier) \(m)")
            }
        }
        let fall = clock(van, 2026, 11, 1), spring = clock(van, 2026, 3, 8), origin = Int64(fall.origin.timeIntervalSince1970 * 1000)
        XCTAssertEqual(fall.ms(minute: 0), origin)
        XCTAssertEqual(fall.ms(minute: 540) - origin, 600 * 60_000, "nine in the morning is ten hours in on the day the clocks go back")
        XCTAssertEqual(fall.ms(minute: 1440) - origin, 25 * 3_600_000, "the day ends at the next midnight")
        XCTAssertEqual(spring.ms(minute: 180) - spring.ms(minute: 119), 60_000, "the skipped hour is not counted")
        XCTAssertEqual(clock("Asia/Tokyo", 2026, 10, 19).ms(minute: -30) - clock("Asia/Tokyo", 2026, 10, 19).ms(minute: 0), -30 * 60_000)
    }

    // MARK: a moment of the day

    func testWhereHeWasAtATimeIsTheStayOrTheStopOrAlongTheRouteStraightAcrossAGap() throws {
        let m = try model()
        func at(_ t: Int64) -> CLLocationCoordinate2D? { m.position(atMs: b + t) }
        func same(_ t: Int64, _ lat: Double, _ lng: Double, _ note: String) {
            guard let p = at(t) else { return XCTFail("nowhere: \(note)") }
            XCTAssertEqual(p.latitude, lat, accuracy: 1e-9, note); XCTAssertEqual(p.longitude, lng, accuracy: 1e-9, note)
        }
        same(300_000, -10.0, 20.0, "in the first stay")
        same(650_000, -10.01, 20.01, "half way along the first stretch")
        same(1_030_000, -10.05, 20.05, "in the stop, which the route passes through otherwise")
        same(1_400_000, -10.07, 20.07, "half way across the gap, a straight line")
        same(2_500_000, -10.1, 20.1, "in the stay he is still in")
        XCTAssertNil(m.position(atMs: b - 1), "before anything")
        let bare = try model(#"{"date": "d", "tz": "UTC", "start_ms": 0, "end_ms": 9, "now_ms": 1, "stays": [], "outings": [{"id": "o", "start_ms": 0, "end_ms": 5, "distance_m": 0, "moving_ms": 0, "points": []}]}"#)
        XCTAssertNil(bare.position(atMs: 1), "an outing with no points")
        XCTAssertEqual(m.stay(atMs: b + 1_030_000)?.id, "stay-s")
        XCTAssertEqual(m.stay(atMs: b + 600_000)?.id, nil, "the first stay ended when the outing began")
        XCTAssertEqual(m.outing(atMs: b + 600_000)?.id, "outing-1")
        XCTAssertEqual(m.outing(atMs: b + 1_800_000)?.id, nil, "and it ended where the second stay began")
        XCTAssertEqual(m.outing(atMs: b + 9_000_000)?.id, "outing-2", "an outing with no end is still going")
    }

    func testBetweenTheStaysAndTheFixesHeIsStraightAlongTheJoinsTheStopsAndTheGap() throws {
        func same(_ m: FootprintModel, _ t: Int64, _ lat: Double, _ lng: Double, _ note: String) {
            guard let p = m.position(atMs: b + t) else { return XCTFail("nowhere: \(note)") }
            XCTAssertEqual(p.latitude, lat, accuracy: 1e-9, note); XCTAssertEqual(p.longitude, lng, accuracy: 1e-9, note)
        }
        let m = try model(joinedJSON())
        same(m, 650_000, -10.01, 20.01, "half way from the centre of the stay he left to his first fix")
        same(m, 950_000, -10.045, 20.045, "between a fix and the stop after it")
        same(m, 1_150_000, -10.05, 20.05, "in the stop")
        same(m, 1_400_000, -10.065, 20.065, "between the stop and the fix after the gap")
        same(m, 1_765_000, -10.089, 20.089, "half way between the last two fixes")
        same(m, 2_500_000, -10.1, 20.1, "in the stay he reached")
        same(try model(joinedJSON(points: false)), 1_315_000, -10.05, 20.05, "no fixes: straight between the two stays")
    }

    func testTheRouteIsItsFixesWithTheGapBetweenFixesAndAJoinToEachStayAndPinToPinWithoutFixes() throws {
        func c(_ lat: Double, _ lng: Double) -> CLLocationCoordinate2D { CLLocationCoordinate2D(latitude: lat, longitude: lng) }
        let m = try model(joinedJSON()), r = m.route(m.reply.outings[0])
        XCTAssertEqual(r.id, "outing-1")
        XCTAssertEqual(r.points.map(\.ms), [b + 700_000, b + 900_000, b + 1_500_000, b + 2_030_000])
        XCTAssertEqual(r.gaps, [
            FootprintMapGap(from: c(-10.0, 20.0), to: c(-10.02, 20.02), startMs: b + 600_000, endMs: b + 700_000),
            FootprintMapGap(from: c(-10.04, 20.04), to: c(-10.08, 20.08), startMs: b + 900_000, endMs: b + 1_500_000),
            FootprintMapGap(from: c(-10.098, 20.098), to: c(-10.1, 20.1), startMs: b + 2_030_000, endMs: b + 2_030_000),
        ], "the join out of home, the one gap (the stop in it split it in two, drawn once), the join into the stay")
        let bare = try model(joinedJSON(points: false)), p = bare.route(bare.reply.outings[0])
        XCTAssertEqual(p.points, [])
        XCTAssertEqual(p.gaps, [FootprintMapGap(from: c(-10.0, 20.0), to: c(-10.1, 20.1), startMs: b + 600_000, endMs: b + 2_030_000)], "one line from stay to stay")
        let none = try model(#"{"date": "d", "tz": "UTC", "start_ms": 0, "end_ms": 9, "now_ms": 1, "stays": [], "outings": [{"id": "o", "start_ms": 0, "end_ms": 5, "distance_m": 0, "moving_ms": 0, "points": []}]}"#)
        XCTAssertEqual(none.route(none.reply.outings[0]).gaps, [], "no stays and no fixes: nothing to draw")
    }

    func testHowFastAndHowHardHisHeartWasIsBetweenTheReadingsAndNilWhenStillOrInTheGap() throws {
        let m = try model()
        XCTAssertNil(m.speedMps(atMs: b + 300_000), "in a stay")
        XCTAssertNil(m.speedMps(atMs: b + 1_030_000), "in a stop")
        XCTAssertNil(m.speedMps(atMs: b + 1_400_000), "no fixes in the gap")
        XCTAssertEqual(m.speedMps(atMs: b + 650_000), 0, "the point after it has no speed: the one before gives it")
        XCTAssertEqual(m.speedMps(atMs: b + 800_000), 10, "and the other way")
        XCTAssertEqual(try XCTUnwrap(m.speedMps(atMs: b + 1_100_000)), 15, accuracy: 1e-9)
        XCTAssertEqual(try XCTUnwrap(m.speedMps(atMs: b + 1_600_000)), 30 - 20.0 / 3, accuracy: 1e-9)
        XCTAssertEqual(try XCTUnwrap(m.heartBpm(atMs: b + 800_000)), 90, accuracy: 1e-9)
        XCTAssertNil(m.heartBpm(atMs: b + 650_000), "before the watch's first reading")
        XCTAssertNil(m.heartBpm(atMs: b + 950_000), "after its last")
        XCTAssertNil(m.heartBpm(atMs: b + 100_000), "not on an outing")
        XCTAssertNil(m.heartBpm(atMs: b + 3_100_000), "an outing the watch said nothing of")
    }

    func testTheStayHeIsInNowIsTodaysOpenOneAndTheDayTotalsAreTheOutingsSummed() throws {
        let m = try model()
        XCTAssertEqual(m.nowStay?.id, "stay-b")
        XCTAssertNil(try model(json(now: b + 86_400_000)).nowStay, "a past day has no now")
        XCTAssertNil(try model(json(now: b - 1)).nowStay)
        XCTAssertEqual(m.totalDistanceM, 1005)
        XCTAssertEqual(m.topSpeedMps, 30)
        XCTAssertNil(try model(#"{"date": "d", "tz": "UTC", "start_ms": 0, "end_ms": 9, "now_ms": 1, "stays": [], "outings": []}"#).topSpeedMps)
    }

    func testTheDiscIsAsWideAsTheTimeSpentIsLong() {
        XCTAssertEqual(FootprintModel.discRadius(minutes: 787), 29, accuracy: 1e-9)
        XCTAssertEqual(FootprintModel.discRadius(minutes: 787 / 4), 14.5, accuracy: 1e-9, "a quarter of the time is a quarter of the area")
        XCTAssertEqual(FootprintModel.discRadius(minutes: 0), 0)
        XCTAssertEqual(FootprintModel.discRadius(minutes: -5), 0)
    }

    func testTheSpeedRampHasItsEndsAndBlendsInBetween() {
        XCTAssertEqual(FootprintPaint.speed(kmh: 0), RGB(240, 132, 218))
        XCTAssertEqual(FootprintPaint.speed(kmh: -3), RGB(240, 132, 218))
        XCTAssertEqual(FootprintPaint.speed(kmh: 25), RGB(172, 124, 255))
        XCTAssertEqual(FootprintPaint.speed(kmh: 12.5), RGB(206, 128, 236.5), "half way between the first two marks")
        XCTAssertEqual(FootprintPaint.speed(kmh: 80), RGB(111, 224, 180))
        XCTAssertEqual(FootprintPaint.speed(kmh: 130), RGB(111, 224, 180), "80 and above holds")
        XCTAssertEqual(FootprintPaint.speed(mps: 80 / 3.6), RGB(111, 224, 180))
    }

    // MARK: the title

    func testAnOutingIsNamedByTheEventItOverlapsMostElseWhereItWentElseThatItIsOnItsWay() throws {
        func event(_ id: String, _ from: Int64, _ to: Int64?, _ title: String?) -> String {
            #"{"id": "\#(id)", "lane": "you", "kind": "event", "state": "done", "start_ms": \#(b + from), "end_ms": \#(to.map { String(b + $0) } ?? "null"), "title": \#(title.map { "\"\($0)\"" } ?? "null")}"#
        }
        let day = try dayJSON([event("e1", 500_000, 700_000, "Short"), event("e2", 700_000, 1_500_000, "Lesson"), event("e3", 0, 600_000, "Before"), event("e4", 800_000, 1_800_000, nil),
                               #"{"id": "s", "lane": "you", "kind": "stay", "state": "done", "start_ms": \#(b + 600_000), "end_ms": \#(b + 1_800_000)}"#].joined(separator: ","))
        let o = try reply(json()).outings[0]
        XCTAssertEqual(try model(day: day).title(o), .event("Lesson"), "the longest overlap wins; one that only touches, one without a title and a stay do not count")
        XCTAssertEqual(try model().title(o), .to("Home"), "no day: where it went, by the name of the place he keeps")
        let park = try model(json(stayB: #""place": "Park", "kept_place": null"#))
        XCTAssertEqual(park.title(park.reply.outings[0]), .to("Park"), "else its address")
        let nameless = try model(json(stayB: #""place": null, "kept_place": null"#))
        XCTAssertEqual(nameless.title(nameless.reply.outings[0]), .outing, "somewhere without a name, and over")
        let toNowhere = try model(json(toStay: "null"))
        XCTAssertEqual(toNowhere.title(toNowhere.reply.outings[0]), .outing)
        XCTAssertEqual(toNowhere.title(toNowhere.reply.outings[1]), .onTheWay, "still out, and nowhere yet")
        let still = try model(json(outingEnd: nil, toStay: "null"))
        XCTAssertEqual(still.title(still.reply.outings[0]), .onTheWay)
        let late = try dayJSON(event("e", 3_100_000, nil, "Open"))
        XCTAssertEqual(try model(json(now: b + 3_500_000), day: late).title(try reply(json()).outings[1]), .event("Open"), "an event with no end runs to the outing's end, which is now")
    }

    // MARK: the made-up day

    #if DEBUG
    func testTheMadeUpDayIsOneDayAcrossTheDayAndTheFootprintWithTheNumbersOfTheLesson() throws {
        let cal = Cal.cur.cal
        let day = FootprintFixture.day(cal), fp = FootprintFixture.footprint(cal)
        XCTAssertEqual([day.date, day.tz, "\(day.start_ms)", "\(day.end_ms)", "\(day.now_ms)"], [fp.date, fp.tz, "\(fp.start_ms)", "\(fp.end_ms)", "\(fp.now_ms)"])
        let clock = TLClock(day)
        XCTAssertEqual(clock.minute(ms: fp.now_ms), TimelineFixture.now, accuracy: 1e-6, "still 21:50")
        XCTAssertEqual(clock.minute(ms: fp.outings[0].start_ms), 787, accuracy: 1e-6)

        // every stay and stop of the footprint is the same stay of the day
        let all = fp.stays + fp.outings.flatMap(\.stops)
        XCTAssertEqual(fp.stays.count, 3)
        for s in all {
            let i = try XCTUnwrap(day.items.first { $0.id == s.id }, s.id)
            XCTAssertEqual([i.kind, "\(i.start_ms)", "\(i.end_ms.map(String.init) ?? "-")"], ["stay", "\(s.start_ms)", "\(s.end_ms.map(String.init) ?? "-")"], s.id)
            XCTAssertEqual(i.lat, s.lat); XCTAssertEqual(i.lng, s.lng); XCTAssertEqual(i.kept_place?.id, s.kept_place?.id)
        }
        XCTAssertEqual(Set(all.map(\.id)).count, all.count, "ids are not reused")
        XCTAssertEqual(fp.stays.last?.end_ms, nil, "home again, still there")
        XCTAssertEqual(fp.stays.first?.kept_place?.role, "home")

        let drive = fp.outings[0], walk = fp.outings[1]
        XCTAssertEqual([drive.activity, walk.activity], ["automotive", "walking"])
        XCTAssertEqual([drive.from_stay, drive.to_stay, walk.from_stay, walk.to_stay], ["stay-1", "stay-5", "stay-5", "stay-6"])
        XCTAssertEqual(drive.stops.map { ($0.end_ms! - $0.start_ms) / 60_000 }, [6, 5], "no stop under five minutes")
        XCTAssertEqual(drive.gaps.map { ($0.end_ms - $0.start_ms) / 60_000 }, [2, 3], "the no-fix stretch without the second stop in it")
        XCTAssertEqual(drive.end_ms, drive.points.last?.at_ms, "the drive ends at its last fix")
        XCTAssertTrue((30_000...60_000).contains(drive.end_ms! - fp.stays[1].start_ms), "a little after the school stay was dated")
        XCTAssertEqual(drive.moving_ms, drive.end_ms! - drive.start_ms - 11 * 60_000)
        XCTAssertEqual(drive.distance_m, 21_500, accuracy: 1_000, "about 21.5 km, stay to stay")
        XCTAssertEqual(drive.max_speed_mps! * 3.6, 81, accuracy: 1.5)
        XCTAssertEqual(drive.heart_rate?.avg_bpm ?? 0, 88, accuracy: 1)
        XCTAssertEqual(drive.heart_rate?.home_avg_bpm, 68)
        XCTAssertNil(walk.heart_rate)
        XCTAssertEqual(walk.distance_m, 3_100, accuracy: 400, "the school stands a few km from home")

        // the points: in time order within the outing, every ~10 s where the line bends, none inside the gap, and their length from the stay he left to the stay he reached is the distance
        func xy(_ s: FootprintStay?) -> (Double, Double) { (s!.lat, s!.lng) }
        for o in fp.outings {
            XCTAssertEqual(o.id, "outing-\(o.start_ms)")
            XCTAssertEqual(o.points.map(\.at_ms), o.points.map(\.at_ms).sorted())
            XCTAssertEqual(o.points.last?.at_ms, o.end_ms)
            XCTAssertTrue(o.points.allSatisfy { (o.start_ms...o.end_ms!).contains($0.at_ms) })
            let line = [xy(fp.stays.first { $0.id == o.from_stay })] + o.points.map { ($0.lat, $0.lng) } + [xy(fp.stays.first { $0.id == o.to_stay })]
            let metres = zip(line, line.dropFirst()).reduce(0.0) { $0 + CLLocation(latitude: $1.0.0, longitude: $1.0.1).distance(from: CLLocation(latitude: $1.1.0, longitude: $1.1.1)) }
            XCTAssertEqual(metres, o.distance_m, accuracy: o.distance_m * 0.01, o.id)
        }
        XCTAssertEqual(walk.points.first?.at_ms, walk.start_ms)
        func away(_ s: FootprintStay, _ p: FootprintPoint) -> Double { CLLocation(latitude: s.lat, longitude: s.lng).distance(from: CLLocation(latitude: p.lat, longitude: p.lng)) }
        XCTAssertEqual(away(fp.stays[0], drive.points[0]), 500, accuracy: 100, "the phone wakes GPS about 500 m out of home")
        XCTAssertEqual(away(fp.stays[1], drive.points[drive.points.count - 1]), 400, accuracy: 100, "and loses it about 400 m before the school")
        XCTAssertTrue((30...400).contains(drive.points.count), "\(drive.points.count)")
        let gap = (drive.gaps[0].start_ms, drive.gaps[1].end_ms)
        XCTAssertFalse(drive.points.contains { $0.at_ms > gap.0 && $0.at_ms < gap.1 })
        XCTAssertTrue(drive.points.contains { $0.at_ms == gap.0 } && drive.points.contains { $0.at_ms == gap.1 })
        let speeds = drive.points.compactMap(\.speed_mps).map { $0 * 3.6 }
        XCTAssertEqual(speeds.count, drive.points.count)
        XCTAssertLessThan(speeds.min()!, 3); XCTAssertGreaterThan(speeds.max()!, 78)
        XCTAssertTrue(speeds.filter { $0 > 20 }.count > 10 && speeds.filter { $0 < 20 }.count > 10, "varied")
        XCTAssertEqual(drive.heart_rate!.samples.count, Int((drive.end_ms! - drive.start_ms) / 15_000) + 1)

        // far from his life: the Canterbury plains
        for p in drive.points + walk.points { XCTAssertTrue((-44.0)...(-43.5) ~= p.lat && 171.5...172.5 ~= p.lng, "\(p.lat), \(p.lng)") }
        for s in all { XCTAssertTrue((-44.0)...(-43.5) ~= s.lat && 171.5...172.5 ~= s.lng) }

        // and the model reads it
        let m = FootprintModel(reply: fp, day: day)
        let lesson = try XCTUnwrap(day.items.first { $0.kind == "event" }?.title)
        XCTAssertEqual(m.title(drive), .event(lesson))
        XCTAssertEqual(m.title(walk), .to(try XCTUnwrap(fp.stays.last?.kept_place?.name)))
        XCTAssertEqual(m.nowStay?.id, "stay-6")
        XCTAssertEqual(m.totalDistanceM, drive.distance_m + walk.distance_m)
        for s in drive.stops {
            let mid = (s.start_ms + s.end_ms!) / 2
            XCTAssertEqual(m.stay(atMs: mid)?.id, s.id)
            XCTAssertNil(m.speedMps(atMs: mid))
            XCTAssertEqual(m.position(atMs: mid)?.latitude, s.lat)
        }
        let cruising = drive.start_ms + 35 * 60_000
        XCTAssertNil(m.speedMps(atMs: cruising), "inside the gap")
        XCTAssertGreaterThan(try XCTUnwrap(m.speedMps(atMs: drive.start_ms + 20 * 60_000)), 3)
        XCTAssertNotNil(m.heartBpm(atMs: drive.start_ms + 20 * 60_000))
        XCTAssertNil(m.heartBpm(atMs: walk.start_ms + 60_000))
        XCTAssertEqual(m.stay(atMs: fp.now_ms)?.id, "stay-6")
    }
    #endif
}
