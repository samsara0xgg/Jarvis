import XCTest
@testable import Jarvis

/// The follow camera's spring: it arrives without overshooting, whatever the frame length, takes the short way round in heading, eases height in log space and says when it has settled.
final class FootprintCameraTests: XCTestCase {
    private func camera(heading: Double = 0, height: Double = 500) -> FootprintCamera {
        FootprintCamera(x: 1000, y: 2000, heading: heading, pitch: 60, height: height)
    }

    func testASpringConvergesWithoutOvershootingAndItsFrameLengthDoesNotMatter() {
        var fine = FootprintCamera.Spring(100), coarse = fine
        fine.target = 0; coarse.target = 0
        let w = 2 * Double.pi / 0.25
        for _ in 0..<30 {
            fine.step(dt: 1.0 / 60, omega: w)
            XCTAssertGreaterThanOrEqual(fine.value, 0, "never past the aim")
        }
        coarse.step(dt: 0.5, omega: w)
        XCTAssertEqual(fine.value, coarse.value, accuracy: 1e-9, "30 frames at 60 Hz are one step of half a second")
        XCTAssertLessThan(fine.value, 0.01, "settled within half a second")
        XCTAssertGreaterThan(fine.value, 0)
    }

    func testAMovingAimKeepsTheSpeedTheCameraHas() {
        var c = camera()
        c.aim(x: 1100, y: 2000, heading: nil, pitch: 60, height: 500, response: 0.25)
        for _ in 0..<6 { c.step(dt: 1.0 / 60) }
        let (v, speed) = (c.x.value, c.x.velocity)
        c.aim(x: 1200, y: 2000, heading: nil, pitch: 60, height: 500, response: 0.25)
        XCTAssertEqual(c.x.value, v)
        XCTAssertEqual(c.x.velocity, speed, "a new aim changes where it goes, not how it moves now")
    }

    func testHeadingTakesTheShortWayRoundAcrossNorthBothWays() {
        XCTAssertEqual(FootprintCamera.turn(from: 350, to: 10), 20)
        XCTAssertEqual(FootprintCamera.turn(from: 10, to: 350), -20)
        XCTAssertEqual(FootprintCamera.turn(from: 0, to: 90), 90)
        XCTAssertEqual(FootprintCamera.turn(from: 90, to: 270).magnitude, 180)
        for (from, to) in [(350.0, 10.0), (10.0, 350.0)] {
            var c = camera(heading: from)
            c.aim(x: 1000, y: 2000, heading: to, pitch: 60, height: 500, response: 0.25)
            var seen = [Double]()
            for _ in 0..<60 { c.step(dt: 1.0 / 60); seen.append(c.headingDegrees) }
            XCTAssertTrue(seen.allSatisfy { $0 >= 350 || $0 <= 10 }, "it stays within the 20 degrees across north: \(seen)")
            XCTAssertEqual(c.headingDegrees, to, accuracy: 0.05)
        }
    }

    func testANilHeadingKeepsTheOneItHas() {
        var c = camera(heading: 123)
        c.aim(x: 5000, y: 2000, heading: nil, pitch: 60, height: 500, response: 0.25)
        for _ in 0..<60 { c.step(dt: 1.0 / 60) }
        XCTAssertEqual(c.headingDegrees, 123, accuracy: 1e-9)
    }

    func testHeightEasesInLogSpaceSoOutAndBackAreMirrorImages() {
        var up = camera(height: 500), down = camera(height: 2000)
        up.aim(x: 1000, y: 2000, heading: nil, pitch: 60, height: 2000, response: 0.25)
        down.aim(x: 1000, y: 2000, heading: nil, pitch: 60, height: 500, response: 0.25)
        for _ in 0..<12 {
            up.step(dt: 1.0 / 60); down.step(dt: 1.0 / 60)
            XCTAssertEqual(up.height * down.height, 500 * 2000, accuracy: 1e-6, "the ratio changes evenly, not the metres")
        }
        XCTAssertGreaterThan(up.height, 500); XCTAssertLessThan(up.height, 2000)
    }

    func testItIsSettledOnlyOnceEveryPartHasArrivedAndACutArrivesAtOnce() {
        var c = camera()
        XCTAssertTrue(c.settled, "a camera with no new aim is at rest")
        c.aim(x: 1500, y: 2000, heading: 90, pitch: 0, height: 1200, response: 0.25)
        XCTAssertFalse(c.settled)
        var frames = 0
        while !c.settled && frames < 600 { c.step(dt: 1.0 / 60); frames += 1 }
        XCTAssertLessThan(frames, 90, "within 1.5 s")
        XCTAssertGreaterThan(frames, 15, "not at once")
        c.aim(x: 0, y: 0, heading: 0, pitch: 60, height: 500, response: 0.25)
        c.cut()
        XCTAssertTrue(c.settled)
        XCTAssertEqual(c.x.value, 0); XCTAssertEqual(c.height, 500, accuracy: 1e-9); XCTAssertEqual(c.headingDegrees, 0)
    }

    func testAnotherResponseIsAFasterOrSlowerSpring() {
        var quick = camera(), slow = camera()
        quick.aim(x: 2000, y: 2000, heading: nil, pitch: 60, height: 500, response: 0.1)
        slow.aim(x: 2000, y: 2000, heading: nil, pitch: 60, height: 500, response: 0.5)
        for _ in 0..<10 { quick.step(dt: 1.0 / 60); slow.step(dt: 1.0 / 60) }
        XCTAssertGreaterThan(quick.x.value, slow.x.value)
    }
}
