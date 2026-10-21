import SwiftUI

/// Today's ring: the past solid in its colours, the future a faint track with a tick for each thing planned, a white dot at now.
/// Shared by the home screen (thick) and the complications (thin).
struct WatchRingView: View {
    var day: WatchDay
    var now: Double
    var thickness: CGFloat
    /// Always On: everything but the now dot is dimmed
    var dim = false

    var body: some View {
        Canvas { ctx, size in
            let side = min(size.width, size.height)
            let centre = CGPoint(x: size.width / 2, y: size.height / 2)
            let r = Double(side - thickness) / 2
            let k = dim ? 0.4 : 1.0
            let track = Path(ellipseIn: CGRect(x: centre.x - r, y: centre.y - r, width: 2 * r, height: 2 * r))
            ctx.stroke(track, with: .color(WatchTheme.track.opacity(k)), lineWidth: thickness)
            // SwiftUI's arc runs from the right (3 o'clock), and `clockwise: false` is clockwise on screen: turn it a quarter back so 0 is the top
            for a in WatchRing.arcs(day.blocks) {
                var p = Path()
                p.addArc(center: centre, radius: r, startAngle: .radians(a.start - .pi / 2), endAngle: .radians(a.end - .pi / 2), clockwise: false)
                ctx.stroke(p, with: .color(WatchTheme.color(day.blocks[a.index].rgb).opacity(k)), style: StrokeStyle(lineWidth: thickness, lineCap: .butt))
            }
            let half = Double(thickness) * 0.9
            for t in day.ahead where t.t > now {
                var p = Path()
                p.move(to: WatchRing.point(minute: t.t, centre: centre, radius: r - half))
                p.addLine(to: WatchRing.point(minute: t.t, centre: centre, radius: r + half))
                ctx.stroke(p, with: .color((t.waits ? WatchTheme.amber : WatchTheme.ink.opacity(0.7)).opacity(k)), style: StrokeStyle(lineWidth: max(1.5, thickness * 0.3), lineCap: .round))
            }
            let d = WatchRing.point(minute: now, centre: centre, radius: r)
            let dr = Double(thickness) * 0.8
            let dot = Path(ellipseIn: CGRect(x: d.x - dr, y: d.y - dr, width: 2 * dr, height: 2 * dr))
            ctx.stroke(dot, with: .color(.black), lineWidth: max(1, thickness * 0.25))
            ctx.fill(dot, with: .color(WatchTheme.warm))
        }
        .accessibilityHidden(true)
    }
}

/// the ring's touchable band, for the home screen's taps on a block
struct WatchRingBand: Shape {
    var thickness: CGFloat
    func path(in rect: CGRect) -> Path {
        let side = min(rect.width, rect.height)
        let r = (side - thickness) / 2
        return Path(ellipseIn: CGRect(x: rect.midX - r, y: rect.midY - r, width: 2 * r, height: 2 * r)).strokedPath(StrokeStyle(lineWidth: thickness))
    }
}
