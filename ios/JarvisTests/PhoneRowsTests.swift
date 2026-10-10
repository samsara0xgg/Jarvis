import XCTest
@testable import Jarvis

/// What /phone/ws carries besides her words (ADR 0215, jarvis/surface/phone_link.py), read from the frames as the host writes them.
final class PhoneRowsTests: XCTestCase {
    private func row(_ type: String, _ payload: String) throws -> RowFrame {
        let json = #"{"type":"row","event_type":"\#(type)","event_uid":"6f1c2d3e4a5b46788899aabbccddeeff","ts_epoch_ms":1791650000000,"payload":\#(payload)}"#
        guard case .row(let r)? = HostFrame(Data(json.utf8)) else { throw XCTSkip("not a row frame") }
        return r
    }

    func testAnAskCardRowIsACardWithItsFieldsAndItsOwnButtons() throws {
        let r = try row("clarification.requested", #"{"clarification_id":"Q1","turn_id":"T1","action_id":"A1","question":"哪一天，几位？","fields":[{"label":"哪一天","choices":["周五","周六"]},{"label":"几位"}]}"#)
        guard case .card(let c)? = PhoneRows.event(r) else { return XCTFail("a card") }
        XCTAssertEqual(c.brainId, "Q1")
        XCTAssertEqual(c.kind, .clarification)
        XCTAssertEqual(c.title, "哪一天，几位？")
        XCTAssertEqual(c.fields, [CardField(label: "哪一天", choices: ["周五", "周六"]), CardField(label: "几位", choices: [])])
        XCTAssertEqual(c.actions.map(\.answer), [.dismiss, .fill])
    }

    /// a bus card (ADR 0205) has no fields and rows to read: it is closed, not answered
    func testABusCardShowsItsOptionsAndOnlyCloses() throws {
        let r = try row("clarification.requested", #"{"clarification_id":"O1","turn_id":"T1","question":"西二旗","fields":[],"trip":{"to":"西二旗","made_at":"2026-10-10T08:00:00+08:00","options":[{"index":1,"route":"32 路","board_stop":"中关村","leave_at":"08:12","departs":"08:20","arrive_at":"08:52"},{"index":2,"route":"4 路","board_stop":"海淀","leave_at":"08:30","departs":"08:37","arrive_at":"09:05"}]}}"#)
        guard case .card(let c)? = PhoneRows.event(r) else { return XCTFail("a card") }
        XCTAssertEqual(c.title, "西二旗")
        XCTAssertEqual(c.trip, ["08:12 出发 · 32 路 · 中关村 → 08:52", "08:30 出发 · 4 路 · 海淀 → 09:05"])
        XCTAssertEqual(c.actions.map(\.answer), [.dismiss])
        XCTAssertTrue(c.fields.isEmpty)
    }

    func testAWithdrawnCardNamesTheCardThatWentAway() throws {
        guard case .cardGone(let id)? = PhoneRows.event(try row("clarification.withdrawn", #"{"clarification_id":"Q1","turn_id":"T1"}"#)) else { return XCTFail("gone") }
        XCTAssertEqual(id, "Q1")
        XCTAssertNil(PhoneRows.event(try row("clarification.withdrawn", #"{"turn_id":"T1"}"#)), "no id, nothing to take down")
    }

    /// her words are the voice session's, and a card is never her words
    func testHerWordsAreNotCardRows() throws {
        XCTAssertNil(PhoneRows.event(try row("surface.response_chunk", #"{"turn_id":"T1","text":"好"}"#)))
        XCTAssertNil(PhoneRows.event(try row("surface.response_emitted", #"{"turn_id":"T1","text":"好"}"#)))
    }
}
