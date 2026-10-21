import SwiftUI

/// The first screen: today's ring with her in it, and under her what comes next (or, for a few seconds, the block he tapped on the ring).
/// She herself is drawn by `WatchRoot` (one view that grows from here into the corner as the list scrolls up); this page leaves her a place.
struct WatchHomePage: View {
    var day: WatchDay?
    @Binding var told: Int?
    var openRide: () -> Void
    var herPlace: (CGRect) -> Void
    @Environment(\.isLuminanceReduced) private var aod

    var body: some View {
        GeometryReader { g in
            let side = max(0, min(g.size.width, g.size.height) - 12)
            let thick = side * 0.055
            ZStack {
                if let day {
                    TimelineView(.everyMinute) { ctx in
                        WatchRingView(day: day, now: WatchRing.nowMinute(day, at: ctx.date), thickness: thick, dim: aod)
                    }
                    .frame(width: side, height: side)
                    ring(day, side: side, thick: thick)
                }
                Color.clear.frame(width: 80, height: 80).offset(y: -side * 0.13)
                    .onGeometryChange(for: CGRect.self) { $0.frame(in: .named(WatchRoot.space)) } action: { herPlace($0) }
                if !aod { centre(side: side) }
            }
            .frame(width: g.size.width, height: g.size.height)
        }
    }

    /// a tap on the ring's band: the block under the finger, told in the centre
    private func ring(_ day: WatchDay, side: CGFloat, thick: CGFloat) -> some View {
        let band = thick + 14
        return Color.clear.frame(width: side, height: side)
            .contentShape(WatchRingBand(thickness: band))
            .onTapGesture(coordinateSpace: .local) { p in
                let c = CGPoint(x: side / 2, y: side / 2)
                told = WatchRing.hit(p, centre: c, radius: Double(side - thick) / 2, band: Double(band) / 2, blocks: day.blocks)
            }
    }

    @ViewBuilder private func centre(side: CGFloat) -> some View {
        let y = side * 0.215
        if let day, let i = told, day.blocks.indices.contains(i) {
            let b = day.blocks[i]
            VStack(spacing: 2) {
                Text(b.title).font(.system(size: 15, weight: .semibold)).foregroundStyle(WatchTheme.ink).lineLimit(1)
                Text(b.sub).font(.system(size: 11)).foregroundStyle(WatchTheme.muted).multilineTextAlignment(.center).lineLimit(2).minimumScaleFactor(0.85)
            }
            .frame(width: side * 0.6).offset(y: y).transition(.opacity)
        } else if let h = day?.headline {
            let label = VStack(spacing: 1) {
                Text(h.title).font(.system(size: 15, weight: .semibold)).foregroundStyle(WatchTheme.ink).lineLimit(1).minimumScaleFactor(0.8)
                Text(h.note).font(.system(size: 13)).foregroundStyle(h.waits ? WatchTheme.amber : WatchTheme.muted).lineLimit(1)
            }
            .frame(width: side * 0.6)
            Group {
                if day?.ride != nil { Button(action: openRide) { label.contentShape(Rectangle()) }.buttonStyle(.plain) } else { label }
            }
            .offset(y: y).transition(.opacity)
        } else if day == nil {
            Text(Words.Watch.noDay).font(.system(size: 13)).foregroundStyle(WatchTheme.muted).offset(y: y)
        }
    }
}
