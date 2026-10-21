import XCTest
@testable import Jarvis

/// Only a phone senses (ADR 0336): an iPad left at home must never report where it lies, nor send Health that iCloud synced to it.
final class DeviceRoleTests: XCTestCase {
    func testOnlyAPhoneSenses() {
        XCTAssertTrue(DeviceRole.senses(on: .phone))
        XCTAssertFalse(DeviceRole.senses(on: .pad))
        XCTAssertFalse(DeviceRole.senses(on: .mac))
    }
}
