import AppIntents
import SwiftUI
import WidgetKit

// Two ways to start talking from outside the app, her Live Activity, and the recording's control. The talk visuals are placeholders (an SF Symbol) for the design team.

@main
struct JarvisWidgets: WidgetBundle {
    var body: some Widget {
        TalkControl()
        TalkLockWidget()
        JarvisLiveActivity()
        RideLiveActivityWidget()
        LifelogControl()
    }
}

/// Control Center, the lock screen's controls and the Action button's control slot: runs TalkIntent, which opens the app listening.
struct TalkControl: ControlWidget {
    var body: some ControlWidgetConfiguration {
        StaticControlConfiguration(kind: "com.allen.jarvis.talk-control") {
            ControlWidgetButton(action: TalkIntent()) {
                Label("跟 Jarvis 说话", systemImage: "waveform")
            }
        }
        .displayName("跟 Jarvis 说话")
        .description("Talk to Jarvis")
    }
}

/// Lock-screen circle: a tap opens the app at jarvis://talk, which starts listening.
struct TalkLockWidget: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: "com.allen.jarvis.talk-lock", provider: TalkProvider()) { _ in
            Image(systemName: "waveform")
                .font(.title2)
                .widgetURL(URL(string: "jarvis://talk"))
                .containerBackground(.clear, for: .widget)
        }
        .configurationDisplayName("跟 Jarvis 说话")
        .description("Talk to Jarvis")
        .supportedFamilies([.accessoryCircular])
    }
}

private struct TalkEntry: TimelineEntry { let date = Date() }

private struct TalkProvider: TimelineProvider {
    func placeholder(in context: Context) -> TalkEntry { TalkEntry() }
    func getSnapshot(in context: Context, completion: @escaping (TalkEntry) -> Void) { completion(TalkEntry()) }
    func getTimeline(in context: Context, completion: @escaping (Timeline<TalkEntry>) -> Void) {
        completion(Timeline(entries: [TalkEntry()], policy: .never))
    }
}
