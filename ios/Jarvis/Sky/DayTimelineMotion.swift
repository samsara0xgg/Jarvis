import SwiftUI

// The motion of the timeline, in one place (docs: the day timeline's design, section 9), so that what moves does so by one rule: motion that says where a thing comes from, in under a
// quarter of a second, and none that has nothing to say. Under Reduce Motion each one is a plain fade of 120 ms: no slides, no growth, no shimmer.

enum TLMotion {
    /// the bar's stretches grow in from the left, in the order of time, when the timeline opens
    static let barGrow = 0.45
    /// the first rows rise into place from a little below, one after another
    static let riseCount = 10, riseOffset = 6.0, riseDuration = 0.22, riseStagger = 0.025
    /// another day: its list comes in the way time goes, a hand's breadth, fading; its bar a hair
    static let daySlide = 16.0, barSlide = 8.0, dayDuration = 0.22
    /// what is new grows from no height at its place, and the rows under it move with it
    static let grow = 0.24
    /// what every motion becomes under Reduce Motion
    static let reducedFade = 0.12

    static var reduced: Bool { UIAccessibility.isReduceMotionEnabled }

    /// the bar growing in, or under Reduce Motion fading in
    static func barGrowth(reduce: Bool) -> Animation { .easeOut(duration: reduce ? reducedFade : barGrow) }
    /// row number `rank` of the first rows rising, each a little after the one before
    static func rise(rank: Int, reduce: Bool) -> Animation {
        reduce ? .easeOut(duration: reducedFade) : .easeOut(duration: riseDuration).delay(Double(rank) * riseStagger)
    }
    /// what stands beside the bar's stretches (the hours, the sun, the stars, the line for now) comes in as they grow: a fade of 200 ms that begins once the growth has gone `after` of its way
    static func arrival(after: Double, reveal: Bool, reduce: Bool) -> Animation {
        let fade = Animation.easeOut(duration: reduce ? reducedFade : 0.2)
        return reveal && !reduce ? fade.delay(after * barGrow) : fade
    }
    /// a change of day
    static func dayChange(reduce: Bool) -> Animation { .easeOut(duration: reduce ? reducedFade : dayDuration) }
    /// what is new in the list, and the rows that make room for it
    static func listChange(reduce: Bool) -> Animation { .easeOut(duration: reduce ? reducedFade : grow) }
}

// MARK: - what is new grows

/// What is new in the list grows from no height at its place while it fades in (240 ms), and the rows under it move down as its height comes.
enum TLGrow {
    static var transition: AnyTransition { .modifier(active: Grown(k: 0), identity: Grown(k: 1)) }

    /// the modifier that is animated: how much of its height it has and how much it shows
    struct Grown: ViewModifier, Animatable {
        var k: Double
        var animatableData: Double {
            get { k }
            set { k = newValue }
        }
        func body(content: Content) -> some View {
            TLGrownLayout(k: k) { content.opacity(k) }.clipped()
        }
    }
}

/// a layout that is `k` of the height of the one view in it and all of its width, with the view put at its top
struct TLGrownLayout: Layout {
    var k: Double

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        guard let v = subviews.first else { return .zero }
        let s = v.sizeThatFits(proposal)
        return CGSize(width: s.width, height: s.height * max(0, min(1, k)))
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        subviews.first?.place(at: CGPoint(x: bounds.minX, y: bounds.minY), anchor: .topLeading, proposal: ProposedViewSize(width: bounds.width, height: nil))
    }
}
