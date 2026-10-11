import XCTest
@testable import Jarvis

/// What is planned: Outlook events, pinned bus departures and the day's all-day labels.
final class PlannedDayTests: XCTestCase {
    private var base: Int64 { Int64(Cal.cur.origin.timeIntervalSince1970 * 1000) }
    private func at(_ m: Int64) -> Int64 { base + m * 60_000 }
    override func tearDown() { _ = RealDay.shared.rebuild([:], now: 0) }

    private func reply(_ items: String, _ extra: String = "") throws -> DayReply {
        let day = #"{"date": "2026-10-11", "tz": "UTC", "start_ms": \#(base), "end_ms": \#(base + 86_400_000), "now_ms": \#(at(900)), "missing": [], "items": [\#(items)]\#(extra)}"#
        return try JSONDecoder().decode(DayReply.self, from: Data(day.utf8))
    }
    private var event: String { #"{"id": "event-3f9a1c2b7d10", "lane": "you", "kind": "event", "state": "next", "start_ms": \#(at(1020)), "end_ms": \#(at(1080)), "title": "Study group"}"# }
    private var departure: String { #"{"id": "departure-1", "lane": "you", "kind": "departure", "state": "next", "start_ms": \#(at(960)), "end_ms": \#(at(960)), "title": "12 · Stop A", "route": "12", "board_stop": "Stop A", "to": "Downtown", "leave_at_ms": \#(at(950))}"# }

    func testAnEventAndADepartureAreDecoded() throws {
        let r = try reply(event + "," + departure)
        XCTAssertEqual(r.items.count, 2)
        XCTAssertEqual(r.items[0].title, "Study group")
        XCTAssertEqual(r.items[1].route, "12")
        XCTAssertEqual(r.items[1].board_stop, "Stop A")
        XCTAssertEqual(r.items[1].to, "Downtown")
        XCTAssertEqual(r.items[1].leave_at_ms, at(950))
    }

    func testAnEventIsAStretchAndADepartureASmallStarWithAShortLabel() throws {
        let evs = RealDay.shared.rebuild(["d": try reply(event + "," + departure)], now: 900)
        let ev = try XCTUnwrap(evs.first { $0.id == "event-3f9a1c2b7d10" }), dep = try XCTUnwrap(evs.first { $0.id == "departure-1" })
        XCTAssertEqual(ev.st, .next)
        XCTAssertEqual(ev.d, 60, "an event reaches from its start to its end")
        XCTAssertEqual(ev.s, "Study group")
        XCTAssertEqual(ev.src, "日历")
        XCTAssertNil(dep.d, "a departure is a moment")
        XCTAssertEqual(dep.mag, 0.8)
        XCTAssertEqual(dep.s, "12 · Stop A")
        XCTAssertTrue(dep.meta.contains("16:00") && dep.meta.contains("15:50"), dep.meta)
        XCTAssertEqual(dep.note, "在Stop A上车 · 去Downtown")
    }

    func testOnceItIsDoneAnEventIsAPastStar() throws {
        let done = event.replacingOccurrences(of: "\"next\"", with: "\"done\"")
        let evs = RealDay.shared.rebuild(["d": try reply(done)], now: 1200)
        XCTAssertEqual(evs.first?.st, .done)
    }

    func testTheAllDayLabelsAreForTheirDay() throws {
        let r = try reply("", #", "labels": [{"title": "Open", "source": "calendar"}, {"title": "Fair", "source": "calendar"}]"#)
        XCTAssertEqual(r.labels, [DayLabel(title: "Open", source: "calendar"), DayLabel(title: "Fair", source: "calendar")])
        _ = RealDay.shared.rebuild(["d": r], now: 900)
        XCTAssertEqual(RealDay.shared.dayLabel(0), "Open +1")
        XCTAssertNil(RealDay.shared.dayLabel(1))
        _ = RealDay.shared.rebuild(["d": try reply("", #", "labels": [{"title": "Open Day", "source": "calendar"}]"#)], now: 900)
        XCTAssertEqual(RealDay.shared.dayLabel(0), "Open Day")
        _ = RealDay.shared.rebuild(["d": try reply("", #", "labels": [{"title": "A very long all-day title indeed", "source": "calendar"}]"#)], now: 900)
        XCTAssertTrue(RealDay.shared.dayLabel(0)?.hasSuffix("…") == true, "cut to fit")
    }

    func testADayWithoutThemIsAsItWas() throws {
        let r = try reply("")
        XCTAssertEqual(r.labels, [])
        XCTAssertTrue(r.items.isEmpty)
        _ = RealDay.shared.rebuild(["d": r], now: 900)
        XCTAssertNil(RealDay.shared.dayLabel(0))
        // an old stay or work item has none of the new fields
        let old = try reply(#"{"id": "w", "lane": "you", "kind": "work", "state": "done", "start_ms": 0, "end_ms": 1}"#)
        XCTAssertNil(old.items[0].title)
        XCTAssertNil(old.items[0].leave_at_ms)
    }
}
