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
    var symbol: String?
    var action: () -> Void

    var body: some View {
        let b = Button(action: action) {
            HStack(spacing: 4) {
                if let symbol { Image(systemName: symbol).font(.system(size: 12, weight: .semibold)) }
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

/// 今天要办的: the headline the phone sent, then 等你 N (a reminder's check answers it here; a tap on a row opens its card over her), then 接下来,
/// and last the way into 设置.
struct WatchTodayPage: View {
    var day: WatchDay
    var answered: Set<String>
    var open: (_ id: String) -> Void
    var answer: (_ id: String, _ action: String) -> Void
    var openSettings: () -> Void
    var mark: (() -> Void)?    // 标一下, when the brain has its route
    var atEnd = false          // opens scrolled to the bottom (the debug scenes)

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
                if let mark {
                    Button(action: mark) {
                        WatchRowView(symbol: "star", color: WatchTheme.warm, title: Words.Watch.mark, sub: "", right: "") { EmptyView() }
                    }
                    .buttonStyle(.plain)
                    .padding(.top, 14)
                }
                Button(action: openSettings) {
                    WatchRowView(symbol: "gearshape", color: WatchTheme.icon, title: Words.Watch.settings, sub: "", right: "") {
                        Image(systemName: "chevron.right").font(.system(size: 13, weight: .semibold)).foregroundStyle(WatchTheme.muted.opacity(0.8))
                    }
                }
                .buttonStyle(.plain)
                .padding(.top, 14)
            }
            .padding(.horizontal, 4)
            .padding(.bottom, 16)
        }
        .defaultScrollAnchor(atEnd ? .bottom : .top)
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

/// 设置, pushed from the end of 今天要办的; she stays in the corner. Only what belongs on the wrist: her voice and her taps (kept on the watch, WatchSettings),
/// whether the phone is there, and how to make her what he sees when he raises his wrist. The rest is on the iPhone.
struct WatchSettingsPage: View {
    var reachable: Bool
    var atEnd = false          // opens scrolled to the bottom (the debug scenes)
    @AppStorage(WatchSettings.taps) private var taps = true

    var body: some View {
        ScrollViewReader { proxy in ScrollView {
            VStack(alignment: .leading, spacing: 4) {
                Text(Words.Watch.settings).font(.system(size: 17, weight: .bold)).foregroundStyle(WatchTheme.ink).padding(.leading, 2)
                header(Words.Watch.settingsHer)
                later(Words.Watch.speaks, Words.Watch.speaksOff)          // 以后: nothing plays her voice on the watch yet
                toggle(Words.Watch.taps, nil, $taps)
                header(Words.Watch.link)
                cell {
                    HStack {
                        Text(Words.Watch.iPhone).font(.system(size: 14.5, weight: .medium)).foregroundStyle(WatchTheme.ink)
                        Spacer(minLength: 4)
                        Circle().fill(reachable ? Color.green : WatchTheme.faint).frame(width: 7, height: 7)
                        Text(reachable ? Words.Watch.linked : Words.Watch.unlinked).font(.system(size: 13)).foregroundStyle(WatchTheme.muted).lineLimit(1)
                    }
                }
                later(Words.Watch.standalone, Words.Watch.standaloneNote)          // 以后: phone-free cellular is not in this version
                header(Words.Watch.stayTitle)
                Text(Words.Watch.stayHow).font(.system(size: 12)).foregroundStyle(WatchTheme.muted).padding(.horizontal, 2).fixedSize(horizontal: false, vertical: true)
                Text(Words.Watch.elsewhere(Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? ""))
                    .font(.system(size: 11.5)).foregroundStyle(WatchTheme.faint).multilineTextAlignment(.center)
                    .frame(maxWidth: .infinity).padding(.top, 7)
                    .id("end")
            }
            .padding(.horizontal, 8)
            .padding(.bottom, 12)
        }
        .task {          // after the push has laid it out (an initial anchor lands short of the end here)
            guard atEnd else { return }
            try? await Task.sleep(for: .milliseconds(600))
            proxy.scrollTo("end", anchor: .bottom)
        } }
        .toolbar { WatchCornerSeat() }
    }

    private func header(_ text: String) -> some View {
        Text(text).font(.system(size: 11.5, weight: .semibold)).foregroundStyle(WatchTheme.faint).padding(.top, 8).padding(.leading, 2)
    }

    /// a setting's rounded cell, in the board's grey
    private func cell<C: View>(@ViewBuilder _ content: () -> C) -> some View {
        content()
            .padding(.horizontal, 12).padding(.vertical, 7)
            .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
            .background(Color.white.opacity(0.12), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
    }

    /// a setting not in this version: greyed, tagged 以后, not tappable
    private func later(_ title: String, _ note: String) -> some View {
        cell {
            VStack(alignment: .leading, spacing: 1) {
                Text(title).font(.system(size: 14.5, weight: .medium)).foregroundStyle(WatchTheme.ink)
                HStack(alignment: .firstTextBaseline, spacing: 4) {
                    Text(Words.Watch.later).font(.system(size: 10.5, weight: .medium)).foregroundStyle(WatchTheme.ink)
                        .padding(.horizontal, 6).padding(.vertical, 1).background(Color.white.opacity(0.14), in: Capsule())
                    Text(note).font(.system(size: 11.5)).foregroundStyle(WatchTheme.muted)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .opacity(0.45)
        .accessibilityAddTraits(.isStaticText)
    }

    private func toggle(_ title: String, _ sub: String?, _ on: Binding<Bool>) -> some View {
        cell {
            Toggle(isOn: on) {
                VStack(alignment: .leading, spacing: 1) {
                    Text(title).font(.system(size: 14.5, weight: .medium)).foregroundStyle(WatchTheme.ink)
                    if let sub { Text(sub).font(.system(size: 11.5)).foregroundStyle(WatchTheme.muted) }
                }
            }
            .tint(WatchTheme.her)
        }
    }
}

/// The wrist's own settings, kept on the watch (UserDefaults). 提醒时轻敲 gates the tap when something new waits (WatchRoot). 她说出声 and 只在戴耳机时
/// are 以后 in v1: the page shows 她说出声 greyed, and these keys wait for her voice on the watch.
enum WatchSettings {
    static let speaks = "watch.speaks"                      // 她说出声: off, words only
    static let headphonesOnly = "watch.speaksHeadphonesOnly"  // 只在戴耳机时: shown once she speaks
    static let taps = "watch.taps"                          // 提醒时轻敲: on
    /// 标一下's entry (今天要办的, jarvis-watch://mark): on once POST /inherent/day/mark (ADR 0409) is live on the Pi
    static let markReady = false
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
