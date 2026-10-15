import XCTest
@testable import Jarvis

/// The bus ride's Live Activity as the brain pushes it (jarvis/runtime/rides.py `_attributes` and `_content`, ADR 0224).
final class RideActivityTests: XCTestCase {
    private func state(_ json: String) throws -> RideAttributes.ContentState {
        try JSONDecoder().decode(RideAttributes.ContentState.self, from: Data(json.utf8))
    }

    func testTheContentStateTheBrainSendsWhileRidingDecodes() throws {
        let s = try state(#"{"route":"32","next_stop":"光华路","stops_left":4,"minutes_to_alight":6,"alight_stop":"国贸","position_live":true,"arrival_live":true,"phase":"riding"}"#)
        XCTAssertEqual(s.route, "32")
        XCTAssertEqual(s.nextStop, "光华路")
        XCTAssertEqual(s.stopsLeft, 4)
        XCTAssertEqual(s.minutesToAlight, 6)
        XCTAssertEqual(s.alightStop, "国贸")
        XCTAssertFalse(s.isArriving)
        XCTAssertTrue(s.hasLivePosition)
        XCTAssertTrue(s.hasLiveArrival)
        XCTAssertEqual(s.minutesText, "6 分钟")
        XCTAssertEqual(s.trustNote, "")
    }

    func testNullsAndAnUnknownKeyDecode() throws {
        // no progress yet (`prog` is None): everything but route, alight_stop, the two flags and phase is null; a key the phone has never heard of is ignored
        let s = try state(#"{"route":"32","next_stop":null,"stops_left":null,"minutes_to_alight":null,"alight_stop":null,"position_live":false,"arrival_live":false,"phase":"riding","eta_ms":1760000000000}"#)
        XCTAssertNil(s.nextStop)
        XCTAssertNil(s.stopsLeft)
        XCTAssertNil(s.minutesToAlight)
        XCTAssertNil(s.alightStop)
        XCTAssertNil(s.minutesText)
        XCTAssertFalse(s.hasLivePosition)
        XCTAssertEqual(s.trustNote, "没有车的实时位置，按时刻表")
    }

    func testALiveBusWithHisStopOnlyInTheTimetableSaysSo() throws {
        let s = try state(#"{"route":"32","next_stop":"光华路","stops_left":4,"minutes_to_alight":5,"alight_stop":"国贸","position_live":true,"arrival_live":false,"phase":"riding"}"#)
        XCTAssertTrue(s.hasLivePosition)
        XCTAssertFalse(s.hasLiveArrival)
        XCTAssertEqual(s.minutesText, "约 5 分钟")
        XCTAssertEqual(s.trustNote, "到站时间按时刻表估算")
    }

    func testAnOlderBrainsStaleFlagIsReadOnlyWhenTheTwoFactsAreAbsent() throws {
        let old = try state(#"{"route":"32","minutes_to_alight":5,"stale":true,"phase":"riding"}"#)
        XCTAssertFalse(old.hasLivePosition)
        XCTAssertFalse(old.hasLiveArrival)
        let fresh = try state(#"{"route":"32","minutes_to_alight":5,"stale":false,"phase":"riding"}"#)
        XCTAssertTrue(fresh.hasLivePosition)
        XCTAssertTrue(fresh.hasLiveArrival)
        let both = try state(#"{"route":"32","minutes_to_alight":5,"stale":false,"position_live":false,"arrival_live":false,"phase":"riding"}"#)
        XCTAssertFalse(both.hasLivePosition, "the two facts win over stale")
        let missing = try state(#"{"route":"32"}"#)
        XCTAssertFalse(missing.hasLivePosition, "the brain's own rule: nothing known is not live")
    }

    func testTheWarningOneStopBeforeIsArriving() throws {
        let s = try state(#"{"route":"32","next_stop":"国贸","stops_left":1,"minutes_to_alight":1,"alight_stop":"国贸","position_live":true,"arrival_live":true,"phase":"arriving"}"#)
        XCTAssertTrue(s.isArriving)
        XCTAssertEqual(s.minutesText, "1 分钟")
    }

    func testTheEndStateDecodes() throws {
        let s = try state(#"{"route":"32","next_stop":null,"stops_left":null,"minutes_to_alight":null,"alight_stop":"国贸","position_live":false,"arrival_live":false,"phase":"ended"}"#)
        XCTAssertTrue(s.isEnded)
    }

    func testAMinuteCountOfZeroSaysItIsHere() throws {
        XCTAssertEqual(try state(#"{"route":"32","minutes_to_alight":0,"position_live":true,"arrival_live":true}"#).minutesText, "马上到")
    }

    func testTheAttributesOfAPushToStartDecode() throws {
        let a = try JSONDecoder().decode(RideAttributes.self, from: Data(#"{"ride_id":"R1a2b","route":"32","board_stop":"西直门","started_at_ms":1760000000000}"#.utf8))
        XCTAssertEqual(a.rideId, "R1a2b")
        XCTAssertEqual(a.route, "32")
        XCTAssertEqual(a.boardStop, "西直门")
        XCTAssertEqual(a.startedAtMs, 1_760_000_000_000)
        let bare = try JSONDecoder().decode(RideAttributes.self, from: Data(#"{"ride_id":"R1a2b","route":"32","board_stop":null,"started_at_ms":1760000000000}"#.utf8))
        XCTAssertNil(bare.boardStop)
    }

    func testTheRideTokensAreToldToTheBrainUnderItsKeys() throws {
        let body = PushWire.registration(deviceToken: "ab12", environment: "sandbox", activity: .keep, rideStart: .set("cd34"), rideActivity: .clear)
        let json = String(decoding: try JSONSerialization.data(withJSONObject: body, options: [.sortedKeys]), as: UTF8.self)
        XCTAssertEqual(json, #"{"device_token":"ab12","environment":"sandbox","ride_activity_token":null,"ride_start_token":"cd34"}"#)
        let plain = PushWire.registration(deviceToken: "ab12", environment: "sandbox", activity: .keep) as NSDictionary
        XCTAssertEqual(plain, ["device_token": "ab12", "environment": "sandbox"], "no ride key keeps the stored tokens")
    }
}
