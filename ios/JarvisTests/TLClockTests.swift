import XCTest
@testable import Jarvis

/// The minutes of a day are those the wall clock shows, in the zone the brain wrote the day in: on the two days a year the clocks change, and on a phone in another zone.
final class TLClockTests: XCTestCase {
    private func cal(_ zone: String) -> Calendar { var c = Calendar(identifier: .gregorian); c.timeZone = TimeZone(identifier: zone)!; return c }
    private func instant(_ zone: String, _ y: Int, _ mo: Int, _ d: Int, _ h: Int, _ mi: Int = 0) -> Date {
        cal(zone).date(from: DateComponents(year: y, month: mo, day: d, hour: h, minute: mi))!
    }
    private func ms(_ d: Date) -> Int64 { Int64(d.timeIntervalSince1970 * 1000) }
    private func reply(_ zone: String, _ y: Int, _ mo: Int, _ d: Int, _ items: [String] = []) throws -> DayReply {
        let c = cal(zone), start = c.startOfDay(for: instant(zone, y, mo, d, 12)), end = c.date(byAdding: .day, value: 1, to: start)!
        let json = #"{"date": "\#(String(format: "%04d-%02d-%02d", y, mo, d))", "tz": "\#(zone)", "start_ms": \#(ms(start)), "end_ms": \#(ms(end)), "now_ms": \#(ms(start)), "missing": [], "items": [\#(items.joined(separator: ","))]}"#
        return try JSONDecoder().decode(DayReply.self, from: Data(json.utf8))
    }
    private func stay(_ id: String, _ zone: String, _ y: Int, _ mo: Int, _ d: Int, from h0: Int, to h1: Int) -> String {
        #"{"id": "\#(id)", "lane": "you", "kind": "stay", "state": "done", "start_ms": \#(ms(instant(zone, y, mo, d, h0))), "end_ms": \#(ms(instant(zone, y, mo, d, h1)))}"#
    }
    private let van = "America/Los_Angeles"          // clocks go back on 2026-11-01 and forward on 2026-03-08 here

    func testOnTheDayTheClocksGoBackNineInTheMorningIsMinute540() throws {
        let r = try reply(van, 2026, 11, 1)
        XCTAssertEqual(r.end_ms - r.start_ms, 25 * 3_600_000, "the day lasts 25 hours")
        let c = TLClock(r)
        XCTAssertEqual(c.minute(instant(van, 2026, 11, 1, 0, 30)), 30)
        XCTAssertEqual(c.minute(instant(van, 2026, 11, 1, 9)), 540, "elapsed it is 600")
        XCTAssertEqual(c.minute(instant(van, 2026, 11, 1, 23, 30)), 1410, "the last hour is on the bar and not past it")
        XCTAssertEqual(c.minute(instant(van, 2026, 11, 2, 0)), 1440, "the next midnight ends the day")
    }

    func testOnTheDayTheClocksGoForwardNineInTheMorningIsMinute540() throws {
        let r = try reply(van, 2026, 3, 8)
        XCTAssertEqual(r.end_ms - r.start_ms, 23 * 3_600_000, "the day lasts 23 hours")
        let c = TLClock(r)
        XCTAssertEqual(c.minute(instant(van, 2026, 3, 8, 1, 59)), 119)
        XCTAssertEqual(c.minute(instant(van, 2026, 3, 8, 3)), 180, "the hour that was skipped is not counted")
        XCTAssertEqual(c.minute(instant(van, 2026, 3, 8, 9)), 540, "elapsed it is 480")
        XCTAssertEqual(c.minute(instant(van, 2026, 3, 9, 0)), 1440, "the day ends on the minute it shows, not an hour early")
    }

    func testTheDaysAfterAClockChangeStillBeginOnTheMultiplesOf1440() {
        let c = cal(van), origin = c.startOfDay(for: instant(van, 2026, 10, 31, 12))
        XCTAssertEqual(Cal.wall(instant(van, 2026, 11, 2, 0), in: c, from: origin), 2 * 1440)
        XCTAssertEqual(Cal.wall(instant(van, 2026, 11, 2, 9), in: c, from: origin), 2 * 1440 + 540)
        XCTAssertEqual(Cal.wall(instant(van, 2026, 10, 30, 21, 15), in: c, from: origin), -DAY + 21 * 60 + 15)
    }

    func testTheLongDayKeepsItsLastHourOnTheBarAndTheTimesOnTheRowsRead() throws {
        let r = try reply(van, 2026, 11, 1, [stay("a", van, 2026, 11, 1, from: 9, to: 11), stay("b", van, 2026, 11, 1, from: 23, to: 24)])
        let m = DayTimelineModel.build(r, now: nil)
        XCTAssertEqual(m.entries.map(\.time), ["09:00", "23:00"], "09:00 printed 10:00")
        XCTAssertEqual(m.entries.map(\.t0), [540, 1380])
        XCTAssertEqual(m.entries.last?.t1, 1440)
        XCTAssertEqual(m.scene.pieces.last?.t1, 1440, "the bar reaches its end with the last hour on it")
        XCTAssertEqual(m.segs.map { $0.t1 }.max(), 1440)
    }

    func testTheShortDayEndsAtMidnightToo() throws {
        let r = try reply(van, 2026, 3, 8, [stay("a", van, 2026, 3, 8, from: 9, to: 11), stay("b", van, 2026, 3, 8, from: 23, to: 24)])
        let m = DayTimelineModel.build(r, now: nil)
        XCTAssertEqual(m.entries.map(\.time), ["09:00", "23:00"])
        XCTAssertEqual(m.entries.last?.t1, 1440, "not 1380: the day does not end an hour before the bar does")
    }

    func testAPhoneInAnotherZoneReadsTheBrainsDay() throws {
        // the brain's day is Tokyo's; whatever zone this phone is in, 09:00 there is 09:00 on the list
        let r = try reply("Asia/Tokyo", 2026, 10, 19, [stay("a", "Asia/Tokyo", 2026, 10, 19, from: 9, to: 11)])
        let m = DayTimelineModel.build(r, now: nil)
        XCTAssertEqual(m.entries.first?.time, "09:00")
        XCTAssertEqual(m.entries.first?.t0, 540)
        XCTAssertEqual(m.entries.first?.right.hasPrefix("09:00–11:00"), true)
        // a zone it does not know is read in this phone's own
        let json = #"{"date": "2026-10-19", "tz": "Atlantis/Nowhere", "start_ms": 0, "end_ms": 1, "now_ms": 1, "missing": [], "items": []}"#
        XCTAssertEqual(TLClock(try JSONDecoder().decode(DayReply.self, from: Data(json.utf8))).cal.timeZone, Cal.cur.cal.timeZone)
    }

    func testNowIsTheMinuteOfTheBrainsDayWhenNowIsInIt() throws {
        let tl = TimelineState(), zone = "Pacific/Auckland", c = cal(zone), here = Cal.now()
        let r = try reply(zone, c.component(.year, from: here), c.component(.month, from: here), c.component(.day, from: here))
        let n = try XCTUnwrap(tl.nowMinute(in: r))
        let parts = c.dateComponents([.hour, .minute], from: Cal.now())
        XCTAssertEqual(n, Double((parts.hour ?? 0) * 60 + (parts.minute ?? 0)), accuracy: 1.5, "the clock on the wall there")
        let before = c.date(byAdding: .day, value: -3, to: here)!
        let past = try reply(zone, c.component(.year, from: before), c.component(.month, from: before), c.component(.day, from: before))
        XCTAssertNil(tl.nowMinute(in: past), "another day has no line for now")
    }
}
