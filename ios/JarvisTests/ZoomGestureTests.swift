import XCTest
@testable import Jarvis

/// Two fingers on the dial: moving together up and down steps the zoom level, a pinch still zooms continuously, and only the pinch turns time.
@MainActor
final class ZoomGestureTests: XCTestCase {
    private func twoFingers(_ e: StarEngine, at y: Double) {
        e.pointerDown(1, CGPoint(x: 180, y: y), 0)
        e.pointerDown(2, CGPoint(x: 260, y: y), 0)
    }

    private func move(_ e: StarEngine, y1: Double, y2: Double, x1: Double = 180, x2: Double = 260) {
        e.pointerMove(1, [(CGPoint(x: x1, y: y1), 0)])
        e.pointerMove(2, [(CGPoint(x: x2, y: y2), 0)])
    }

    func testSlideUpGoesOutOneLevelAndSettles() {
        let e = StarEngine()
        twoFingers(e, at: 500)
        for y in stride(from: 490.0, through: 370.0, by: -10) { move(e, y1: y, y2: y) }   // 130 points up
        XCTAssertEqual(e.qs.value, 130.0 / 120, accuracy: 0.05)
        let f = e.fs.value
        e.pointerUp(1, 1, cancelled: false)
        e.pointerUp(2, 1, cancelled: false)
        XCTAssertEqual(e.qGoal, 1, "settles on the week")
        XCTAssertEqual(f, e.fs.value, accuracy: 0.001, "a slide does not turn time")
    }

    func testSlideDownGoesInOneLevel() {
        let e = StarEngine()
        twoFingers(e, at: 500)
        for y in stride(from: 510.0, through: 640.0, by: 10) { move(e, y1: y, y2: y) }   // 140 points down
        XCTAssertLessThan(e.qs.value, -0.9)
        e.pointerUp(1, 1, cancelled: false)
        e.pointerUp(2, 1, cancelled: false)
        XCTAssertEqual(e.qGoal, -1, "settles on the close-up")
    }

    func testPinchStaysAPinch() {
        let e = StarEngine()
        twoFingers(e, at: 500)
        for i in 1...12 { move(e, y1: 500, y2: 500, x1: 180 - Double(i) * 6, x2: 260 + Double(i) * 6) }   // spread from 80 to 224 points
        XCTAssertLessThan(e.qs.value, -0.5, "spreading zooms in, by the pinch rule")
    }
}
