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
                if planned { Circle().strokeBorder(WatchTheme.ink.opacity(0.34), style: StrokeStyle(lineWidth: 1, dash: [2.5, 2.5])) } else { Circle().fill(Color.white.opacity(0.09)) }
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
    var tint: Color = WatchTheme.faint
    var body: some View {
        Text(text).font(.system(size: 11.5, weight: .semibold)).foregroundStyle(tint).padding(.top, 10).padding(.bottom, 2)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}
