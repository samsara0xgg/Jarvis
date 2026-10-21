import SwiftUI

/// One row of the list, in the phone timeline's anatomy (Sky/DayTimelineRowViews.swift): the icon in its circle (dashed for what has not happened), the title,
/// a grey line under it, and what is at the right. Not a card: nothing around it.
struct WatchRowView<Trailing: View>: View {
    var symbol: String
    var color: Color
    var title: String
    var sub: String
    var right: String
    var planned = false
    @ViewBuilder var trailing: Trailing

    var body: some View {
        HStack(spacing: 8) {
            ZStack {
                if planned { Circle().strokeBorder(WatchTheme.ink.opacity(0.34), style: StrokeStyle(lineWidth: 1, dash: [2.5, 2.5])) } else { Circle().fill(Color.white.opacity(0.08)) }
                Image(systemName: symbol).font(.system(size: 13)).foregroundStyle(color)
            }
            .frame(width: 28, height: 28)
            VStack(alignment: .leading, spacing: 1) {
                Text(title).font(.system(size: 14.5, weight: .medium)).foregroundStyle(WatchTheme.ink).lineLimit(1)
                if !sub.isEmpty { Text(sub).font(.system(size: 11.5)).foregroundStyle(WatchTheme.muted).lineLimit(1) }
            }
            Spacer(minLength: 4)
            if !right.isEmpty { Text(right).font(.system(size: 12).monospacedDigit()).foregroundStyle(WatchTheme.muted).lineLimit(1).fixedSize() }
            trailing
        }
        .frame(minHeight: 44)
        .contentShape(Rectangle())
    }
}

extension WatchRowView where Trailing == EmptyView {
    init(symbol: String, color: Color, title: String, sub: String, right: String, planned: Bool = false) {
        self.init(symbol: symbol, color: color, title: title, sub: sub, right: right, planned: planned) { EmptyView() }
    }
}

/// a section's small, quiet header
struct WatchHeader: View {
    var text: String
    var tint: Color = WatchTheme.muted
    var body: some View {
        Text(text).font(.system(size: 11, weight: .semibold)).foregroundStyle(tint).padding(.top, 10).padding(.bottom, 2)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// the day's list under the home screen: 现在, 等你 (each one answerable), 接下来
struct WatchList: View {
    var day: WatchDay
    var answered: Set<String>
    var open: (WatchRoot.Route) -> Void
    var answer: (_ id: String, _ action: String) -> Void

    var body: some View {
        let waiting = day.waiting.filter { !answered.contains($0.id) }
        VStack(spacing: 0) {
            if day.current != nil || day.ride != nil {
                WatchHeader(text: Words.Watch.now)
                if let r = day.ride {
                    Button { open(.ride) } label: {
                        WatchRowView(symbol: "bus", color: WatchTheme.icon, title: r.title, sub: r.sub, right: r.route) {
                            Image(systemName: "chevron.right").font(.system(size: 11, weight: .semibold)).foregroundStyle(WatchTheme.muted)
                        }
                    }.buttonStyle(.plain)
                }
                if let c = day.current { row(c) }
            }
            if !waiting.isEmpty {
                WatchHeader(text: Words.Watch.waiting, tint: WatchTheme.amber)
                ForEach(waiting) { w in
                    Button { open(.waiting(w.id)) } label: {
                        WatchRowView(symbol: w.symbol, color: WatchTheme.amber, title: w.title, sub: w.sub, right: "") {
                            switch w.kind {
                            case .reminder: check(w)
                            case .card: Image(systemName: "chevron.right").font(.system(size: 11, weight: .semibold)).foregroundStyle(WatchTheme.muted)
                            }
                        }
                    }.buttonStyle(.plain)
                }
            }
            if !day.next.isEmpty {
                WatchHeader(text: Words.Watch.next)
                ForEach(day.next) { row($0) }
            }
            if day.current == nil, day.ride == nil, waiting.isEmpty, day.next.isEmpty {
                Text(Words.Watch.nothingMore).font(.system(size: 13)).foregroundStyle(WatchTheme.muted).padding(.top, 16)
            }
        }
        .padding(.bottom, 24)
    }

    private func row(_ r: WatchDay.Row) -> some View {
        WatchRowView(symbol: r.symbol, color: r.rgb.map(WatchTheme.color) ?? WatchTheme.icon, title: r.title, sub: r.sub, right: r.right, planned: r.planned)
    }

    /// a reminder's check: its first action, without opening it
    private func check(_ w: WatchDay.Waiting) -> some View {
        Button { if let a = w.actions.first { answer(w.id, a.id) } } label: {
            Image(systemName: "checkmark").font(.system(size: 13, weight: .semibold)).foregroundStyle(WatchTheme.amber)
                .frame(width: 32, height: 32).overlay(Circle().strokeBorder(WatchTheme.amber.opacity(0.6), lineWidth: 1.5))
        }
        .buttonStyle(.plain)
        .accessibilityLabel(w.actions.first?.title ?? "")
    }
}
