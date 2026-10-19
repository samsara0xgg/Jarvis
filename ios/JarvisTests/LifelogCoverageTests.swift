import XCTest
@testable import Jarvis

/// ADR 0292: the phone drops its audio only when the brain says a terminal records AND a fresh, clear position is inside home; every doubt keeps it.
final class LifelogPolicyTests: XCTestCase {
    private let now = Date(timeIntervalSince1970: 1_800_000_000)
    // home near Victoria, radius 120 m; 0.001 degrees of latitude is about 111 m
    private let home = LifelogCoverage.Home(lat: 48.4284, lng: -123.3656, radiusM: 120)

    private func coverage(mac: Bool = true, home: LifelogCoverage.Home? = nil) -> LifelogCoverage {
        .init(nowMs: 1_800_000_000_000, macRecording: mac, terminal: "mac", newestPieceEndMs: 1_799_999_700_000, home: home ?? self.home)
    }
    private func fix(dLat: Double = 0, age: TimeInterval = 30, accuracy: Double = 65) -> LifelogFix {
        .init(lat: home.lat + dLat, lng: home.lng, accuracyM: accuracy, at: now - age)
    }

    func testHomeWithTheMacRecordingDropsTheAudio() {
        let d = LifelogPolicy.decide(coverage(), fix: fix(), now: now)
        XCTAssertFalse(d.keep)
        XCTAssertEqual(d.code, "home-mac-recording")
        XCTAssertEqual(d.line, "在家，Mac 在录，手机暂不留音频")
    }

    func testEveryDoubtKeepsTheAudioAndSaysWhy() {
        let cases: [(LifelogDecision, String)] = [
            (LifelogPolicy.decide(nil, fix: fix(), now: now), "no-answer"),
            (LifelogPolicy.decide(coverage(mac: false), fix: fix(), now: now), "mac-not-recording"),
            (LifelogPolicy.decide(.init(nowMs: 1, macRecording: true, terminal: "mac", newestPieceEndMs: 1, home: nil), fix: fix(), now: now), "no-home"),
            (LifelogPolicy.decide(coverage(), fix: nil, now: now), "no-fix"),
            (LifelogPolicy.decide(coverage(), fix: fix(age: 15 * 60 + 1), now: now), "stale-fix"),
            (LifelogPolicy.decide(coverage(), fix: fix(accuracy: 151), now: now), "vague-fix"),
            (LifelogPolicy.decide(coverage(), fix: fix(accuracy: -1), now: now), "vague-fix"),
            (LifelogPolicy.decide(coverage(), fix: fix(dLat: 0.002), now: now), "away"),
        ]
        for (d, code) in cases {
            XCTAssertTrue(d.keep, code)
            XCTAssertEqual(d.code, code)
            XCTAssertTrue(d.line.hasSuffix("手机在录"), d.line)
        }
    }

    func testTheEdgesOfFreshnessAccuracyAndTheCircle() {
        XCTAssertFalse(LifelogPolicy.decide(coverage(), fix: fix(age: 15 * 60), now: now).keep, "exactly 15 minutes is still fresh")
        XCTAssertFalse(LifelogPolicy.decide(coverage(), fix: fix(accuracy: 150), now: now).keep)
        XCTAssertFalse(LifelogPolicy.decide(coverage(), fix: fix(dLat: 0.001), now: now).keep, "111 m is inside a 120 m circle")
        XCTAssertTrue(LifelogPolicy.decide(coverage(), fix: fix(dLat: 0.0012), now: now).keep, "133 m is outside it")
        let tight = LifelogCoverage.Home(lat: home.lat, lng: home.lng, radiusM: 50)
        XCTAssertTrue(LifelogPolicy.decide(coverage(home: tight), fix: fix(dLat: 0.001), now: now).keep, "the circle is the brain's, not a constant")
    }

    func testTheBrainsAnswerDecodes() throws {
        let json = #"{"now_ms":1800000000000,"mac_recording":true,"terminal":"mac","newest_piece_end_ms":1799999700000,"home":{"lat":48.4284,"lng":-123.3656,"radius_m":120}}"#
        let c = try XCTUnwrap(LifelogCoverage.decode(Data(json.utf8)))
        XCTAssertEqual(c, coverage())
        let bare = try XCTUnwrap(LifelogCoverage.decode(Data(#"{"now_ms":1,"mac_recording":false,"terminal":null,"newest_piece_end_ms":null,"home":null}"#.utf8)))
        XCTAssertFalse(bare.macRecording)
        XCTAssertNil(bare.home)
        XCTAssertNil(LifelogCoverage.decode(Data("nope".utf8)))
    }
}

@MainActor
final class LifelogGateTests: XCTestCase {
    private let home = LifelogCoverage.Home(lat: 48.4284, lng: -123.3656, radiusM: 120)
    private func cover(_ mac: Bool) -> LifelogCoverage {
        .init(nowMs: 0, macRecording: mac, terminal: "mac", newestPieceEndMs: 0, home: home)
    }
    private var here: LifelogFix { .init(lat: home.lat, lng: home.lng, accuracyM: 40, at: Date()) }

    func testTheWriterStopsKeepingWhenTheDecisionIsToDropAndKeepsAgainWhenItChanges() async {
        let writer = LifelogWriter()
        let gate = LifelogGate(writer: writer)
        var answer: LifelogCoverage? = cover(true)
        var asked = 0
        gate.fetch = { answer }
        gate.locate = { asked += 1; return self.here }
        gate.run(true)
        await gate.ask()
        XCTAssertTrue(writer.dropping)
        XCTAssertFalse(gate.keeping)
        XCTAssertEqual(gate.line, "在家，Mac 在录，手机暂不留音频")

        answer = cover(false)                                   // the Mac stopped
        let before = asked
        await gate.ask()
        XCTAssertFalse(writer.dropping)
        XCTAssertTrue(gate.keeping)
        XCTAssertEqual(asked, before, "no position is asked for when the answer cannot be to drop")

        answer = cover(true)
        await gate.ask()
        XCTAssertTrue(writer.dropping)
        answer = nil                                            // the brain cannot be reached
        await gate.ask()
        XCTAssertFalse(writer.dropping)
        XCTAssertEqual(gate.line, "问不到大脑，手机在录")

        answer = cover(true)
        await gate.ask()
        XCTAssertTrue(writer.dropping)
        gate.run(false)                                         // the switch goes off: everything is kept again
        XCTAssertFalse(writer.dropping)
        XCTAssertEqual(gate.line, "")
    }

    func testADecisionNotRenewedLapses() {
        let writer = LifelogWriter()
        writer.setDropping(until: LifelogClock.nowMs() + 60_000)
        XCTAssertTrue(writer.dropping)
        writer.setDropping(until: LifelogClock.nowMs() - 1)
        XCTAssertFalse(writer.dropping, "past its time the writer keeps again by itself")
        writer.setDropping(until: LifelogClock.nowMs() + 60_000)
        writer.setDropping(until: nil)
        XCTAssertFalse(writer.dropping)
    }

    func testAnAnswerThatArrivesAfterTheSwitchWentOffChangesNothing() async {
        let writer = LifelogWriter()
        let gate = LifelogGate(writer: writer)
        gate.fetch = { self.cover(true) }
        gate.locate = { self.here }
        await gate.ask()                                        // the switch was never on
        XCTAssertFalse(writer.dropping)
        XCTAssertEqual(gate.line, "")
    }
}
