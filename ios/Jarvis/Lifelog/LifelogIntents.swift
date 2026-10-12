import AppIntents
import Foundation

// Compiled into the app and the widget extension (the control and the Live Activity button name these intents). Both conform to a system intent
// kind (`AudioRecordingIntent`, `LiveActivityIntent`): iOS runs those in the app's process, in the background, without opening the app
// (unlike TalkIntent, which asks to open it). When iOS runs one in the extension instead, it only sets the switch (`setFromExtension`).

enum LifelogSwitch {
    static let controlKind = "com.allen.jarvis.lifelog-control"
    /// posted when the switch is set outside the app; the app, alive while it records, follows it at once
    static let changedNote = "com.yilunshi.jarvis.lifelog.switch" as CFString

    /// iOS runs the intent here, not in the app, when it does not route it there (seen on the phone 2026-10-11 with the app suspended):
    /// the switch is set in the App Group and the app is told; a recording app turns off at once, a suspended one follows when it next runs.
    static func setFromExtension(_ on: Bool) {
        LifelogStore.isOn = on
        CFNotificationCenterPostNotification(CFNotificationCenterGetDarwinNotifyCenter(), CFNotificationName(changedNote), nil, nil, true)
    }
}

/// The control's switch. Starting needs a Live Activity up while the microphone runs (`AudioRecordingIntent`'s rule): `turn` brings it up through
/// `LifelogActivity`. `LiveActivityIntent` is what makes iOS run it in the app, launched in the background if need be, and lets it start the
/// Live Activity there; `AudioRecordingIntent` alone left it to the extension whenever the app was not running.
struct LifelogToggleIntent: SetValueIntent, AudioRecordingIntent, LiveActivityIntent {
    static let title: LocalizedStringResource = "随身录音"
    static let description = IntentDescription("开关随身录音")

    @Parameter(title: "录音")
    var value: Bool

    @MainActor
    func perform() async throws -> some IntentResult {
        #if WIDGET_EXTENSION
        LifelogSwitch.setFromExtension(value)
        #else
        await LifelogRecorder.shared.turn(value)
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
        #if WIDGET_EXTENSION
        LifelogSwitch.setFromExtension(false)
        #else
        await LifelogRecorder.shared.turn(false)
        #endif
        return .result()
    }
}
