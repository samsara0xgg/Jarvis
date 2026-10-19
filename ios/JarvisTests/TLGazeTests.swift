import XCTest
@testable import Jarvis

/// Where her eyes look in the timeline (Sky/DayTimelineGaze.swift): toward his finger while it is on the bar, back to him when it lifts, and once to a row that opens.
@MainActor
final class TLGazeTests: XCTestCase {
    /// the eyes' end of the line: what she is asked to look at, and how
    private final class Eyes: HerDrive {
        var glances: [(g: (Double, Double), ms: Double)] = []
        var looks: [(Double, Double)?] = []
        func herUpdate(now: Double, dt: Double, expr: String, look: (Double, Double)?, hear: Double, pressed: Bool, say: Double?) { looks.append(look) }
        func herLookAt(_ g: (Double, Double), now: Double, ms: Double) { glances.append((g, ms)) }
        func herEffect(_ name: String, now: Double) {}
    }

    private func engine(home: Bool = true) -> (StarEngine, Eyes) {
        let e = StarEngine(), eyes = Eyes()
        e.herDrive = eyes
        e.ui.tlHome = home
        return (e, eyes)
    }

    // MARK: where it looks

    func testTheEyesTurnFromOneEndOfTheBarToTheOtherAsTheFingerGoesAlongIt() {
        let left = TLGaze.toward(TLGaze.bar(x: 0)), mid = TLGaze.toward(TLGaze.bar(x: TLLayout.barW / 2)), right = TLGaze.toward(TLGaze.bar(x: TLLayout.barW))
        XCTAssertLessThan(left.0, mid.0, "the finger goes to the right and so do her eyes")
        XCTAssertLessThan(mid.0, right.0)
        XCTAssertGreaterThan(left.1, mid.1, "and she looks less far down as it goes away from the place under her")
        XCTAssertGreaterThan(mid.1, right.1)
        XCTAssertGreaterThan(right.0 - left.0, 0.6, "a turn that can be seen at her size, not a flutter")
        XCTAssertLessThan(abs(left.0), 0.3, "a finger right under her is below her, not off to a side")
        for g in [left, mid, right] {
            XCTAssertLessThanOrEqual(abs(g.0), 0.8, "and not so far that the eyes leave her face")
            XCTAssertTrue((-0.5...0.55).contains(g.1))
        }
    }

    func testThePlaceUnderHerHasNoWayToIt() {
        let g = TLGaze.toward(TLLayout.her)
        XCTAssertEqual(g.0, 0)
        XCTAssertEqual(g.1, 0)
    }

    func testAFingerIsOnTheBarAtTheMiddleOfItsHeightAndNotPastItsEnds() {
        XCTAssertEqual(TLGaze.bar(x: 0).x, 30)
        XCTAssertEqual(TLGaze.bar(x: 190).x, 220)
        XCTAssertEqual(TLGaze.bar(x: -40).x, 30, "a finger dragged past the left end is at it")
        XCTAssertEqual(TLGaze.bar(x: 900).x, 30 + TLLayout.barW, "and past the right")
        XCTAssertEqual(TLGaze.bar(x: 100).y, TLLayout.barY + TLLayout.barH / 2)
    }

    func testARowIsWhereItsWordsBeginHalfWayDownItsLineAndMovesUpAsTheListGoes() {
        let a = TLGaze.row(top: 100, height: 44, scrolled: 0), b = TLGaze.row(top: 100, height: 44, scrolled: 60)
        XCTAssertEqual(a.x, 30 + TLColumns.text + 60)
        XCTAssertEqual(a.y, TLLayout.listTop + 122)
        XCTAssertEqual(a.x, b.x)
        XCTAssertEqual(a.y - b.y, 60, "a list scrolled up by 60 pt has the row 60 pt higher")
    }

    // MARK: when it looks

    func testShesAskedToLookAtTheFingerOnlyWhileItIsOnTheBarInHerCorner() throws {
        let (e, _) = engine()
        XCTAssertNil(e.timelineGaze, "no finger, nowhere in particular")
        e.ui.tl.finger = TLGaze.bar(x: 300)
        XCTAssertEqual(try XCTUnwrap(e.timelineGaze).0, TLGaze.toward(TLGaze.bar(x: 300)).0)
        e.ui.tl.follow(nil, in: nil)
        XCTAssertNil(e.timelineGaze, "it lifts")
        e.ui.tl.finger = TLGaze.bar(x: 300)
        e.ui.tlHome = false
        XCTAssertNil(e.timelineGaze, "and a finger on a bar she is not in the corner of is no business of hers")
    }

    func testWhenTheFingerLiftsHerEyesComeToHimOnceAndOnlyThen() {
        let (e, eyes) = engine()
        e.syncGaze()
        XCTAssertTrue(eyes.glances.isEmpty, "nothing has happened")
        e.ui.tl.finger = TLGaze.bar(x: 80)
        e.syncGaze(); e.syncGaze()
        XCTAssertTrue(eyes.glances.isEmpty, "while the finger is down they follow it and are not sent anywhere")
        e.ui.tl.follow(nil, in: nil)
        e.syncGaze(); e.syncGaze(); e.syncGaze()
        XCTAssertEqual(eyes.glances.count, 1, "the finger lifted: once")
        XCTAssertEqual(eyes.glances.first?.g.0, HerCore.you.x)
        XCTAssertEqual(eyes.glances.first?.g.1, HerCore.you.y)
        XCTAssertEqual(eyes.glances.first?.ms, TLGaze.back)
    }

    func testLeavingTheCornerWithAFingerDownBringsThemBackToo() {
        let (e, eyes) = engine()
        e.ui.tl.finger = TLGaze.bar(x: 200)
        e.syncGaze()
        e.ui.tlHome = false
        e.syncGaze()
        XCTAssertEqual(eyes.glances.count, 1)
    }

    func testARowThatOpensIsGlancedAtOnceAndNotWhenSheIsNotInHerCorner() {
        let (e, eyes) = engine()
        e.glance(at: TLGaze.row(top: 300, height: 44, scrolled: 0))
        XCTAssertEqual(eyes.glances.count, 1)
        XCTAssertEqual(eyes.glances.first?.ms, TLGaze.glance)
        let want = TLGaze.toward(TLGaze.row(top: 300, height: 44, scrolled: 0))
        XCTAssertEqual(eyes.glances.first?.g.0, want.0)
        XCTAssertEqual(eyes.glances.first?.g.1, want.1)
        e.ui.tlHome = false
        e.glance(at: TLGaze.row(top: 300, height: 44, scrolled: 0))
        XCTAssertEqual(eyes.glances.count, 1)
    }
}
