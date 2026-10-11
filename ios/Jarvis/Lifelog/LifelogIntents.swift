import AppIntents
import Foundation

// Compiled into the app and the widget extension (the control and the Live Activity button name these intents). Both conform to a system intent
// kind (`AudioRecordingIntent`, `LiveActivityIntent`): iOS runs those in the app's process, in the background, without opening the app
// (unlike TalkIntent, which asks to open it). The extension never runs `perform`; its copy has no recorder (WIDGET_EXTENSION).

enum LifelogSwitch {
    static let controlKind = "com.allen.jarvis.lifelog-control"
}

/// The control's switch. Starting needs a Live Activity up while the microphone runs (`AudioRecordingIntent`'s rule): `setOn` brings it up through `LifelogActivity`.
struct LifelogToggleIntent: SetValueIntent, AudioRecordingIntent {
    static let title: LocalizedStringResource = "随身录音"
    static let description = IntentDescription("开关随身录音")

    @Parameter(title: "录音")
    var value: Bool

    @MainActor
    func perform() async throws -> some IntentResult {
        #if !WIDGET_EXTENSION
        LifelogRecorder.shared.setOn(value)
        #endif
        return .result()
    }
}

/// The Live Activity's 暂停 button.
struct LifelogPauseIntent: LiveActivityIntent {
    static let title: LocalizedStringResource = "暂停随身录音"
    static let isDiscoverable = false

    @MainActor
    func perform() async throws -> some IntentResult {
        #if !WIDGET_EXTENSION
        LifelogRecorder.shared.setOn(false)
        #endif
        return .result()
    }
}
