import AppIntents
import Foundation
#if !WIDGET_EXTENSION
import UIKit
#endif

// Compiled into the app and the widget extension (the control names this intent). It is a `LiveActivityIntent`, so iOS runs it in the
// app's process, launched in the background if need be (with `AudioRecordingIntent` alone it ran in the extension whenever the app was
// not running). No Live Activity is ever started: he wants the recording out of sight. When iOS runs it in the extension anyway, it only
// sets the switch (`setFromExtension`).

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

/// The control's switch. Turning off works in the background. Turning on opens Jarvis first: iOS lets an app start the microphone from the
/// background only through `AudioRecordingIntent`, which requires a Live Activity for as long as it records.
struct LifelogToggleIntent: SetValueIntent, LiveActivityIntent {
    static let title: LocalizedStringResource = "随身录音"
    static let description = IntentDescription("开关随身录音")
    @available(iOS 26.0, *)
    static var supportedModes: IntentModes { [.background, .foreground(.dynamic)] }

    @Parameter(title: "录音")
    var value: Bool

    @MainActor
    func perform() async throws -> some IntentResult {
        #if WIDGET_EXTENSION
        LifelogSwitch.setFromExtension(value)
        #else
        if value, UIApplication.shared.applicationState != .active, #available(iOS 26.0, *) {
            try await continueInForeground(nil, alwaysConfirm: false)
        }
        await LifelogRecorder.shared.turn(value)
        #endif
        return .result()
    }
}
