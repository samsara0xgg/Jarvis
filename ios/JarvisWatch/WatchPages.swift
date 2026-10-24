import SwiftUI

/// the corner she sits in on a page: top right, about 58 pt of her on 208, kept free here (WatchRoot draws her there). With something at the top left too,
/// watchOS centres the time; a page with nothing there keeps that place empty (`balanced`) so the time is centred all the same.
struct WatchCornerSeat: ToolbarContent {
    var balanced = false
    var body: some ToolbarContent {
        if balanced { ToolbarItem(placement: .topBarLeading) { Color.clear.frame(width: 44, height: 44).accessibilityHidden(true) } }
        ToolbarItem(placement: .topBarTrailing) { Color.clear.frame(width: 44, height: 44).accessibilityHidden(true) }
    }
}

/// a card's button: the first filled (amber, or white on her answer) and answering a double tap; the second quiet. Both big enough for a finger.
struct WatchPill: View {
    var title: String
    var primary: Bool
    var fill: Color = WatchTheme.amber
    var action: () -> Void

    var body: some View {
        let b = Button(action: action) {
            HStack(spacing: 4) {
                Text(title).font(.system(size: 14.5, weight: .semibold)).lineLimit(1).minimumScaleFactor(0.7)
                if primary { Text(Words.Watch.doubleTap).font(.system(size: 10, weight: .medium)).lineLimit(1).opacity(0.65) }
            }
            .foregroundStyle(primary ? WatchTheme.darkInk : WatchTheme.ink)
            .padding(.horizontal, 8)
            .frame(maxWidth: .infinity, minHeight: 40)
            .background(primary ? fill : Color.white.opacity(0.14), in: Capsule())
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        if primary { b.handGestureShortcut(.primaryAction) } else { b }
    }
}

/// 今天要办的: the headline the phone sent, then 等你 N (a reminder's check answers it here; a tap on a row opens its card over her), then 接下来.
struct WatchTodayPage: View {
    var day: WatchDay
    var answered: Set<String>
    var open: (_ id: String) -> Void
    var answer: (_ id: String, _ action: String) -> Void

    var body: some View {
        let waiting = day.waiting.filter { !answered.contains($0.id) }
        ScrollView {
            VStack(spacing: 0) {
                if let h = day.headline {
                    VStack(alignment: .leading, spacing: 1) {
                        Text(h.title).font(.system(size: 14, weight: .semibold)).foregroundStyle(WatchTheme.ink)
                        Text(h.note).font(.system(size: 13)).foregroundStyle(h.waits ? WatchTheme.amber : WatchTheme.muted)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                if !waiting.isEmpty {
                    WatchHeader(text: Words.Watch.waitingCount(waiting.count), tint: WatchTheme.amber)
                    ForEach(waiting) { w in
                        Button { open(w.id) } label: {
                            WatchRowView(symbol: w.symbol, color: WatchTheme.icon, title: w.title, sub: w.sub, right: "") {
                                if w.kind == .reminder, let a = w.actions.first { check(w.id, a) }
                            }
                        }
                        .buttonStyle(.plain)
                    }
                }
                if !day.next.isEmpty {
                    WatchHeader(text: Words.Watch.next)
                    ForEach(day.next) { r in
                        WatchRowView(symbol: r.symbol, color: r.rgb.map(WatchTheme.color) ?? WatchTheme.icon, title: r.title, sub: r.sub, right: r.right, planned: r.planned)
                    }
                }
                if day.headline == nil, waiting.isEmpty, day.next.isEmpty {
                    Text(Words.Watch.nothingMore).font(.system(size: 13)).foregroundStyle(WatchTheme.muted).padding(.top, 16)
                }
            }
            .padding(.horizontal, 4)
            .padding(.bottom, 16)
        }
        .toolbar { WatchCornerSeat(balanced: true) }
    }

    /// a reminder's check: its first action, without opening it
    private func check(_ id: String, _ a: WatchDay.Action) -> some View {
        Button { answer(id, a.id) } label: {
            Image(systemName: "checkmark").font(.system(size: 13, weight: .semibold)).foregroundStyle(WatchTheme.amber)
                .frame(width: 30, height: 30).overlay(Circle().strokeBorder(WatchTheme.amber, lineWidth: 1.6))
                .frame(width: 40, height: 40).contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(a.title)
    }
}

/// the bus he is on: how many stops, where to, the stops as dots on a line, and the tap he will get
struct WatchRideView: View {
    var ride: WatchDay.Ride

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(ride.title).font(.system(size: 28, weight: .semibold)).foregroundStyle(WatchTheme.ink).lineLimit(1).minimumScaleFactor(0.6)
            Text(ride.sub).font(.system(size: 13)).foregroundStyle(WatchTheme.muted).lineLimit(1)
            stops.padding(.top, 14)
            HStack {
                Text(ride.stops.first ?? "")
                Spacer(minLength: 8)
                Text(ride.stops.last ?? "")
            }
            .font(.system(size: 11)).foregroundStyle(WatchTheme.muted).lineLimit(1)
            if let note = ride.note {
                HStack(alignment: .top, spacing: 6) {
                    Image(systemName: "applewatch.radiowaves.left.and.right").font(.system(size: 12)).foregroundStyle(WatchTheme.muted)
                    Text(note).font(.system(size: 12)).foregroundStyle(ride.alightNext ? WatchTheme.amber : WatchTheme.muted)
                }
                .padding(.top, 8)
            }
            Spacer(minLength: 0)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 2)
        .toolbar {
            ToolbarItem(placement: .topBarLeading) {
                HStack(spacing: 4) {
                    Image(systemName: "bus").font(.system(size: 12))
                    Text(ride.route).font(.system(size: 12.5, weight: .semibold))
                }
                .foregroundStyle(WatchTheme.trip)
            }
            WatchCornerSeat()
        }
    }

    /// the stops: passed ones filled, the one he is at larger and white, the ones to come hollow
    private var stops: some View {
        Canvas { ctx, size in
            let n = ride.stops.count
            guard n > 0 else { return }
            let y = size.height / 2, pad = 8.0
            let x = { (i: Int) -> Double in n == 1 ? size.width / 2 : pad + (size.width - 2 * pad) * Double(i) / Double(n - 1) }
            var line = Path(); line.move(to: CGPoint(x: pad, y: y)); line.addLine(to: CGPoint(x: size.width - pad, y: y))
            ctx.stroke(line, with: .color(WatchTheme.ink.opacity(0.2)), lineWidth: 1.5)
            let r = n > 12 ? 2.5 : 3.5
            for i in 0..<n {
                if i == ride.at { continue }
                let c = CGRect(x: x(i) - r, y: y - r, width: 2 * r, height: 2 * r)
                if i < ride.at { ctx.fill(Path(ellipseIn: c), with: .color(WatchTheme.muted)) }
                else {
                    ctx.fill(Path(ellipseIn: c), with: .color(.black))
                    ctx.stroke(Path(ellipseIn: c), with: .color(WatchTheme.muted), lineWidth: 1.2)
                }
            }
            if ride.stops.indices.contains(ride.at) {
                let R = 6.5
                ctx.fill(Path(ellipseIn: CGRect(x: x(ride.at) - R, y: y - R, width: 2 * R, height: 2 * R)), with: .color(.white))
            }
        }
        .frame(height: 20)
        .accessibilityHidden(true)
    }
}
