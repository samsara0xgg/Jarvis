import SwiftUI
import WidgetKit

@main
struct JarvisWatchWidgets: WidgetBundle {
    var body: some Widget { HerComplication() }
}

struct DayEntry: TimelineEntry {
    let date: Date
    let day: WatchDay?
    var now: Double { day.map { WatchRing.nowMinute($0, at: date) } ?? 0 }
    /// the Smart Stack puts her first only while something waits on him
    var relevance: TimelineEntryRelevance? { TimelineEntryRelevance(score: day?.waiting.isEmpty == false ? 100 : 1) }
}

struct HerComplication: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: "com.yilunshi.jarvis.watch.her", provider: Provider()) { entry in
            ComplicationEntryView(entry: entry)
                .widgetURL(URL(string: "jarvis-watch://talk"))
                .containerBackground(for: .widget) { Color.clear }
        }
        .configurationDisplayName(MainActor.assumeIsolated { Words.Watch.complicationName })
        .description(MainActor.assumeIsolated { Words.Watch.complicationDetail })
        .supportedFamilies([.accessoryCircular, .accessoryRectangular, .accessoryInline, .accessoryCorner])
    }

    struct Provider: TimelineProvider {
        func placeholder(in context: Context) -> DayEntry { DayEntry(date: .now, day: WatchDayStore.load()) }
        func getSnapshot(in context: Context, completion: @escaping (DayEntry) -> Void) { completion(DayEntry(date: .now, day: Self.day())) }
        /// the ring's dot moves every few minutes: an entry each 5 for the next hour, then the system asks again (the app also reloads when the phone sends a new day)
        func getTimeline(in context: Context, completion: @escaping (Timeline<DayEntry>) -> Void) {
            let day = Self.day(), start = Date()
            completion(Timeline(entries: (0...12).map { DayEntry(date: start.addingTimeInterval(Double($0) * 300), day: day) }, policy: .atEnd))
        }
        /// the last day the phone sent; the words follow its language
        private static func day() -> WatchDay? {
            let d = WatchDayStore.load()
            if let l = d.flatMap({ Words.Lang(rawValue: $0.language) }) { MainActor.assumeIsolated { Words.shared.set(l) } }
            return d
        }
    }
}

struct ComplicationEntryView: View {
    var entry: DayEntry
    @Environment(\.widgetFamily) private var family

    var body: some View {
        switch family {
        case .accessoryRectangular: RectangularComplication(day: entry.day) { her("her-comp-out") }
        case .accessoryInline: InlineComplication(day: entry.day)
        case .accessoryCorner: CornerComplication(day: entry.day) { her("her-comp") }
        default: CircularComplication(day: entry.day, now: entry.now) { her("her-comp-out") }
        }
    }

    /// her, as the asset catalog has her: `her-comp-out` (surfaced, reads better in the ring) or `her-comp` (the at-home rest look); kept in full colour
    private func her(_ name: String) -> some View { Image(name).resizable().widgetAccentedRenderingMode(.fullColor).scaledToFit() }
}
