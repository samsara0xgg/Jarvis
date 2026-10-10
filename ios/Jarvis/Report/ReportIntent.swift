import AppIntents

/// What a Shortcuts automation can tell Jarvis. iOS has no public way for an app to see an alarm, and Focus
/// and the charger are only seen when the app wakes; an automation ("When the alarm is stopped", "When Do Not
/// Disturb turns on", "When the charger connects") that runs this action says it the moment it happens, and
/// wakes the app for it. The user adds the automation in Shortcuts, once per trigger.
enum PhoneSignalChange: String, AppEnum {
    case focusOn, focusOff, alarmStopped, alarmSnoozed, chargerConnected, chargerDisconnected

    static let typeDisplayRepresentation: TypeDisplayRepresentation = "发生了什么"
    static let caseDisplayRepresentations: [PhoneSignalChange: DisplayRepresentation] = [
        .focusOn: "专注模式开了",
        .focusOff: "专注模式关了",
        .alarmStopped: "闹钟关掉了",
        .alarmSnoozed: "闹钟推迟了",
        .chargerConnected: "插上了充电器",
        .chargerDisconnected: "拔掉了充电器",
    ]

    var change: StateChange {
        switch self {
        case .focusOn: .focus(on: true)
        case .focusOff: .focus(on: false)
        case .alarmStopped: .alarm(snoozed: false)
        case .alarmSnoozed: .alarm(snoozed: true)
        case .chargerConnected: .power(connected: true)
        case .chargerDisconnected: .power(connected: false)
        }
    }
}

struct TellJarvisIntent: AppIntent {
    static let title: LocalizedStringResource = "告诉 Jarvis 发生了什么"
    static let description = IntentDescription("给「快捷指令」的自动化用：专注模式、闹钟、充电器有变化时，让 Jarvis 知道。")
    static let openAppWhenRun = false

    @Parameter(title: "发生了什么") var what: PhoneSignalChange

    @MainActor
    func perform() async throws -> some IntentResult {
        await Reporter.shared.tell(what.change)
        return .result()
    }
}
