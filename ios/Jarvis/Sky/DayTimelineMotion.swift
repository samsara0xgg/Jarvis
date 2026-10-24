import SwiftUI

// The motion of the timeline, in one place (docs: the day timeline's design, section 9), so that what moves does so by one rule: motion that says where a thing comes from, in under a
// quarter of a second, and none that has nothing to say. Under Reduce Motion each one is a plain fade of 120 ms: no slides, no growth, no shimmer. What moves is a spring that does not bounce
// (`.smooth`), which takes up a motion that is in the middle of itself where it is; what only fades is an ease.

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
    static func barGrowth(reduce: Bool) -> Animation { reduce ? .easeOut(duration: reducedFade) : .smooth(duration: barGrow) }
    /// row number `rank` of the first rows rising, each a little after the one before
    static func rise(rank: Int, reduce: Bool) -> Animation {
        reduce ? .easeOut(duration: reducedFade) : .smooth(duration: riseDuration).delay(Double(rank) * riseStagger)
    }
    /// words that are replaced by other words in the same place (her sentence, the tag after the date, the weather): the old ones leave over 70 ms and the new ones arrive over 140 ms
    /// after them, so that the two are never readable together; under Reduce Motion the same in 120 ms (a fade moves nothing)
    static func swapWords(reduce: Bool) -> AnyTransition {
        let out = reduce ? 0.05 : 0.07, into = reduce ? 0.07 : 0.14
        return .asymmetric(insertion: .opacity.animation(.easeOut(duration: into).delay(out)), removal: .opacity.animation(.easeIn(duration: out)))
    }

    /// what stands beside the bar's stretches (the hours, the line for now) comes in as they grow: a fade of 200 ms that begins once the growth has gone `after` of its way
    static func arrival(after: Double, reveal: Bool, reduce: Bool) -> Animation {
        let fade = Animation.easeOut(duration: reduce ? reducedFade : 0.2)
        return reveal && !reduce ? fade.delay(after * barGrow) : fade
    }
    /// a change of day
    static func dayChange(reduce: Bool) -> Animation { reduce ? .easeOut(duration: reducedFade) : .smooth(duration: dayDuration) }
    /// the page that another day's page takes the place of goes in half that time, so that two days are never on show at the strength of one
    static func dayLeave(reduce: Bool) -> Animation { reduce ? .easeOut(duration: reducedFade / 2) : .smooth(duration: dayDuration / 2) }
    /// another day's list, or its bar: it comes in the way time goes, `slide` pt, fading in as `dayChange`, and the old one leaves the other way as `dayLeave`; under Reduce Motion both only fade
    static func page(slide: Double, reduce: Bool) -> AnyTransition {
        let come = reduce ? AnyTransition.opacity : .opacity.combined(with: .offset(x: -slide)), go = reduce ? AnyTransition.opacity : .opacity.combined(with: .offset(x: slide))
        return .asymmetric(insertion: come.animation(dayChange(reduce: reduce)), removal: go.animation(dayLeave(reduce: reduce)))
    }
    /// what is new in the list, and the rows that make room for it
    static func listChange(reduce: Bool) -> Animation { reduce ? .easeOut(duration: reducedFade) : .smooth(duration: grow) }
    /// the list going down with the top as her words come up under it (and back as they go): 0.3 s, ease-out, no bounce
    static func captionLift(reduce: Bool) -> Animation { reduce ? .easeOut(duration: reducedFade) : .easeOut(duration: 0.3) }
    /// the list sent to a place (the way back to now, a row the waiting cards name): longer for a longer way, so that a short one is not slow and a long one is not a blur
    static func scrollTo(distance: Double) -> Animation { .smooth(duration: min(0.8, 0.3 + distance / 4000)) }
    /// A page over the screen (the footprint's day, its outing) comes in from the right and goes back the same way: a spring of 0.3 s that does not bounce, so that a tap or a swipe in the middle
    /// of it takes it over where it is; under Reduce Motion a cross-fade.
    static func detail(reduce: Bool) -> Animation { reduce ? .easeOut(duration: reducedFade) : .spring(response: 0.3, dampingFraction: 1) }
    /// the list carried from row to row by a finger on the bar: a short spring that lets the move be seen and takes up the next one where it is
    static let follow = Animation.interactiveSpring(response: 0.2, dampingFraction: 0.9)
    /// a swipe let go: the same spring, starting at the finger's speed (`speed`: parts of the way left per second), so what it moved goes on as it was moving
    static func release(speed: Double) -> Animation { .interpolatingSpring(duration: 0.3, bounce: 0, initialVelocity: speed) }
    /// something small that comes or goes where it stands (a word on the bar, a badge, a mark): 200 ms, or under Reduce Motion a fade of 120 ms
    static func fade(reduce: Bool) -> Animation { .easeOut(duration: reduce ? reducedFade : 0.2) }

    /// A row opening where it stands: what it opens to unfolds under it and the list carries the row to the top of the list together, a spring of 0.32 s that does not bounce; closing is
    /// quicker, 0.28 s. Under Reduce Motion both are a fade of 120 ms.
    static func unfold(open: Bool, reduce: Bool) -> Animation { reduce ? .easeOut(duration: reducedFade) : .spring(response: open ? 0.32 : 0.28, dampingFraction: 1) }
    /// what a row opens to comes in a line after another as it unfolds, from 6 pt below: the first 40 ms in, each 25 ms after the one before (the eighth and on together), 220 ms each
    static let lineRise = 6.0
    static func line(rank: Int, reduce: Bool) -> Animation {
        reduce ? .easeOut(duration: reducedFade) : .smooth(duration: 0.22).delay(0.04 + 0.025 * Double(min(rank, 7)))
    }
}

// MARK: - what is new grows

/// What is new in the list grows from no height at its place while it fades in (240 ms), and the rows under it move down as its height comes; what goes (an event he says is not his) does it
/// the other way, and the rows under it come up as its height goes.
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

/// What a row opens to, unfolding where it stands: its height comes and what is in it rises in by itself (`TLMotion.line`); closing, its height and its words go together. It is clipped to
/// its height and not to its width, so a line lit across the row's whole width keeps its rounded ends.
enum TLUnfold {
    static var transition: AnyTransition {
        .asymmetric(insertion: .modifier(active: Folded(k: 0, fades: false), identity: Folded(k: 1, fades: false)),
                    removal: .modifier(active: Folded(k: 0, fades: true), identity: Folded(k: 1, fades: true)))
    }

    struct Folded: ViewModifier, Animatable {
        var k: Double
        let fades: Bool
        var animatableData: Double {
            get { k }
            set { k = newValue }
        }
        func body(content: Content) -> some View {
            TLGrownLayout(k: k) { content.opacity(fades ? k : 1) }.clipShape(Band())
        }
    }

    /// its own height, and 12 pt beyond either side
    private struct Band: Shape {
        func path(in r: CGRect) -> Path { Path(r.insetBy(dx: -12, dy: 0)) }
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
