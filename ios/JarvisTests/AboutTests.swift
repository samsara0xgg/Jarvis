import XCTest
@testable import Jarvis

/// The item he has open, as the brain takes it (ADR 0214, jarvis/shared/about.py): kept whole or not sent, on a typed turn's body.
final class AboutTests: XCTestCase {
    private func star(id: String = "R-1", kind: String? = "reminder", ms: Int64? = 1_791_000_000_000, live: Bool = true) -> Ev {
        let e = Ev(id, 900, .her, .next, "提醒：给 Mia 回电话", "15:00")
        e.live = live; e.kind = kind; e.startMs = ms
        return e
    }

    func testTheWireObjectHasTheFourKeysTheHostReads() throws {
        let a = try XCTUnwrap(About.of(star()))
        let o = try JSONSerialization.jsonObject(with: JSONSerialization.data(withJSONObject: a.json)) as? [String: Any]
        XCTAssertEqual(Set(o?.keys.map { $0 } ?? []), ["kind", "id", "title", "start_ms"], "the host refuses any other key")
        XCTAssertEqual(o?["kind"] as? String, "reminder")
        XCTAssertEqual(o?["id"] as? String, "R-1")
        XCTAssertEqual(o?["title"] as? String, "提醒：给 Mia 回电话")
        XCTAssertEqual((o?["start_ms"] as? NSNumber)?.int64Value, 1_791_000_000_000, "an integer epoch ms, not a double")
    }

    func testTheSubmitBodyCarriesItOnlyWhileTheChipShows() throws {
        let with = try BrainClient.submitBody("把它推到明天", attachments: [], about: About.of(star()))
        let o = try XCTUnwrap(JSONSerialization.jsonObject(with: with) as? [String: Any])
        XCTAssertEqual(o["text"] as? String, "把它推到明天")
        XCTAssertEqual((o["about"] as? [String: Any])?["id"] as? String, "R-1")
        let without = try XCTUnwrap(JSONSerialization.jsonObject(with: BrainClient.submitBody("嗨", attachments: ["f1"], about: nil)) as? [String: Any])
        XCTAssertNil(without["about"], "no chip, no key")
        XCTAssertEqual(without["attachments"] as? [String], ["f1"])
    }

    /// a cut id names a different item: a long one goes whole, and one the host would refuse is never offered
    func testAnIdGoesWholeOrTheChipIsNotOffered() throws {
        let long = String(repeating: "a", count: 256)
        XCTAssertEqual(About.of(star(id: long))?.json["id"] as? String, long)
        XCTAssertNil(About.of(star(id: long + "a")), "257 characters")
        XCTAssertNil(About.of(star(id: "")))
        XCTAssertNil(About.of(star(id: "a b")), "a space is not in the id set")
        XCTAssertNil(About.of(star(id: "提醒")))
        XCTAssertNotNil(About.of(star(id: "AAMk-=+/.|:_1")), "a Graph-like id")
    }

    func testOnlyAnItemOfTheBrainsDayIsOffered() {
        XCTAssertNil(About.of(star(live: false)), "a prototype star")
        XCTAssertNil(About.of(star(kind: nil)))
        XCTAssertNil(About.of(star(kind: "flight")), "a kind the host does not know")
        XCTAssertNil(About.of(star(ms: nil)))
        for k in ["reminder", "event", "todo", "stay", "move", "sleep", "work", "call", "talk"] { XCTAssertNotNil(About.of(star(kind: k)), k) }
    }

    func testTheChipWritesTheDayOnlyOffToday() throws {
        var c = Calendar(identifier: .gregorian); c.timeZone = TimeZone(identifier: "Asia/Shanghai")!
        let f = DateFormatter(); f.calendar = c; f.timeZone = c.timeZone; f.locale = Locale(identifier: "en_US_POSIX"); f.dateFormat = "yyyy-MM-dd'T'HH:mm"
        let at = f.date(from: "2026-10-11T15:00")!
        let a = About(kind: "reminder", id: "R", title: "t", startMs: Int64(at.timeIntervalSince1970 * 1000))
        XCTAssertEqual(a.when(c, f.date(from: "2026-10-11T09:00")!), "15:00")
        XCTAssertEqual(a.when(c, f.date(from: "2026-10-10T09:00")!), "明天 15:00")
        XCTAssertEqual(a.when(c, f.date(from: "2026-10-09T09:00")!), "10月11日 周日 15:00")
        XCTAssertEqual(a.when(c, f.date(from: "2026-10-12T09:00")!), "昨天 15:00")
    }
}
