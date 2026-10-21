import SwiftUI

/// the corner she sits in on every screen but home: top right, 28 pt (watchOS then centres the time)
struct WatchCornerHer: ToolbarContent {
    var face: HerFace
    /// the screen's primary action (double tap) is talking to her
    var primary = false
    var model = WatchModel.shared
    var body: some ToolbarContent {
        ToolbarItem(placement: .topBarTrailing) {
            let b = Button { model.startTalk() } label: { HerSprite(face: face, size: 28) }.buttonStyle(.plain)
            if primary { b.handGestureShortcut(.primaryAction) } else { b }
        }
    }
}

/// big amber (or white) pill; the primary one answers a double tap
struct WatchPill: View {
    var title: String
    var primary: Bool
    var fill: Color = WatchTheme.amber
    var hint: String?
    var action: () -> Void

    var body: some View {
        let b = Button(action: action) {
            HStack {
                Text(title).font(.system(size: primary ? 18 : 15, weight: primary ? .semibold : .medium)).lineLimit(1).minimumScaleFactor(0.7)
                if let hint { Spacer(minLength: 4); Text(hint).font(.system(size: 10)).opacity(0.6) }
            }
            .foregroundStyle(primary ? WatchTheme.darkInk : WatchTheme.ink)
            .padding(.horizontal, 14)
            .frame(maxWidth: .infinity, minHeight: primary ? 46 : 38)
            .background(primary ? fill : Color.white.opacity(0.12), in: Capsule())
        }
        .buttonStyle(.plain)
        if primary { b.handGestureShortcut(.primaryAction) } else { b }
    }
}

/// 等你 1/2: one waiting item, one screen. The first action is the big one (double tap), the second a quiet one.
struct WatchWaitingView: View {
    var item: WatchDay.Waiting
    var index: Int
    var count: Int
    var answer: (_ action: String) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(item.title).font(.system(size: 28, weight: .semibold)).foregroundStyle(WatchTheme.ink).lineLimit(2).minimumScaleFactor(0.7)
            Text(item.sub).font(.system(size: 13)).foregroundStyle(WatchTheme.muted).lineLimit(2)
            Spacer(minLength: 6)
            if let a = item.actions.first { WatchPill(title: a.title, primary: true, hint: Words.Watch.doubleTap) { answer(a.id) } }
            if item.actions.count > 1 { WatchPill(title: item.actions[1].title, primary: false) { answer(item.actions[1].id) } }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 2)
        .navigationBarBackButtonHidden(true)
        .toolbar {
            ToolbarItem(placement: .topBarLeading) {
                HStack(spacing: 4) {
                    Circle().fill(WatchTheme.amber).frame(width: 7, height: 7)
                    Text(Words.Watch.waitingOf(index, count)).font(.system(size: 13, weight: .semibold)).foregroundStyle(WatchTheme.amber)
                }
            }
            WatchCornerHer(face: .ask)
        }
    }
}

/// the bus he is on: how many stops, where to, the stops as dots on a line, and the tap he will get
struct WatchRideView: View {
    var ride: WatchDay.Ride

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(ride.title).font(.system(size: 30, weight: .semibold)).foregroundStyle(WatchTheme.ink).lineLimit(1).minimumScaleFactor(0.6)
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
        .navigationBarBackButtonHidden(true)
        .toolbar {
            ToolbarItem(placement: .topBarLeading) {
                HStack(spacing: 4) {
                    Image(systemName: "bus").font(.system(size: 12)).foregroundStyle(WatchTheme.ink)
                    Text(ride.route).font(.system(size: 13, weight: .semibold)).foregroundStyle(WatchTheme.ink)
                }
            }
            WatchCornerHer(face: ride.alightNext ? .ask : .rest)
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
