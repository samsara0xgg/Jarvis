import SwiftUI
import WidgetKit

// The complications' views. They are also compiled into the watch app (JarvisWatch/WatchDebugScene.swift draws them in a gallery, since the simulator cannot
// easily put a complication on a watch face), so they take the day and a minute and nothing from WidgetKit's timeline.

/// her small at-home look inside a thin ring of today, and an amber dot while something waits
struct CircularComplication<Her: View>: View {
    var day: WatchDay?
    var now: Double
    @ViewBuilder var her: Her

    var body: some View {
        ZStack {
            if let day { WatchRingView(day: day, now: now, thickness: 4) }
            her.padding(7)
            if let day, !day.waiting.isEmpty {
                Circle().fill(WatchTheme.amber).frame(width: 7, height: 7).widgetAccentable().offset(x: 17, y: -17)          // on the ring's edge, upper right
            }
        }
    }
}

/// her, and beside her what waits (等你 N and the first of it) or else the headline: the face's rectangle and the Smart Stack's card
struct RectangularComplication<Her: View>: View {
    var day: WatchDay?
    @ViewBuilder var her: Her
    var body: some View {
        HStack(spacing: 6) {
            her.frame(width: 36, height: 36)
            lines
        }
    }

    @ViewBuilder private var lines: some View {
        VStack(alignment: .leading, spacing: 1) {
            if let w = day?.waiting, let first = w.first {
                Text(Words.Watch.waitingCount(w.count)).font(.system(size: 13, weight: .semibold)).foregroundStyle(WatchTheme.amber).widgetAccentable()
                Text(first.title).font(.system(size: 15, weight: .semibold)).lineLimit(1).minimumScaleFactor(0.8)
            } else if let h = day?.headline {
                Text(h.title).font(.system(size: 15, weight: .semibold)).lineLimit(1).minimumScaleFactor(0.8)
                Text(h.note).font(.system(size: 13)).foregroundStyle(h.waits ? WatchTheme.amber : WatchTheme.muted).lineLimit(1)
                    .widgetAccentable(h.waits)
            } else {
                Text(Words.Watch.name).font(.system(size: 15, weight: .semibold))
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// 等你 N, or the headline when nothing waits
@MainActor func complicationLine(_ day: WatchDay?) -> String {
    if let n = day?.waiting.count, n > 0 { return Words.Watch.waitingCount(n) }
    if let h = day?.headline { return "\(h.title) \(h.note)" }
    return Words.Watch.name
}

struct InlineComplication: View {
    var day: WatchDay?
    var body: some View { Text(complicationLine(day)) }
}

struct CornerComplication<Her: View>: View {
    var day: WatchDay?
    @ViewBuilder var her: Her
    var body: some View {
        her.padding(4).widgetLabel { Text(complicationLine(day)) }
    }
}
