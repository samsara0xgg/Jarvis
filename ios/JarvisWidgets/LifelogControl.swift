import AppIntents
import SwiftUI
import WidgetKit

/// Control Center, the lock screen's controls and the Action button: 随身录音 on or off, read from the switch in the App Group.
struct LifelogControl: ControlWidget {
    var body: some ControlWidgetConfiguration {
        StaticControlConfiguration(kind: LifelogSwitch.controlKind, provider: Provider()) { isOn in
            ControlWidgetToggle("随身录音", isOn: isOn, action: LifelogToggleIntent()) { on in
                Label(on ? "录音中" : "已关闭", systemImage: "record.circle")
            }
        }
        .displayName("随身录音")
        .description("整天录下周围的声音，只发给你自己的大脑")
    }

    struct Provider: ControlValueProvider {
        var previewValue: Bool { false }
        func currentValue() async throws -> Bool { LifelogStore.isOn }
    }
}
