import Foundation

/// What he was doing, as his thread on the dial shows it: a colour from the app's own palette and how brightly the thread burns.
/// `lum` multiplies the thread's brightness (1 is how it has always looked) and, a little, its width.
struct RingTone: Equatable {
    let col: RGB, lum: Double

    static let sleep = RingTone(col: .dial, lum: 0.55)      // dim, blue-violet
    static let idle = RingTone(col: .ink, lum: 0.6)         // a stay with nothing else going on: faint
    static let call = RingTone(col: .wait, lum: 1.1)        // amber
    static let ride = RingTone(col: .lit, lum: 1.4)         // on a bus: a solid, warm white stretch
    static let car = RingTone(col: .lit, lum: 1.2)
    static let none = RingTone(col: .lit, lum: 1)           // a gap, and the sample sky's own ring

    /// Focused work is blue and burns brighter the more of its span he was active in (`active` seconds of `span` seconds).
    static func work(active: Double?, span: Double) -> RingTone {
        let f = active.map { span > 0 ? clamp($0 / span, 0, 1) : 0.6 } ?? 0.6
        return RingTone(col: .work, lum: 0.9 + 0.6 * f)
    }

    /// Moving on his own feet is green; a car is warm white, a little less than a bus ride.
    static func move(_ activity: String?) -> RingTone {
        switch activity {
        case "running": return RingTone(col: .done, lum: 1.1)
        case "automotive": return .car
        default: return RingTone(col: .done, lum: 1)
        }
    }

    /// The tone an item of the brain's day gives its span, and which one wins where spans overlap (higher). nil: the item says nothing about what he was doing.
    static func of(_ kind: String, activity: String? = nil, active: Double? = nil, span: Double = 0) -> (tone: RingTone, prio: Int)? {
        switch kind {
        case "ride": return (.ride, 5)
        case "move": return (move(activity), 4)
        case "call": return (.call, 3)
        case "sleep": return (.sleep, 3)
        case "work": return (work(active: active, span: span), 2)
        case "stay": return (.idle, 1)
        default: return nil
        }
    }

    /// the thread's width factor: a brighter thread is a little thicker, a faint one a little thinner
    var width: Double { clamp(1 + 0.5 * (lum - 1), 0.85, 1.25) }
}
