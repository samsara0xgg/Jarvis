import XCTest
@testable import Jarvis

/// 问她 on a stretch of what he was doing, by its window (ADR 0410, contracts/about.json); the stars at the time their highlight says; his body over a window beside his usual level
/// (ADR 0411, contracts/body/window.json). The brain is never asked.
@MainActor
final class DoingAboutAndBodyTests: XCTestCase {
    private func object(_ a: About) throws -> [String: Any] {
        try XCTUnwrap(JSONSerialization.jsonObject(with: JSONSerialization.data(withJSONObject: a.json)) as? [String: Any])
    }

    // MARK: 问她 on a stretch

    func testAStretchGoesOutAsTheContractsDoingCaseAndNoOtherKindCarriesAnEnd() throws {
        let doing = About(kind: "doing", id: "doing-1791997200000", title: "Jarvis", startMs: 1_791_997_200_000, endMs: 1_792_005_600_000)
        XCTAssertEqual(try object(doing) as NSDictionary, ["kind": "doing", "id": "doing-1791997200000", "title": "Jarvis", "start_ms": 1_791_997_200_000, "end_ms": 1_792_005_600_000] as NSDictionary,
                       "the contract's valid stretch, key for key")
        let stay = About(kind: "stay", id: "stay-cafe", title: "Cafe", startMs: 1_791_997_200_000, endMs: 1_792_005_600_000)
        XCTAssertNil(try object(stay)["end_ms"], "an end on another kind is a 422 / bad_say: it never goes out")
        XCTAssertTrue(About.kinds.contains("doing"))
        let body = try XCTUnwrap(JSONSerialization.jsonObject(with: BrainClient.submitBody("这段我在干嘛", attachments: [], about: doing)) as? [String: Any])
        XCTAssertEqual((body["about"] as? [String: Any])?["end_ms"] as? Int64, 1_792_005_600_000, "POST /inherent/submit carries it whole")
    }

    func testEveryStretchOfTheDayIsAskedAboutByItsWindowAndOffers问她() throws {
        let tl = TimelineState()
        tl.day = tl.today
        tl.fixture = .init(replies: [0: TimelineFixture.today(Cal.cur, doing: true)], now: TimelineFixture.now)
        let m = tl.model(try XCTUnwrap(tl.reply))
        let stretches = m.entries.filter { $0.kind == .segment }
        XCTAssertFalse(stretches.isEmpty)
        for e in stretches {
            let a = try XCTUnwrap(e.about, e.id)
            XCTAssertEqual(a.kind, "doing")
            XCTAssertEqual(a.id, e.id, "its id is the brain's `doing-<start_ms>`")
            XCTAssertGreaterThan(try XCTUnwrap(a.endMs, "one still going ends now"), a.startMs)
            XCTAssertTrue(e.actions.contains(.ask), "问她, as on any other row")
        }
        for h in m.entries.filter({ $0.header && $0.kind == .stay }) {
            XCTAssertEqual(h.about?.kind, "stay", "the group of stretches at a place still asks her about the place")
        }
    }

    // MARK: stars

    func testAStarStandsAtItsHighlightsTimeAndAnOlderBrainsIsFoundByItsId() {
        let row = TLEntry(id: "stay-1", kind: .stay, t0: 600, title: "Home")
        let m: (Int64) -> Double = { Double($0) / 60_000 }
        let stars = DayTimelineModel.starsOf([Highlight(id: "gone", label: "a", by: "him", start_ms: 720 * 60_000),
                                               Highlight(id: "stay-1", label: "b", by: "her", start_ms: 630 * 60_000),
                                               Highlight(id: "stay-1x", label: "c", by: "him"),
                                               Highlight(id: "stay-1", label: "d", by: "her")],
                                              rows: [row], stretches: [], span: 0...DAY, m)
        XCTAssertEqual(stars.map(\.t), [630, 720], "its time, even when its item is no row; without one only an id that joins is drawn")
        XCTAssertEqual(stars.map(\.id), ["stay-1", "gone"])
    }

    func testTheDaysHighlightsReadTheirStart() throws {
        let json = #"{"date":"2026-10-14","tz":"America/Vancouver","start_ms":1791961200000,"end_ms":1792047600000,"now_ms":1791993600000,"items":[],"missing":[],"highlights":[{"id":"stay-cafe","label":"Best flat white","start_ms":1791992100000,"by":"him"},{"id":"old","label":"x","by":"her"}]}"#
        let r = try JSONDecoder().decode(DayReply.self, from: Data(json.utf8))
        XCTAssertEqual(r.highlights.map(\.start_ms), [1_791_992_100_000, nil])
    }

    // MARK: 跟平时比

    /// contracts/body/window.json, example `today`, as it is
    private let today = #"{"tz":"America/Vancouver","start_ms":1792738800000,"end_ms":1792802160000,"now_ms":1792802160000,"baseline":{"days_before":14,"min_days":3,"rule":"the same clock hours on each of the 14 days before; his usual level is the median of what those days held, with its middle half (the 25th to the 75th percentile), when at least 3 of them hold any"},"measures":{"steps":{"kind":"total","unit":"steps","value":2500,"usual":{"median":4800,"low":4800,"high":4800,"days":14},"vs_usual":"below"},"heart_rate":{"kind":"reading","unit":"bpm","value":90,"usual":{"median":74,"low":71,"high":77,"days":14},"vs_usual":"above","readings":36,"low":84,"high":96},"resting_heart_rate":{"kind":"reading","unit":"bpm","value":55,"usual":{"median":null,"low":null,"high":null,"days":0},"vs_usual":null,"readings":1,"low":55,"high":55},"hrv":{"kind":"reading","unit":"ms","value":null,"usual":{"median":null,"low":null,"high":null,"days":0},"vs_usual":null,"readings":0,"low":null,"high":null},"breathing_rate":{"kind":"reading","unit":"/min","value":null,"usual":{"median":null,"low":null,"high":null,"days":0},"vs_usual":null,"readings":0,"low":null,"high":null},"blood_oxygen":{"kind":"reading","unit":"%","value":null,"usual":{"median":null,"low":null,"high":null,"days":0},"vs_usual":null,"readings":0,"low":null,"high":null},"sleep":{"kind":"sleep","unit":"min","value":420,"usual":{"median":450,"low":450,"high":450,"days":14},"vs_usual":"below","sleeps":1}}}"#

    func testOnlyAMeasureWithAValueAndAPlaceAgainstHisUsualIsALine() throws {
        let r = try JSONDecoder().decode(BodyReply.self, from: Data(today.utf8))
        let night = BodyStore.lines(r, NightStage.measures)
        XCTAssertEqual(night.map(\.id), ["sleep"], "resting heart rate has a value but no usual level; the rest no value")
        XCTAssertEqual(night.first?.mark, .below)
        XCTAssertEqual(night.first?.value, tlDur(420))
        let stretch = BodyStore.lines(r, ["steps", "heart_rate"])
        XCTAssertEqual(stretch.map(\.mark), [.below, .above])
        XCTAssertEqual(stretch.map(\.value), ["2,500" + Words.PhoneRow.stepsUnit, "90" + Words.PhoneRow.perMinute])
        XCTAssertEqual(BodyStore.lines(r, ["glucose"]), [], "a measure this phone does not know is kept out")
    }

    func testAWindowIsAskedOnceAndOneThatFailsShowsNothing() async throws {
        let store = BodyStore()
        var asked: [BodyWindow] = []
        let reply = try JSONDecoder().decode(BodyReply.self, from: Data(today.utf8))
        store.fetch = { w in asked.append(w); return w.startMs == 1 ? nil : reply }
        let w = BodyWindow(startMs: 1_792_738_800_000, endMs: 1_792_802_160_000), bad = BodyWindow(startMs: 1, endMs: 2)
        store.ensure(w); store.ensure(w); store.ensure(bad)
        for _ in 0..<100 where asked.count < 2 || store.reply(w) == nil { try await Task.sleep(nanoseconds: 10_000_000) }
        try await Task.sleep(nanoseconds: 50_000_000)
        store.ensure(w)
        XCTAssertEqual(asked, [w, bad], "a window is asked once while the app stays open")
        XCTAssertEqual(store.lines(w, ["steps"]).count, 1)
        XCTAssertEqual(store.lines(bad, NightStage.measures), [], "a failed ask: no lines, no error")
        store.ensure(BodyWindow(startMs: 5, endMs: 5))
        XCTAssertEqual(asked.count, 2, "an empty window is not asked")
    }
}
