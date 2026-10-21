import SwiftUI

/// Today's ring: the past solid in its colours, the future a faint track with a tick inside its edge for each thing planned, a small white dot at now.
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
            for t in day.ahead where t.t > now {
                let tick = WatchRing.tick(minute: t.t, centre: centre, radius: r, thickness: Double(thickness))
                var p = Path()
                p.move(to: tick.from)
                p.addLine(to: tick.to)
                ctx.stroke(p, with: .color((t.waits ? WatchTheme.amber : WatchTheme.muted).opacity(k)), style: StrokeStyle(lineWidth: tick.width, lineCap: .round))
            }
            let dot = WatchRing.dot(thickness: Double(thickness))
            let d = WatchRing.point(minute: now, centre: centre, radius: r)
            ctx.fill(Path(ellipseIn: CGRect(x: d.x - dot.radius, y: d.y - dot.radius, width: 2 * dot.radius, height: 2 * dot.radius)), with: .color(.black))
            let core = dot.radius - dot.edge
            ctx.fill(Path(ellipseIn: CGRect(x: d.x - core, y: d.y - core, width: 2 * core, height: 2 * core)), with: .color(.white))
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
