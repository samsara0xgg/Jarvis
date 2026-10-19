import XCTest
import SwiftUI
@testable import Jarvis

/// The timeline's motion is one table (Sky/DayTimelineMotion.swift): what the design asks of it, and what each of them becomes under Reduce Motion.
final class TLMotionTests: XCTestCase {
    func testTheTableIsTheDesigns() {
        XCTAssertEqual(TLMotion.barGrow, 0.45, "the bar grows in over 450 ms")
        XCTAssertEqual(TLMotion.riseCount, 10, "the first ten rows rise")
        XCTAssertEqual([TLMotion.riseOffset, TLMotion.riseDuration, TLMotion.riseStagger], [6, 0.22, 0.025], "6 pt, over 220 ms, 25 ms apart")
        XCTAssertEqual([TLMotion.daySlide, TLMotion.barSlide, TLMotion.dayDuration], [16, 8, 0.22], "a day's list 16 pt, its bar 8 pt, over 220 ms")
        XCTAssertEqual(TLMotion.grow, 0.24, "what is new grows over 240 ms")
        XCTAssertEqual(TLMotion.reducedFade, 0.12)
    }

    func testEachMotionIsAFadeOf120msUnderReduceMotionAndNothingMoves() {
        let fade = Animation.easeOut(duration: 0.12)
        XCTAssertEqual(TLMotion.barGrowth(reduce: true), fade)
        XCTAssertEqual(TLMotion.rise(rank: 7, reduce: true), fade, "and the rows do not wait for one another")
        XCTAssertEqual(TLMotion.dayChange(reduce: true), fade)
        XCTAssertEqual(TLMotion.listChange(reduce: true), fade)
    }

    func testWhatStandsBesideTheBarComesInOnceTheGrowthHasGonePartOfItsWayAndNotWhenItGoes() {
        XCTAssertEqual(TLMotion.arrival(after: 0.7, reveal: true, reduce: false), Animation.easeOut(duration: 0.2).delay(0.7 * 0.45))
        XCTAssertEqual(TLMotion.arrival(after: 0.25, reveal: true, reduce: false), Animation.easeOut(duration: 0.2).delay(0.25 * 0.45))
        XCTAssertEqual(TLMotion.arrival(after: 0.7, reveal: false, reduce: false), Animation.easeOut(duration: 0.2), "put back, it does not wait")
        XCTAssertEqual(TLMotion.arrival(after: 0.7, reveal: true, reduce: true), Animation.easeOut(duration: 0.12), "and under Reduce Motion it is a fade of 120 ms with no wait")
    }

    func testWhatIsNewGrowsFromNoHeightToAllOfItAtItsWidth() {
        func size(_ k: Double) -> CGSize {
            UIHostingController(rootView: TLGrownLayout(k: k) { Color.red.frame(width: 120, height: 80) }).sizeThatFits(in: CGSize(width: 300, height: 600))
        }
        XCTAssertEqual(size(0), CGSize(width: 120, height: 0), "no height at first")
        XCTAssertEqual(size(0.25).height, 20, accuracy: 0.5)
        XCTAssertEqual(size(1), CGSize(width: 120, height: 80), "and all of it at the end")
        XCTAssertEqual(size(1.5).height, 80, "never more")
        XCTAssertEqual(size(-1).height, 0, "nor less")
    }

    func testTheLoadingTrackHasASoftBandGoingAlongItThatRestsBetweenPasses() {
        let w = 380.0, band = 96.0
        func x(_ t: Double) -> Double { TLShimmer.x(at: t, width: w, band: band) }
        XCTAssertEqual(x(0), -band, "it starts off the track's left end")
        XCTAssertEqual(x(TLShimmer.sweep), w, accuracy: 0.001, "and ends off its right end")
        XCTAssertEqual(x(TLShimmer.sweep + 0.2), w, "then the track rests")
        XCTAssertEqual(x(TLShimmer.period), -band, accuracy: 0.001, "and it goes again")
        let along = stride(from: 0.0, through: TLShimmer.sweep, by: 0.05).map(x)
        XCTAssertEqual(along, along.sorted(), "it only goes one way")
        XCTAssertLessThan(x(0.1) + band, 0.1 * (w + band), "slowly at first")
        XCTAssertEqual(x(TLShimmer.sweep / 2), (w - band) / 2, accuracy: 0.001, "and half way at half the time")
    }

    func testTheMotionsAreTheDesignsOtherwise() {
        XCTAssertEqual(TLMotion.barGrowth(reduce: false), .easeOut(duration: 0.45))
        XCTAssertEqual(TLMotion.rise(rank: 4, reduce: false), .easeOut(duration: 0.22).delay(0.1), "the fifth row waits for four")
        XCTAssertEqual(TLMotion.dayChange(reduce: false), .easeOut(duration: 0.22))
        XCTAssertEqual(TLMotion.listChange(reduce: false), .easeOut(duration: 0.24))
    }
}
