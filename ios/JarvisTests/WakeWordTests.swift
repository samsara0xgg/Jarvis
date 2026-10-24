import XCTest
@testable import Jarvis

/// The phone's wake word is the Mac's, "Hey Jarvis" (ADR 0042), as Apple's recognizer writes it in either language.
final class WakeWordTests: XCTestCase {
    func testHeyJarvisWakesHerAsEitherRecognizerWritesIt() {
        for t in ["Hey Jarvis, what time is it", "hey, jarvis", "嘿，贾维斯，你好", "嘿Jarvis", "嗨 贾维斯"] { XCTAssertTrue(heardWake(t), t) }
    }

    func testTheNameAloneDoesNotWakeHer() {
        for t in ["Jarvis, what time is it", "贾维斯你好", "hello", "hey there"] { XCTAssertFalse(heardWake(t), t) }
    }
}
