import SwiftUI

/// The line under her in a talk (WatchRoot draws her): his words as she hears them, or why the talk failed. The whole screen is its button.
struct WatchCaptions: View {
    var text: String
    var tint: Color
    /// where the line starts, in screen points
    var top: CGFloat
    var tap: () -> Void

    var body: some View {
        Button(action: tap) {
            Text(text).font(.system(size: 16, weight: .medium)).foregroundStyle(tint)
                .multilineTextAlignment(.center).lineLimit(2).truncationMode(.head).minimumScaleFactor(0.85)          // the newest words stay
                .padding(.horizontal, 12)
                .padding(.top, top)
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .handGestureShortcut(.primaryAction)
        .ignoresSafeArea()
    }
}

/// A card over her (WatchRoot lays it at the bottom and makes her peek above it): the watch's own dark material, translucent, a thin light line along its top
/// edge. Her answer, a waiting item, or 该出门了. With buttons, they answer it; without, a tap on it puts it away. Too long for the screen, it scrolls inside.
struct WatchCardView: View {
    var card: WatchCard
    var now: Date
    var maxHeight: CGFloat
    var answer: (_ id: String, _ action: String) -> Void
    var dismiss: () -> Void

    private static let shape = RoundedRectangle(cornerRadius: 30, style: .continuous)

    var body: some View {
        ViewThatFits(in: .vertical) {
            content
            ScrollView { content }
        }
        .frame(maxWidth: .infinity, maxHeight: maxHeight)
        .background(.regularMaterial, in: Self.shape)
        .overlay(Self.shape.strokeBorder(LinearGradient(colors: [.white.opacity(0.22), .clear], startPoint: .top, endPoint: .init(x: 0.5, y: 0.25)), lineWidth: 0.7))
        .shadow(color: .black.opacity(0.45), radius: 12, y: -6)
        .environment(\.colorScheme, .dark)
        .contentShape(Self.shape)
        .onTapGesture { if actions == nil { dismiss() } }
    }

    /// the id the buttons answer, and its actions; none on a card without buttons
    private var actions: (id: String, list: [WatchDay.Action])? {
        switch card {
        case .answer(_, _, let a?): (a.id, a.actions)
        case .waiting(let w) where !w.actions.isEmpty: (w.id, w.actions)
        case .morning: ("morning", [.init(id: "today", title: Words.Watch.seeToday), .init(id: "ok", title: Words.Watch.ok)])
        case .unsure(let u): (u.id, [.init(id: "unsure", title: Words.Watch.yes), .init(id: "talk", title: Words.Watch.no)])   // 对: WatchActionID.unsure on the phone
        default: nil
        }
    }

    private var content: some View {
        VStack(alignment: .leading, spacing: 2) {
            switch card {
            case .answer(let spoken, let written, _):
                // her first line is the card's title, the rest under it
                let lines = (written.isEmpty ? spoken : written).split(separator: "\n", maxSplits: 1).map { $0.trimmingCharacters(in: .whitespaces) }
                Text(lines.first ?? "").font(.system(size: 16.5, weight: .semibold)).foregroundStyle(WatchTheme.ink)
                if lines.count > 1 { line(lines[1]) }
            case .waiting(let w):
                label(w.symbol, w.kind == .reminder ? Words.Watch.reminder : Words.Watch.waiting, WatchTheme.amber)
                Text(w.title).font(.system(size: 18, weight: .semibold)).foregroundStyle(WatchTheme.ink).padding(.top, 1)
                if !w.sub.isEmpty { line(w.sub) }
            case .leave(let t):
                label("figure.walk", Words.Watch.leave, WatchTheme.trip)
                let left = Int((t.timeIntervalSince(now) / 60).rounded(.up))
                Text(left > 0 ? Words.Watch.minutes(left) : Words.Watch.now)
                    .font(.system(size: 24, weight: .semibold, design: .rounded)).monospacedDigit().foregroundStyle(WatchTheme.ink)
                line(Words.Watch.leaveAt(t.formatted(.dateTime.hour(.twoDigits(amPM: .omitted)).minute(.twoDigits))))
            case .morning(let slept, let things, let first):
                label("sun.max", Words.Watch.morning, WatchTheme.muted)
                Text(Words.Watch.slept(slept)).font(.system(size: 18, weight: .semibold)).foregroundStyle(WatchTheme.ink).padding(.top, 1)
                line(Words.Watch.today(things, first: first))
            case .unsure(let u):
                label("questionmark.circle", Words.Watch.unsure(u.span), WatchTheme.muted)
                HStack(alignment: .firstTextBaseline, spacing: 5) {
                    Image(systemName: u.symbol).font(.system(size: 15, weight: .semibold))
                        .foregroundStyle(u.rgb.map(WatchTheme.color) ?? WatchTheme.icon)
                    Text(Words.Watch.asked(u.title)).font(.system(size: 18, weight: .semibold)).foregroundStyle(WatchTheme.ink)
                }
                .padding(.top, 1)
                if !u.line.isEmpty { line(u.line) }
            }
            if let a = actions {
                HStack(spacing: 6) {
                    ForEach(Array(a.list.prefix(2).enumerated()), id: \.offset) { i, act in
                        WatchPill(title: act.title, primary: i == 0, fill: isAmber ? WatchTheme.amber : WatchTheme.ink, symbol: act.id == "talk" ? "mic.fill" : nil) { answer(a.id, act.id) }
                    }
                }
                .padding(.top, 8)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(EdgeInsets(top: 13, leading: 14, bottom: 12, trailing: 14))
    }

    /// what waits on him has amber buttons; her own cards have white ones
    private var isAmber: Bool { if case .waiting = card { true } else { false } }

    private func label(_ symbol: String, _ text: String, _ tint: Color) -> some View {
        HStack(spacing: 5) {
            Image(systemName: symbol).font(.system(size: 11, weight: .semibold))
            Text(text).font(.system(size: 11.5, weight: .semibold))
        }
        .foregroundStyle(tint)
    }

    private func line(_ text: String) -> some View {
        Text(text).font(.system(size: 13.5)).foregroundStyle(WatchTheme.ink.opacity(0.82)).fixedSize(horizontal: false, vertical: true)
    }
}
