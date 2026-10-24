import XCTest
@testable import Jarvis

/// The follow camera: its centre is the aim while it follows, and a jump eases over the flight with the place coming onto the screen at an even pace; its springs arrive without overshooting,
/// whatever the frame length; it takes the short way round in heading, eases height in log space and says when it has settled.
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

    func testWhileItFollowsTheCentreIsTheAimSoHeStaysAtOnePlaceOnTheScreen() {
        var c = camera()
        for k in 1...30 {
            c.aim(x: 1000 + Double(k * k), y: 2000, heading: nil, pitch: 60, height: 500, response: 0.25)
            c.step(dt: 1.0 / 60)
            XCTAssertEqual(c.x, 1000 + Double(k * k), accuracy: 1e-9, "moving or still, the centre is where he is")
        }
    }

    func testAJumpKeepsTheCentreWhereItStandsAndEasesItOverTheFlight() {
        var c = camera()
        c.aim(x: 5000, y: 2000, heading: nil, pitch: 60, height: 500, response: 0.25, jump: true)
        XCTAssertEqual(c.x, 1000, accuracy: 1e-9, "nothing moves at once")
        XCTAssertTrue(c.flying)
        var last = c.x, frames = 0
        while c.flying && frames < 600 {
            c.step(dt: 1.0 / 60); frames += 1
            XCTAssertGreaterThanOrEqual(c.x, last - 1e-9, "on its way, never back"); XCTAssertLessThanOrEqual(c.x, 5000 + 1e-6, "never past")
            last = c.x
        }
        XCTAssertGreaterThan(Double(frames) / 60, 0.6, "a flight of more than 0.6 s, not a snap")
        XCTAssertLessThan(Double(frames) / 60, 2.5)
        XCTAssertEqual(c.x, 5000, accuracy: 1)
    }

    func testGoingDownToAPlaceItComesOntoTheScreenAtAnEvenPaceAndGoingBackUpItNeverOvershoots() {
        // from the day's whole, 30 km up and 8 km off, down to 1.2 km over him: how far he is from the centre, in screens (metres over the height), only ever shrinks
        var c = FootprintCamera(x: 0, y: 0, heading: 0, pitch: 0, height: 30_000)
        c.aim(x: 8000, y: 0, heading: 90, pitch: 60, height: 1200, response: 0.25, jump: true)
        var screens = 8000 / 30_000.0
        for _ in 0..<120 {
            c.step(dt: 1.0 / 60)
            let now = abs(8000 - c.x) / c.height
            XCTAssertLessThanOrEqual(now, screens + 1e-9, "he never runs off the screen while the camera drops")
            screens = now
        }
        // and back up to the whole: the centre goes straight to it, never past
        c.aim(x: 0, y: 0, heading: 0, pitch: 0, height: 30_000, response: 0.25, jump: true)
        var last = c.x
        for _ in 0..<180 {
            c.step(dt: 1.0 / 60)
            XCTAssertLessThanOrEqual(c.x, last + 1e-9); XCTAssertGreaterThanOrEqual(c.x, -1e-6)
            last = c.x
        }
        XCTAssertTrue(c.settled)
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
        XCTAssertEqual(c.x, 0); XCTAssertEqual(c.height, 500, accuracy: 1e-9); XCTAssertEqual(c.headingDegrees, 0)
    }

    func testAnotherResponseIsAFasterOrSlowerSpring() {
        var quick = camera(), slow = camera()
        quick.aim(x: 1000, y: 2000, heading: 90, pitch: 60, height: 500, response: 0.1)
        slow.aim(x: 1000, y: 2000, heading: 90, pitch: 60, height: 500, response: 0.5)
        for _ in 0..<10 { quick.step(dt: 1.0 / 60); slow.step(dt: 1.0 / 60) }
        XCTAssertGreaterThan(quick.headingDegrees, slow.headingDegrees)
    }

    func testTheTiltComesInOnlyAsTheCameraNearsTheGround() {
        XCTAssertEqual(FootprintMapFollow.pitch(at: 1200), 60)
        XCTAssertEqual(FootprintMapFollow.pitch(at: 4000), 60, accuracy: 1e-9)
        XCTAssertEqual(FootprintMapFollow.pitch(at: 15_000), 0, accuracy: 1e-9)
        XCTAssertEqual(FootprintMapFollow.pitch(at: 33_000), 0)
        XCTAssertLessThan(FootprintMapFollow.pitch(at: 9000), FootprintMapFollow.pitch(at: 6000))
    }
}
