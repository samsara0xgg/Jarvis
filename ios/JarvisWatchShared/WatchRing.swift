import CoreGraphics
import Foundation

/// The day as a ring, as pure numbers: 24 hours to the turn, 0 at the top, clockwise. No views here (the home screen and the complications draw from it;
/// the phone app compiles it with the rest of the shared folder, so JarvisTests reaches it through `@testable import Jarvis`).
enum WatchRing {
    static let dayMinutes = 1440.0

    /// the part of the turn (0...1) a minute of the day is at
    static func turn(_ minute: Double) -> Double { min(max(minute, 0), dayMinutes) / dayMinutes }

    /// radians clockwise from the top
    static func angle(_ minute: Double) -> Double { turn(minute) * 2 * .pi }

    /// where a minute is on a circle (y grows downward, as on the screen)
    static func point(minute: Double, centre: CGPoint, radius: Double) -> CGPoint {
        let a = angle(minute)
        return CGPoint(x: centre.x + radius * sin(a), y: centre.y - radius * cos(a))
    }

    /// the minute of the day under a point, in 0..<1440 (the top is 0, to the right 360)
    static func minute(at p: CGPoint, centre: CGPoint) -> Double {
        var a = atan2(Double(p.x - centre.x), Double(centre.y - p.y))
        if a < 0 { a += 2 * .pi }
        return min(a / (2 * .pi) * dayMinutes, dayMinutes - 0.001)
    }

    /// one block's stretch of the ring: `index` into the day's blocks, the angles in radians clockwise from the top
    struct Arc: Equatable {
        var index: Int
        var start: Double
        var end: Double
    }

    /// the arcs of the blocks, those with no length left out
    static func arcs(_ blocks: [WatchDay.Block]) -> [Arc] {
        blocks.enumerated().compactMap { i, b in
            let a = angle(b.t0), z = angle(b.t1)
            return z > a ? Arc(index: i, start: a, end: z) : nil
        }
    }

    /// The block a touch is on: the touch must be within `band` points of the ring's radius, and the block is the one that holds its minute, or the nearest
    /// one within `slop` minutes (a short block is only a few points wide). None when the touch is off the ring or on its empty stretch.
    static func hit(_ p: CGPoint, centre: CGPoint, radius: Double, band: Double, blocks: [WatchDay.Block], slop: Double = 12) -> Int? {
        let d = hypot(Double(p.x - centre.x), Double(p.y - centre.y))
        guard abs(d - radius) <= band else { return nil }
        let m = minute(at: p, centre: centre)
        var best: (index: Int, gap: Double)?
        for (i, b) in blocks.enumerated() where b.t1 > b.t0 {
            let gap = m < b.t0 ? b.t0 - m : m >= b.t1 ? m - b.t1 : 0
            if gap <= slop, best == nil || gap < best!.gap { best = (i, gap) }
        }
        return best?.index
    }

    /// the minute of the day at `date` for a day the phone made at `day.made` (it stops at midnight: after that the day is old)
    static func nowMinute(_ day: WatchDay, at date: Date) -> Double {
        min(dayMinutes, day.now + max(0, date.timeIntervalSince(day.made)) / 60)
    }
}
