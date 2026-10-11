import XCTest
@testable import Jarvis

/// Where he was: the kept place in a stay, the name the dial writes for it, and the hue it gets.
final class PlaceTintTests: XCTestCase {
    private func stay(_ extra: String) throws -> DayItem {
        let day = #"{"date": "2026-10-11", "tz": "UTC", "start_ms": 0, "end_ms": 1, "now_ms": 1, "missing": [], "items": [{"id": "s", "lane": "you", "kind": "stay", "state": "done", "start_ms": 0, "end_ms": 1\#(extra)}]}"#
        return try XCTUnwrap(JSONDecoder().decode(DayReply.self, from: Data(day.utf8)).items.first)
    }

    func testAStayWithoutAKeptPlaceDecodesAsBefore() throws {
        let i = try stay(#", "place": "Corner Library, 3 Example Ave""#)
        XCTAssertNil(i.kept_place)
        XCTAssertEqual(i.placeLabel, "Corner Library, 3 Example Ave", "the caption keeps the phone's whole label")
    }

    func testTheKeptPlaceIsDecoded() throws {
        let i = try stay(#", "place": "12 Example St, Town", "kept_place": {"id": "p-home", "name": "Home", "role": "home"}"#)
        XCTAssertEqual(i.kept_place, KeptPlace(id: "p-home", name: "Home", role: "home"))
    }

    func testTheKeptNameBeatsThePhonesLabel() throws {
        let i = try stay(#", "place": "12 Example St, Town", "kept_place": {"id": "p-home", "name": "Home", "role": "home"}"#)
        XCTAssertEqual(i.placeLabel, "Home", "the caption under the time")
        XCTAssertEqual(i.ringName?.name, "Home")
        XCTAssertEqual(i.ringName?.kept, true)
    }

    func testWithoutAKeptPlaceTheDialWritesTheFirstPartOfTheLabelDimmer() throws {
        let i = try stay(#", "place": "Corner Library, 3 Example Ave, Town""#)
        XCTAssertEqual(i.ringName?.name, "Corner Library")
        XCTAssertEqual(i.ringName?.kept, false)
        XCTAssertEqual(try stay(#", "place": "Café，Main St""#).ringName?.name, "Café", "a full-width comma cuts too")
        XCTAssertEqual(try stay(#", "place": "  ,x""#).ringName == nil, true)
        XCTAssertNil(try stay("").ringName)
        XCTAssertNil(try stay("").placeLabel)
        XCTAssertNil(try stay(#", "place": """#).placeLabel)
    }

    func testHomeIsWarmSchoolIsBlueAndOtherPlacesKeepTheirHue() {
        XCTAssertEqual(PlaceTint.colour(role: "home", id: "a"), .warm)
        XCTAssertEqual(PlaceTint.colour(role: "school", id: "a"), .work)
        let other = PlaceTint.colour(role: "other", id: "p-cafe")
        XCTAssertEqual(PlaceTint.colour(role: "other", id: "p-cafe"), other, "the same place is always the same hue")
        XCTAssertEqual(PlaceTint.colour(role: nil, id: "p-cafe"), other, "a role it does not know is another place")
        XCTAssertNotEqual(other, .warm)
        XCTAssertNotEqual(other, .work)
        // FNV-1a of "a", a published value: the pick does not depend on the launch
        XCTAssertEqual(PlaceTint.fnv("a"), 0xE40C292C)
    }
}
