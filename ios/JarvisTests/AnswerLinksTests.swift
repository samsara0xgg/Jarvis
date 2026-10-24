import XCTest
@testable import Jarvis

/// Her answer's links (ADR 0391, contracts/answer/links.json): read from the /phone/ws answer row and from the conversation's rows, carried to the chat row, and the rows of the day on
/// show they name lit.
@MainActor
final class AnswerLinksTests: XCTestCase {
    /// the contract's example
    private let two = #"[{"date":"2026-10-14","end_ms":1791993900000,"id":"stay-cafe","kind":"row","start_ms":1791992100000,"title":"街角咖啡馆"},{"date":"2026-10-14","end_ms":1791992100000,"id":"outing-1791991200000","kind":"outing","start_ms":1791991200000,"title":"走去咖啡馆"}]"#

    private func row(_ payload: String) throws -> RowFrame {
        let json = #"{"type":"row","event_type":"surface.response_emitted","event_uid":"6f1c2d3e4a5b46788899aabbccddeeff","ts_epoch_ms":1791650000000,"payload":\#(payload)}"#
        guard case .row(let r)? = HostFrame(Data(json.utf8)) else { throw XCTSkip("not a row frame") }
        return r
    }

    func testTheFinalAnswerRowsLinksComeInHerOrder() throws {
        _ = PhoneRows.take()
        PhoneRows.offer(try row(#"{"turn_id":"T1","text":"好","phase":"final","links":\#(two)}"#))
        guard case .links(let ls)? = PhoneRows.take().first else { return XCTFail("links") }
        XCTAssertEqual(ls, [AnswerLink(kind: .row, target: "stay-cafe", date: "2026-10-14", startMs: 1_791_992_100_000, endMs: 1_791_993_900_000, title: "街角咖啡馆"),
                            AnswerLink(kind: .outing, target: "outing-1791991200000", date: "2026-10-14", startMs: 1_791_991_200_000, endMs: 1_791_992_100_000, title: "走去咖啡馆")])
    }

    func testAnAnswerWithoutLinksOrACommentaryRowBringsNone() throws {
        _ = PhoneRows.take()
        PhoneRows.offer(try row(#"{"turn_id":"T1","text":"好","phase":"final"}"#))
        PhoneRows.offer(try row(#"{"turn_id":"T1","text":"我看看","phase":"commentary","links":\#(two)}"#))
        PhoneRows.offer(try row(#"{"turn_id":"T1","text":"好","links":[{"kind":"day","id":"x","date":"2026-10-14","start_ms":1},{"kind":"row","date":"10-14","start_ms":1}]}"#))
        XCTAssertEqual(PhoneRows.take(), [], "a link of another kind or a short date is not guessed at")
    }

    func testMoreThanThreeAreCutToHerFirstThree() {
        let raw = (1...4).map { ["kind": "row", "id": "r\($0)", "date": "2026-10-14", "start_ms": $0] as [String: Any] }
        XCTAssertEqual(AnswerLink.parse(raw).map(\.target), ["r1", "r2", "r3"])
    }

    func testAConversationRowCarriesItsLinksToItsChatRowAndHisCarryNone() throws {
        let json = #"{"rows":[{"seq":1,"source":"allen","ts":"2026-10-14T10:00:00","text":"上午去哪了","id":"rec-1"},{"seq":2,"source":"jarvis","ts":"2026-10-14T10:00:05","text":"街角咖啡馆。","id":"rec-2","links":\#(two)}]}"#
        let rows = try XCTUnwrap(ConversationRow.parse(Data(json.utf8)))
        XCTAssertEqual(rows.map(\.links.count), [0, 2])
        let shown = TalkStream.convDays(rows, cal: Calendar(identifier: .gregorian), now: Date()).flatMap(\.rows)
        XCTAssertEqual(shown.map(\.links.count), [0, 2], "the chat row has the chips")
        XCTAssertEqual(shown.last?.links.map(\.title), ["街角咖啡馆", "走去咖啡馆"])
    }

    func testHerLinksLightTheRowsTheyNameOnTheDayOnShowUntilThePageLeavesIt() throws {
        let tl = TimelineState()
        tl.day = tl.today
        tl.fixture = .init(replies: [0: TimelineFixture.today(Cal.cur)], now: TimelineFixture.now)
        let r = try XCTUnwrap(tl.reply), m = tl.model(r), c = TLClock(r), iso = Cal.cur.iso(tl.day)
        let stay = try XCTUnwrap(m.entries.first { $0.id == "stay-1" })
        // a row whose id is gone: the row that covers its start, the first that does
        let other = try XCTUnwrap(m.entries.first { e in e.id != stay.id && e.t1 != nil && m.entries.first { $0.t0 <= e.t0 + 1 && e.t0 + 1 <= ($0.t1 ?? $0.t0 + 1) }?.id == e.id })
        tl.light([AnswerLink(kind: .row, target: "stay-1", date: iso, startMs: 0, endMs: nil, title: ""),
                  AnswerLink(kind: .outing, target: "outing-gone", date: iso, startMs: c.ms(minute: other.t0 + 1), endMs: nil, title: ""),
                  AnswerLink(kind: .row, target: "stay-1", date: "2001-01-01", startMs: 0, endMs: nil, title: "")])
        XCTAssertEqual(tl.linked, [stay.id, other.id], "a link of another day lights nothing here")
        tl.letGo()
        XCTAssertEqual(tl.linked, [], "another day or another page: they go out")
    }

    func testARowsChipLightsItsRowAndOpensItWhereItStands() throws {
        let tl = TimelineState()
        tl.day = tl.today
        tl.fixture = .init(replies: [0: TimelineFixture.today(Cal.cur)], now: TimelineFixture.now)
        let r = try XCTUnwrap(tl.reply)
        tl.reveal(AnswerLink(kind: .row, target: "stay-1", date: Cal.cur.iso(tl.day), startMs: 0, endMs: nil, title: "在家"), in: r)
        XCTAssertEqual(tl.linked, ["stay-1"])
        XCTAssertEqual(tl.openRow, "stay-1")
    }
}
