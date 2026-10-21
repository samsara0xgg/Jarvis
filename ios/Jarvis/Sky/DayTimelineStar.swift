// words: legacy
import SwiftUI

// The star of what mattered, where the timeline draws it: his mark is a filled star in warm white, her pick the same star outlined. Here are the shape, the star in the corner of a row's
// icon and on the bar, and the button in the open row that turns his mark on and off.

private let ink = SetColor.ink

/// the four-pointed star the dial draws for what mattered, a little fuller (the curves bow out) so that at 7 pt it still reads as a star
struct Star4: Shape {
    func path(in rect: CGRect) -> Path {
        let c = CGPoint(x: rect.midX, y: rect.midY), r = min(rect.width, rect.height) / 2, k = 0.3 * r
        var p = Path()
        p.move(to: CGPoint(x: c.x, y: c.y - r))
        p.addQuadCurve(to: CGPoint(x: c.x + r, y: c.y), control: CGPoint(x: c.x + k, y: c.y - k))
        p.addQuadCurve(to: CGPoint(x: c.x, y: c.y + r), control: CGPoint(x: c.x + k, y: c.y + k))
        p.addQuadCurve(to: CGPoint(x: c.x - r, y: c.y), control: CGPoint(x: c.x - k, y: c.y + k))
        p.addQuadCurve(to: CGPoint(x: c.x, y: c.y - r), control: CGPoint(x: c.x - k, y: c.y - k))
        return p
    }
}

/// How long a star takes to come or go, on a row and on the bar: 200 ms, or under Reduce Motion a fade of 120 ms.
func tlStarFade(reduce: Bool) -> Animation { .easeOut(duration: reduce ? TLMotion.reducedFade : 0.2) }

/// The star on the bar over a mark: filled for his, outlined for hers, and one turns into the other (she picked it, he took it for his own) by a fade. It stands 7 pt wide on the top edge
/// of the bar, with a glow.
struct TLBarStar: View {
    let hers: Bool
    var reduce = false

    var body: some View {
        ZStack {
            Star4().fill(TLPaint.warm.color).shadow(color: RGB(255, 226, 186).color(0.9), radius: 2.5).opacity(hers ? 0 : 1)
            Star4().stroke(TLPaint.warm.color, lineWidth: 0.9).shadow(color: RGB(255, 226, 186).color(0.5), radius: 1.5).opacity(hers ? 1 : 0)
        }
        .frame(width: 7, height: 7)
        .animation(tlStarFade(reduce: reduce), value: hers)
    }
}

/// the star on the corner of a row's circle, or after a place's name: the dial's lit star for his mark and its outlined one for hers, and one turns into the other by a fade
struct TLRowStar: View {
    let hers: Bool
    @Environment(\.accessibilityReduceMotion) private var reduce

    var body: some View {
        ZStack {
            MarkShape(kd: .lit).opacity(hers ? 0 : 1)
            MarkShape(kd: .next).opacity(hers ? 1 : 0)
        }
        .animation(tlStarFade(reduce: reduce), value: hers)
    }
}

/// where a star turned on is in its burst: the star's size, and how far the sparks have flown (0 before and 1 after, when they are gone)
struct TLBurst {
    var scale = 1.0
    var spark = 0.0
}

/// The star in the open row. Filled warm white when it is his mark, outlined in warm white when it is her pick, outlined and quiet when it is nobody's. Turned on it goes from 0.6 to 1.15 to 1
/// of its size over 300 ms with four small sparks flying out from it (`pop` is what bumps when it was turned on, from here or from the menu); under Reduce Motion it is a fade of 120 ms.
struct TLStarButton: View {
    let on: Bool                // his mark
    let hers: Bool              // her pick
    let pop: Int
    var reduce = false
    let action: () -> Void

    private static let size = 19.0

    var body: some View {
        Button(action: action) {
            Group {
                if reduce {
                    glyph
                } else {
                    KeyframeAnimator(initialValue: TLBurst(), trigger: pop) { v in
                        ZStack {
                            sparks(v.spark)
                            glyph.scaleEffect(v.scale)
                        }
                    } keyframes: { _ in
                        KeyframeTrack(\.scale) {
                            MoveKeyframe(0.6)
                            CubicKeyframe(1.15, duration: 0.12)
                            CubicKeyframe(1.0, duration: 0.18)
                        }
                        KeyframeTrack(\.spark) {
                            MoveKeyframe(0)
                            LinearKeyframe(1, duration: 0.3)
                        }
                    }
                }
            }
            .frame(width: 38, height: 38)
            .background(Color.white.opacity(0.1), in: Circle())
            .contentShape(Circle().inset(by: -3))
        }
        .buttonStyle(PressScale())
        .accessibilityLabel(on ? "取消重要" : "标为重要")
        .accessibilityValue(on ? "已标" : hers ? "她挑的" : "未标")
    }

    /// two stars on each other, one filled and one outlined, of which the fade shows one
    private var glyph: some View {
        ZStack {
            Star4().fill(TLPaint.warm.color).shadow(color: RGB(255, 226, 186).color(0.9), radius: 3).opacity(on ? 1 : 0)
            Star4().stroke(hers ? TLPaint.warm.color : ink.opacity(0.55), lineWidth: 1.3).opacity(on ? 0 : 1)
        }
        .frame(width: Self.size, height: Self.size)
        .animation(tlStarFade(reduce: reduce), value: on)
    }

    /// four sparks on the diagonals: each flies out 11 pt as the burst goes, brightest half way, and is nothing before and after
    private func sparks(_ p: Double) -> some View {
        let out = 1 - pow(1 - p, 2), a = min(1, 4 * p * (1 - p))
        return ZStack {
            ForEach(0..<4, id: \.self) { k in
                let angle = Double(k) * .pi / 2 + .pi / 4
                Circle().fill(TLPaint.warm.color).frame(width: 3, height: 3)
                    .offset(x: cos(angle) * (9 + 11 * out), y: sin(angle) * (9 + 11 * out))
                    .scaleEffect(1 - 0.4 * p)
            }
        }
        .opacity(a)
        .allowsHitTesting(false)
    }
}
