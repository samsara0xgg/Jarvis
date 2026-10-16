import XCTest
import CoreLocation
@testable import Jarvis

/// The places an answer names (`places` on the final response_emitted row and on the conversation's rows) and the camera that holds them.
final class MapPlacesTests: XCTestCase {
    private func row(_ payload: String) throws -> RowFrame {
        let json = #"{"type":"row","event_type":"surface.response_emitted","event_uid":"6f1c2d3e4a5b46788899aabbccddeeff","ts_epoch_ms":1791650000000,"payload":\#(payload)}"#
        guard case .row(let r)? = HostFrame(Data(json.utf8)) else { throw XCTSkip("not a row frame") }
        return r
    }

    func testTheFinalAnswerRowsPlacesAreReadWithTheirFields() throws {
        let r = try row(#"{"turn_id":"T1","text":"好","phase":"final","places":[{"name":"Willows Beach","lat":48.4417,"lng":-123.2967,"address":"Beach Dr","place_id":"P1","kind":"place"},{"name":"Gyro Park","lat":48.4553,"lng":-123.3079,"address":null,"place_id":null,"kind":"place"}]}"#)
        let ps = PhoneRows.places(r)
        XCTAssertEqual(ps.map(\.name), ["Willows Beach", "Gyro Park"])
        XCTAssertEqual(ps[0], AnswerPlace(name: "Willows Beach", lat: 48.4417, lng: -123.2967, address: "Beach Dr", placeId: "P1"))
        XCTAssertNil(ps[1].address); XCTAssertNil(ps[1].placeId)
    }

    @MainActor func testARowWithPlacesQueuesThemAndKeepsItsTimesToo() throws {
        _ = PhoneRows.take()
        PhoneRows.offer(try row(#"{"turn_id":"T1","phase":"final","times":[{"kind":"reminder","at_ms":1791702000000,"label":"x","ref":"R"}],"places":[{"name":"A","lat":1,"lng":2,"kind":"place"}]}"#))
        let got = PhoneRows.take()
        XCTAssertEqual(got.count, 2)
        guard case .times? = got.first, case .places(let ps)? = got.last else { return XCTFail("times then places") }
        XCTAssertEqual(ps.map(\.name), ["A"])
    }

    func testNoPlacesFromACommentaryRowOrAnotherRowType() throws {
        XCTAssertTrue(PhoneRows.places(try row(#"{"turn_id":"T1","phase":"commentary","places":[{"name":"A","lat":1,"lng":2,"kind":"place"}]}"#)).isEmpty)
        let chunk = RowFrame(eventType: "surface.response_chunk", eventUid: "u", payload: ["places": [["name": "A", "lat": 1, "lng": 2]]], tsEpochMs: 0)
        XCTAssertTrue(PhoneRows.places(chunk).isEmpty)
    }

    func testAnEntryThatIsNotAPlaceIsDroppedNotGuessed() {
        let raw: [[String: Any]] = [
            ["name": "ok", "lat": 10.0, "lng": 20.0, "kind": "place"],
            ["name": "", "lat": 10.0, "lng": 20.0],                     // no name
            ["name": "off the globe", "lat": 95.0, "lng": 20.0],
            ["name": "no lng", "lat": 10.0],
            ["name": "route", "lat": 10.0, "lng": 20.0, "kind": "route"],  // a kind this phone does not know
            ["name": "ok", "lat": 10.0, "lng": 20.0],                      // the same place again
        ]
        XCTAssertEqual(AnswerPlace.parse(raw).map(\.name), ["ok"])
        XCTAssertTrue(AnswerPlace.parse(nil).isEmpty)
        XCTAssertTrue(AnswerPlace.parse("places").isEmpty)
    }

    func testAtMostEightPlaces() {
        let raw: [[String: Any]] = (0..<12).map { ["name": "p\($0)", "lat": Double($0), "lng": 1.0, "kind": "place"] }
        XCTAssertEqual(AnswerPlace.parse(raw).count, AnswerPlace.maxCount)
        XCTAssertEqual(AnswerPlace.parse(raw).last?.name, "p7")
    }

    func testAConversationRowCarriesItsPlacesAndTheStreamGivesThemToHerRowOnly() throws {
        let json = #"{"rows":[{"seq":1,"source":"allen","ts":"2026-10-14T10:00:00","text":"附近有什么"},{"seq":2,"source":"jarvis","ts":"2026-10-14T10:00:05","text":"这几个","places":[{"name":"A","lat":1,"lng":2,"kind":"place"}]},{"seq":3,"source":"allen","ts":"2026-10-14T10:01:00","text":"谢谢"}]}"#
        let rows = try XCTUnwrap(ConversationRow.parse(Data(json.utf8)))
        XCTAssertEqual(rows[1].places.map(\.name), ["A"])
        XCTAssertTrue(rows[0].places.isEmpty)
        let shown = TalkStream.convDays(rows, cal: Calendar(identifier: .gregorian), now: Date(timeIntervalSince1970: 1_792_000_000)).flatMap(\.rows)
        XCTAssertEqual(shown.map { $0.places.count }, [0, 1, 0])
    }

    func testALiveTurnsPlacesGoToItsTalkRow() {
        var t = LiveTurn(id: 1, his: false, text: "这几个")
        t.places = [AnswerPlace(name: "A", lat: 1, lng: 2, address: nil, placeId: nil)]
        XCTAssertEqual(TalkStream.row(t).places, t.places)
    }

    // MARK: the camera

    func testTheCameraHoldsEveryPlaceAndNoTighterThanAFewStreets() {
        let a = AnswerPlace(name: "A", lat: 48.40, lng: -123.30, address: nil, placeId: nil), b = AnswerPlace(name: "B", lat: 48.46, lng: -123.36, address: nil, placeId: nil)
        let r = PlaceFit.region([a, b], me: nil)
        for p in [a, b] {
            XCTAssertLessThanOrEqual(abs(p.lat - r.center.latitude), r.span.latitudeDelta / 2)
            XCTAssertLessThanOrEqual(abs(p.lng - r.center.longitude), r.span.longitudeDelta / 2)
        }
        XCTAssertGreaterThan(r.span.latitudeDelta, 0.06, "room around the outermost places")
        XCTAssertEqual(PlaceFit.region([a], me: nil).span.latitudeDelta, PlaceFit.minSpan, "one place: no closer than a few streets")
    }

    func testHeIsInTheFrameWhenNearAndLeftOutWhenFarAway() {
        let a = AnswerPlace(name: "A", lat: 48.45, lng: -123.30, address: nil, placeId: nil)
        let near = CLLocationCoordinate2D(latitude: 48.47, longitude: -123.30), far = CLLocationCoordinate2D(latitude: 49.28, longitude: -123.12)
        XCTAssertEqual(PlaceFit.region([a], me: near).center.latitude, 48.46, accuracy: 0.001, "the middle of him and the place")
        XCTAssertEqual(PlaceFit.region([a], me: far).center.latitude, 48.45, accuracy: 0.001, "a city away he would shrink the place to a dot")
    }

    // MARK: Google Maps hand-offs

    func testDirectionsURLWithPlaceIdUsesTheEncodedNameAndTheId() {
        let p = AnswerPlace(name: "维多利亚 Beach & Café", lat: 48.4, lng: -123.3, address: nil, placeId: "ChIJ_abc-1")
        XCTAssertEqual(PlaceHandoff.directionsURL(p)?.absoluteString,
                       "https://www.google.com/maps/dir/?api=1&destination=%E7%BB%B4%E5%A4%9A%E5%88%A9%E4%BA%9A%20Beach%20%26%20Caf%C3%A9&destination_place_id=ChIJ_abc-1&travelmode=transit")
    }

    func testDirectionsURLWithoutPlaceIdFallsBackToTheCoordinate() {
        let p = AnswerPlace(name: "海边", lat: 48.4417, lng: -123.2967, address: nil, placeId: nil)
        XCTAssertEqual(PlaceHandoff.directionsURL(p)?.absoluteString,
                       "https://www.google.com/maps/dir/?api=1&destination=48.441700,-123.296700&travelmode=transit")
    }

    func testSearchURLWithPlaceIdUsesTheEncodedNameAndTheId() {
        let p = AnswerPlace(name: "威洛斯海滩", lat: 48.4, lng: -123.3, address: nil, placeId: "P1")
        XCTAssertEqual(PlaceHandoff.searchURL(p)?.absoluteString,
                       "https://www.google.com/maps/search/?api=1&query=%E5%A8%81%E6%B4%9B%E6%96%AF%E6%B5%B7%E6%BB%A9&query_place_id=P1")
    }

    func testSearchURLWithoutPlaceIdFallsBackToTheCoordinate() {
        let p = AnswerPlace(name: "海边", lat: 48.4417, lng: -123.2967, address: nil, placeId: nil)
        XCTAssertEqual(PlaceHandoff.searchURL(p)?.absoluteString, "https://www.google.com/maps/search/?api=1&query=48.441700,-123.296700")
    }
}
