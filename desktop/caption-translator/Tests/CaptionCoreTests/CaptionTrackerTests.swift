import XCTest
@testable import CaptionCore

final class CaptionTrackerTests: XCTestCase {
    func testUnchangedFrameDoesNotRetranslate() {
        var tracker = CaptionTracker()
        XCTAssertEqual(tracker.ingest("We feel understanding, we feel empathetic."), "We feel understanding, we feel empathetic.")
        XCTAssertNil(tracker.ingest("We feel understanding,\nwe feel empathetic."))
        XCTAssertNil(tracker.ingest("  "))
    }

    func testPartialCaptionIsRevisedRatherThanRepeated() {
        var tracker = CaptionTracker()
        _ = tracker.ingest("We feel understanding, we feel empathetic. We can put ourself")
        let result = tracker.ingest("We feel understanding, we feel empathetic. We can put ourselves in someone else's shoes.")
        XCTAssertEqual(result, "We feel understanding, we feel empathetic. We can put ourselves in someone else's shoes.")
    }

    func testScrollingRetainsContextWithoutDuplicatingTheOverlap() {
        var tracker = CaptionTracker()
        _ = tracker.ingest("There is compassion, where we feel open. We feel understanding, we feel empathetic.")
        let result = tracker.ingest("We feel understanding, we feel empathetic. We can put ourselves in someone else's shoes.")
        XCTAssertEqual(result, "There is compassion, where we feel open. We feel understanding, we feel empathetic. We can put ourselves in someone else's shoes.")
    }

    func testClippedTopLineAndTailRevision() {
        var tracker = CaptionTracker()
        _ = tracker.ingest("We are on a really good path. There is compassion where we feel open")
        let result = tracker.ingest("on a really good path. There is compassion where we feel open, and understood.")
        XCTAssertEqual(result, "We are on a really good path. There is compassion where we feel open, and understood.")
    }

    func testShortGrowingCaption() {
        var tracker = CaptionTracker()
        _ = tracker.ingest("Hello")
        XCTAssertEqual(tracker.ingest("Hello there"), "Hello there")
    }

    func testRevealingEarlierLinesDoesNotAppendTheSameSpeechTwice() {
        var tracker = CaptionTracker()
        _ = tracker.ingest("We feel empathetic. We can put ourselves in someone else's shoes.")
        let expanded = "There is compassion, where we feel open. We feel empathetic. We can put ourselves in someone else's shoes."
        XCTAssertEqual(tracker.ingest(expanded), expanded)
    }

    func testNewUtteranceAndBoundedContext() {
        var tracker = CaptionTracker()
        _ = tracker.ingest("First sentence. Second sentence. Third sentence.")
        XCTAssertEqual(tracker.ingest("A different new thought."), "Second sentence. Third sentence. A different new thought.")
        for index in 0..<100 {
            _ = tracker.ingest("Another unique sentence number \(index).")
        }
        let result = tracker.ingest("Finally we finish with a different ending.")!
        XCTAssertLessThanOrEqual(result.split(separator: " ").count, 110)
    }
}
