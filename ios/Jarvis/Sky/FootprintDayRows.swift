import SwiftUI

// The two small drawings the footprint page adds to a row of the timeline's list (`TLRowView` draws them from a `TLEntry`): the numbered ring of a stop, and the ribbon of an outing's speed.

/// a stop of an outing: its number in a ring, as the map wears it
struct TLStopToken: View {
    let number: Int
    static let size = 18.0

    var body: some View {
        Text("\(number)").font(.system(size: 10, weight: .medium).monospacedDigit()).foregroundStyle(TLPaint.ink.color)
            .frame(width: Self.size, height: Self.size)
            .background(Circle().fill(FootprintPaint.token.color))
            .overlay(Circle().inset(by: 0.625).stroke(TLPaint.ink.color(0.85), lineWidth: 1.25))
    }
}

/// An outing's speed along it, 4 pt tall: slices of equal time in the ramp's colours, blending from one to the next, with a hole where he stood still or nothing was recorded.
struct TLRibbon: View {
    let cells: [RGB?]
    static let height = 4.0

    var body: some View {
        Canvas { ctx, size in
            let w = size.width / Double(max(1, cells.count))
            var i = 0
            while i < cells.count {
                guard cells[i] != nil else { i += 1; continue }
                var j = i
                while j + 1 < cells.count, cells[j + 1] != nil { j += 1 }
                let colors = (i...j).compactMap { cells[$0]?.color }
                let rect = CGRect(x: Double(i) * w, y: 0, width: Double(j - i + 1) * w, height: size.height)
                ctx.fill(Path(rect), with: .linearGradient(Gradient(colors: colors.count > 1 ? colors : colors + colors),
                                                           startPoint: CGPoint(x: rect.minX + w / 2, y: 0), endPoint: CGPoint(x: rect.maxX - w / 2, y: 0)))
                i = j + 1
            }
        }
        .clipShape(Capsule())
        .accessibilityHidden(true)
    }
}
