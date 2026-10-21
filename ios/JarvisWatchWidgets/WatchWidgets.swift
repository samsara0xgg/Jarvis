import SwiftUI
import WidgetKit

@main
struct JarvisWatchWidgets: WidgetBundle {
    var body: some Widget { HerComplication() }
}

struct HerComplication: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: "com.yilunshi.jarvis.watch.her", provider: Provider()) { _ in Text(Words.Watch.name) }
            .supportedFamilies([.accessoryCircular])
    }
    struct Provider: TimelineProvider {
        struct Entry: TimelineEntry { let date: Date }
        func placeholder(in context: Context) -> Entry { Entry(date: .now) }
        func getSnapshot(in context: Context, completion: @escaping (Entry) -> Void) { completion(Entry(date: .now)) }
        func getTimeline(in context: Context, completion: @escaping (Timeline<Entry>) -> Void) { completion(Timeline(entries: [Entry(date: .now)], policy: .never)) }
    }
}
