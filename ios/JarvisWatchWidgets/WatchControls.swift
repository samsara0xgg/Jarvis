import AppIntents
import SwiftUI
import WidgetKit

// Control Center keys (watchOS 26 controls). Each opens the app and asks it for what its link names, as the complication's tap (`jarvis-watch://talk`) does.
// Compiled into the watch app too, as the phone's TalkIntent is: openAppWhenRun launches the app and runs `perform` there; the extension never runs it.

/// 跟她说: her, already listening
@available(watchOS 26.0, *)
struct TalkToHerControl: ControlWidget {
    var body: some ControlWidgetConfiguration {
        StaticControlConfiguration(kind: "com.yilunshi.jarvis.watch.talk") {
            ControlWidgetButton(action: OpenHerIntent(link: "talk")) {
                Label(MainActor.assumeIsolated { Words.Watch.talkToHer }, systemImage: "waveform")
            }
        }
        .displayName(LocalizedStringResource(stringLiteral: MainActor.assumeIsolated { Words.Watch.talkToHer }))
    }
}

/// 标一下: what covers this moment is marked (the app's 标一下, ADR 0409). Not in the bundle until WatchSettings.markReady.
@available(watchOS 26.0, *)
struct MarkControl: ControlWidget {
    var body: some ControlWidgetConfiguration {
        StaticControlConfiguration(kind: "com.yilunshi.jarvis.watch.mark") {
            ControlWidgetButton(action: OpenHerIntent(link: "mark")) {
                Label(MainActor.assumeIsolated { Words.Watch.mark }, systemImage: "star")
            }
        }
        .displayName(LocalizedStringResource(stringLiteral: MainActor.assumeIsolated { Words.Watch.mark }))
    }
}

/// what a key asked the app for (`talk`, `mark`), which may come before her screen is up (a cold launch): WatchRoot takes it when it appears, or when told
enum HerRequest {
    static let notification = Notification.Name("com.yilunshi.jarvis.watch.request")
    nonisolated(unsafe) static var pending: String?
}

/// opens the app and asks it for `link`
struct OpenHerIntent: AppIntent {
    static let title: LocalizedStringResource = "Jarvis"
    static let openAppWhenRun = true
    static let isDiscoverable = false

    @Parameter(title: "Link") var link: String

    init() {}
    init(link: String) { self.link = link }

    @MainActor
    func perform() async throws -> some IntentResult {
        HerRequest.pending = link
        NotificationCenter.default.post(name: HerRequest.notification, object: nil)
        return .result()
    }
}
