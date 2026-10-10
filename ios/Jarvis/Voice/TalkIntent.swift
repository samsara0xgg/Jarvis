import AppIntents
import Foundation

// Compiled into the app and the widget extension (the Control Center button names the intent); the widget never runs it:
// openAppWhenRun launches the app and runs `perform` there.

/// A request to start talking, from an intent that may fire before the session exists (a cold launch from the Action button).
enum TalkRequest {
    static let notification = Notification.Name("com.allen.jarvis.talk")
    nonisolated(unsafe) static var pending = false
}

struct TalkIntent: AppIntent {
    static let title: LocalizedStringResource = "跟 Jarvis 说话"
    static let description = IntentDescription("Talk to Jarvis")
    static let openAppWhenRun = true

    @MainActor
    func perform() async throws -> some IntentResult {
        TalkRequest.pending = true
        NotificationCenter.default.post(name: TalkRequest.notification, object: nil)
        return .result()
    }
}
