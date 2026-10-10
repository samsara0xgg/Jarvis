import XCTest
@testable import Jarvis

/// What the phone tells the brain about its APNs tokens (docs/spec.html `POST /inherent/device/push`).
final class PushRegistrationTests: XCTestCase {
    func testTheTokenIsLowercaseHexWithoutSeparators() {
        XCTAssertEqual(PushWire.hex(Data([0x00, 0x0a, 0xAB, 0xff, 0x10])), "000aabff10")
        XCTAssertEqual(PushWire.hex(Data()), "")
        XCTAssertEqual(PushWire.hex(Data(repeating: 0xCD, count: 32)).count, 64, "a device token is 64 characters; the brain takes 32 to 512")
    }

    func testRegistrationCarriesTheDeviceTokenAndEnvironmentAndNothingElse() {
        let body = PushWire.registration(deviceToken: "ab12", environment: "sandbox", activity: .keep) as NSDictionary
        XCTAssertEqual(body, ["device_token": "ab12", "environment": "sandbox"], "no live_activity_token key keeps the stored one")
    }

    func testLiveActivityTokenIsSetOrClearedWithNull() {
        let set = PushWire.registration(deviceToken: "ab12", environment: "production", activity: .set("cd34")) as NSDictionary
        XCTAssertEqual(set, ["device_token": "ab12", "environment": "production", "live_activity_token": "cd34"])
        let clear = PushWire.registration(deviceToken: "ab12", environment: "sandbox", activity: .clear)
        XCTAssertTrue(clear["live_activity_token"] is NSNull)
        let json = String(decoding: (try? JSONSerialization.data(withJSONObject: clear, options: [.sortedKeys])) ?? Data(), as: UTF8.self)
        XCTAssertEqual(json, #"{"device_token":"ab12","environment":"sandbox","live_activity_token":null}"#)
    }

    private func profile(_ aps: String?) -> Data {
        let entitlements = aps.map { "<key>Entitlements</key><dict><key>aps-environment</key><string>\($0)</string></dict>" } ?? ""
        let xml = "<?xml version=\"1.0\" encoding=\"UTF-8\"?><plist version=\"1.0\"><dict><key>Name</key><string>x</string>\(entitlements)</dict></plist>"
        // a provisioning profile is a signed blob around the plist
        return Data([0x30, 0x82, 0x01, 0xff]) + Data(xml.utf8) + Data([0x00, 0x01, 0x02])
    }

    func testTheEnvironmentIsReadFromTheProfileTheBuildIsSignedWith() {
        XCTAssertEqual(PushWire.environment(inProfile: profile("development")), "sandbox")
        XCTAssertEqual(PushWire.environment(inProfile: profile("production")), "production")
        XCTAssertNil(PushWire.environment(inProfile: profile(nil)))
        XCTAssertNil(PushWire.environment(inProfile: Data("not a profile".utf8)))
    }

    func testThisBuildNamesAnEnvironmentTheBrainAccepts() {
        XCTAssertTrue(["sandbox", "production"].contains(PushWire.environment))
    }
}
