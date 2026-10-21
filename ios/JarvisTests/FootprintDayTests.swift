#if DEBUG
import XCTest
@testable import Jarvis

/// The footprint's one-day screen: the minute under a finger on the bar, and the rows, chips and sentence made from the made-up day.
final class FootprintDayTests: XCTestCase {
    func testTheBarScrubReadsTheMinuteUnderTheFingerAndKeepsItOnTheBar() {
        let span = 360.0...1440.0
        XCTAssertEqual(TLBarScrub.minute(x: 0, span: span), 360)
        XCTAssertEqual(TLBarScrub.minute(x: TLLayout.barW / 2, span: span), 900, accuracy: 1e-9)
        XCTAssertEqual(TLBarScrub.minute(x: TLLayout.barW, span: span), 1440)
        XCTAssertEqual(TLBarScrub.minute(x: -40, span: span), 360, "left of the bar is its start")
        XCTAssertEqual(TLBarScrub.minute(x: 999, span: span), 1440, "right of it is 24:00")
    }

    @MainActor private func made() -> (FootprintDayContent, FootprintModel) {
        let cal = Cal.cur.cal
        let m = FootprintModel(reply: FootprintFixture.footprint(cal), day: FootprintFixture.day(cal))
        return (FootprintDayContent.make(m, past: false), m)
    }

    @MainActor func testTheRowsAreThePlacesAndOutingsInOrderWithTheOutingsStopsAndGapInside() {
        let (c, _) = made(), rows = c.entries
        XCTAssertEqual(rows.first?.id, "stay-1")
        XCTAssertEqual(rows.first?.kind, .stay)
        XCTAssertEqual(rows.first?.rail, .trip); XCTAssertTrue(rows.first?.railOut == true); XCTAssertFalse(rows.first?.railIn == true)
        XCTAssertEqual(rows.last?.kind, .now, "today ends with the line for now")
        XCTAssertEqual(rows.dropLast().last?.kind, .stay)
        XCTAssertTrue(rows.dropLast().last?.going == true)
        XCTAssertEqual(rows.map(\.t0), rows.map(\.t0).sorted(), "in the order of the day")
        let outings = rows.filter { $0.kind == .trip }
        XCTAssertEqual(outings.count, 2)
        XCTAssertTrue(outings.allSatisfy { $0.tapOnly && $0.opens && $0.ribbon.count == FootprintDayContent.ribbonSlices && $0.height == TLMetrics.rowDetail })
        let drive = outings[0]
        XCTAssertTrue(drive.ribbon.contains { $0 == nil } && drive.ribbon.contains { $0 != nil }, "holes where he stood still or nothing was recorded")
        // after the drive come its stops, numbered from 1, and the stretch with no fixes, in time
        let i = rows.firstIndex { $0.id == drive.id }!
        let subs = rows[(i + 1)...].prefix { $0.kind == .pause }
        XCTAssertEqual(subs.compactMap(\.token), Array(1...subs.compactMap(\.token).count))
        XCTAssertEqual(subs.map(\.t0), subs.map(\.t0).sorted())
        let gap = subs.filter { $0.rail == .gap }
        XCTAssertFalse(gap.isEmpty, "the stretch with no fixes, in as many pieces as the stop in it leaves")
        XCTAssertTrue(gap.allSatisfy(\.unknown))
        XCTAssertTrue(subs.allSatisfy { $0.height == TLMetrics.sub })
        // and the walk has none
        XCTAssertEqual(rows.filter { $0.kind == .pause }.count, subs.count)
    }

    @MainActor func testTheMapHasARoutePerOutingTheStopsNumberedAndOnePinForAPlaceHeKeeps() {
        let (c, m) = made()
        XCTAssertEqual(c.routes.map(\.id), m.reply.outings.map(\.id))
        XCTAssertEqual(c.tokens.map(\.number), Array(1...c.tokens.count))
        XCTAssertEqual(c.places.count, 2, "home twice in the day is one place, and the school")
        XCTAssertEqual(c.places.filter { $0.pin == .home }.count, 1)
        XCTAssertNotNil(c.now)
        XCTAssertEqual(c.chips.count, 2)
    }

    @MainActor func testTheChipsSayWhereHeIsAndHowHeMovedMostAndNameTheDistanceByIt() {
        let (c, m) = made(), home = m.reply.stays[2], drive = m.reply.outings[0]
        XCTAssertEqual(c.here.count, 1, "he is at home now")
        XCTAssertEqual(c.here.first?.word, FootprintDayContent.name(home))
        XCTAssertEqual(c.here.first?.value, clock(m.clock.minute(ms: home.start_ms)), "since he got in")
        XCTAssertEqual(c.here.first?.symbol, "house.fill")
        XCTAssertEqual(c.tripSymbol, "car.fill", "the drive has more time moving than the walk")
        XCTAssertEqual(c.chips.first?.word, Words.FootprintDay.moving("automotive"))
        XCTAssertEqual(c.chips.first?.value, tlDistance(drive.distance_m), "the drive's own distance, not the walk's too")
        XCTAssertTrue(FootprintDayContent.make(m, past: true).here.isEmpty, "another day has no now")
    }

    @MainActor func testTheMainWayOfMovingIsTheOneWithMostTimeMovingAndNilForOneWithoutAWord() {
        let (_, m) = made(), drive = m.reply.outings[0], walk = m.reply.outings[1]
        XCTAssertEqual(FootprintDayContent.mainActivity([drive, walk]), "automotive")
        XCTAssertEqual(FootprintDayContent.mainActivity([walk]), "walking")
        XCTAssertNil(FootprintDayContent.mainActivity([]))
        let boat = FootprintOuting(id: "b", start_ms: 0, end_ms: 9, from_stay: nil, to_stay: nil, activity: "boat", distance_m: 5, moving_ms: 9, max_speed_mps: nil, stops: [], gaps: [], heart_rate: nil, points: [])
        XCTAssertNil(FootprintDayContent.mainActivity([boat]))
        XCTAssertNil(Words.FootprintDay.moving("boat"))
    }

    @MainActor func testTheRowUnderTheHeldMinuteIsTheStopThenTheOutingThenThePlace() {
        let (c, _) = made(), rows = c.entries
        let drive = rows.first { $0.kind == .trip }!, stop = rows.first { $0.token == 1 }!, gap = rows.first { $0.rail == .gap }!
        XCTAssertEqual(FootprintDayContent.hot(at: (stop.t0 + stop.t1!) / 2, in: rows), stop.id)
        XCTAssertEqual(FootprintDayContent.hot(at: (gap.t0 + gap.t1!) / 2, in: rows), gap.id)
        XCTAssertEqual(FootprintDayContent.hot(at: drive.t0 + 1, in: rows), drive.id)
        XCTAssertEqual(FootprintDayContent.hot(at: 10 * 60, in: rows), "stay-1")
        XCTAssertNil(FootprintDayContent.hot(at: 23 * 60, in: rows), "after now nothing is lit")
    }

    @MainActor func testTheHeldLineAndTheDotKnowSpeedOnTheRoadAndNothingBeforeTheDay() {
        let (c, m) = made()
        let drive = c.entries.first { $0.kind == .trip }!
        let driving = drive.t0 + 20            // clear of the stops
        XCTAssertNotNil(FootprintDayContent.dot(m, minute: driving))
        XCTAssertNil(FootprintDayContent.dot(m, minute: 23 * 60), "he was not anywhere yet")
        XCTAssertEqual(FootprintDayContent.heldLine(m, minute: 23 * 60, elsewhere: "x"), "x")
        XCTAssertTrue(FootprintDayContent.heldLine(m, minute: 10 * 60, elsewhere: "x").hasPrefix("10:00"), "at a place: the time, then its name")
    }

    func testADatePickedIsTheDayNumberTheTimelineCountsFromItsFirstDay() {
        XCTAssertEqual(FootprintDayPicker.day(of: Cal.cur.date(0)), 0)
        XCTAssertEqual(FootprintDayPicker.day(of: Cal.cur.date(-9).addingTimeInterval(13 * 3600)), -9, "any hour of the day is the day")
        XCTAssertEqual(FootprintDayPicker.day(of: Cal.cur.date(3)), 3)
    }

    func testRowsOfHomeKeepTheirHeightsAndTheNewFieldsAreOff() {
        let e = TLEntry(id: "a", kind: .pause, t0: 0, title: "t")
        XCTAssertEqual(e.height, TLMetrics.row)
        XCTAssertNil(e.token); XCTAssertTrue(e.ribbon.isEmpty); XCTAssertFalse(e.tapOnly); XCTAssertFalse(e.opens)
    }
}
#endif
