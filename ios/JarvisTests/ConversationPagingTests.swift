import XCTest
@testable import Jarvis

/// The conversation in pages (ADR 0215, jarvis/state/memory_db.py `conversation_rows`): `before` goes back by `seq`, `has_more` says older rows exist.
final class ConversationPagingTests: XCTestCase {
    private func rows(_ range: ClosedRange<Int>) -> [ConversationRow] {
        range.map { ConversationRow(seq: $0, source: $0 % 2 == 0 ? "allen" : "jarvis", ts: "2026-10-09T10:00:00", text: "r\($0)") }
    }
    private func seqs(_ r: [ConversationRow]) -> [Int] { r.map(\.seq) }

    func testAPageReadsItsRowsAndWhetherOlderOnesExist() throws {
        let json = #"{"since": "2026-10-08T00:00:00", "has_more": true, "rows": [{"seq": 7, "ts": "2026-10-09T23:50:01", "source": "allen", "text": "a"}, {"seq": 8, "ts": "2026-10-09T23:50:04", "source": "jarvis", "text": "b"}]}"#
        let page = try XCTUnwrap(ConversationPage.parse(Data(json.utf8)))
        XCTAssertEqual(seqs(page.rows), [7, 8])
        XCTAssertTrue(page.hasMore, "older rows exist, below the history floor too")
        XCTAssertFalse(try XCTUnwrap(ConversationPage.parse(Data(#"{"rows": []}"#.utf8))).hasMore, "a brain that does not say has none")
        XCTAssertNil(ConversationPage.parse(Data("{}".utf8)))
    }

    /// `before` and `after` together are a 400: the request carries one cursor
    func testTheRequestCarriesOneCursor() {
        func q(_ a: Int, _ b: Int) -> [String] { BrainClient.conversationQuery(after: a, before: b, limit: 200).map { "\($0.name)=\($0.value ?? "")" } }
        XCTAssertEqual(q(0, 0), ["limit=200", "after=0"], "the newest page")
        XCTAssertEqual(q(41, 0), ["limit=200", "after=41"], "the rows past one")
        XCTAssertEqual(q(0, 41), ["limit=200", "before=41"], "the page before the oldest row held")
        XCTAssertEqual(q(9, 41), ["limit=200", "before=41"], "never both")
    }

    func testAnOlderPageGoesInFrontAndOnlyWhatIsOlder() {
        let held = rows(101...124)
        let m = ConversationPaging.prepend(held: held, older: ConversationPage(rows: rows(61...100), hasMore: true))
        XCTAssertEqual(seqs(m.rows), Array(61...124))
        XCTAssertTrue(m.hasMore)
        let overlap = ConversationPaging.prepend(held: held, older: ConversationPage(rows: rows(98...103), hasMore: false))
        XCTAssertEqual(seqs(overlap.rows), Array(98...124), "a row already held is not listed twice")
        XCTAssertFalse(overlap.hasMore)
    }

    func testAPageThatBringsNothingEndsTheScrollBack() {
        let m = ConversationPaging.prepend(held: rows(101...124), older: ConversationPage(rows: [], hasMore: true))
        XCTAssertEqual(seqs(m.rows), Array(101...124))
        XCTAssertFalse(m.hasMore, "otherwise the panel would ask again and again")
    }

    func testTheNewestPageReadAgainKeepsTheOlderRowsHeld() {
        let held = rows(61...124)                                                              // two pages scrolled back to
        let m = ConversationPaging.merge(held: held, hasMore: true, newest: ConversationPage(rows: rows(105...126), hasMore: true))
        XCTAssertEqual(seqs(m.rows), Array(61...126), "two rows arrived; what was scrolled to stays")
        XCTAssertTrue(m.hasMore)
        let none = ConversationPaging.merge(held: held, hasMore: false, newest: ConversationPage(rows: rows(105...124), hasMore: true))
        XCTAssertEqual(seqs(none.rows), Array(61...124))
        XCTAssertFalse(none.hasMore, "the scroll-back already reached the first record")
    }

    func testTheFirstReadAndAGapStartOverFromTheNewestPage() {
        let first = ConversationPaging.merge(held: [], hasMore: false, newest: ConversationPage(rows: rows(101...124), hasMore: true))
        XCTAssertEqual(seqs(first.rows), Array(101...124))
        XCTAssertTrue(first.hasMore)
        let gap = ConversationPaging.merge(held: rows(1...50), hasMore: false, newest: ConversationPage(rows: rows(301...500), hasMore: true))
        XCTAssertEqual(seqs(gap.rows), Array(301...500), "more than a page arrived since: keeping the old rows would leave a hole")
        XCTAssertTrue(gap.hasMore)
        XCTAssertTrue(ConversationPaging.merge(held: rows(1...5), hasMore: true, newest: ConversationPage(rows: [], hasMore: false)).rows.isEmpty, "the brain keeps no conversation any more")
    }
}
