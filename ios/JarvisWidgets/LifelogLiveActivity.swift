import ActivityKit
import AppIntents
import SwiftUI
import WidgetKit

// 正在录音 on the lock screen and in the Dynamic Island: a red dot, today's total counting up by itself, and a 暂停 button
// (LifelogPauseIntent, run in the app). Same night palette and faces as her own activity.

private let recRed = Color(red: 1, green: 92 / 255, blue: 92 / 255)

private struct RecDot: View {
    var size: CGFloat = 9
    var body: some View { Circle().fill(recRed).frame(width: size, height: size) }
}

/// Today's total as a running timer; the far end is two days out so it never stops on its own.
private struct Total: View {
    let since: Date
    let size: CGFloat
    var body: some View {
        Text(timerInterval: since...since.addingTimeInterval(172_800), pauseTime: nil, countsDown: false, showsHours: true)
            .font(mono(size)).monospacedDigit().foregroundStyle(Night.ink).multilineTextAlignment(.trailing).lineLimit(1)
    }
}

private struct PauseButton: View {
    var body: some View {
        Button(intent: LifelogPauseIntent()) {
            Label("暂停", systemImage: "pause.fill").font(.system(size: 14, weight: .medium)).foregroundStyle(Night.pale)
                .padding(.horizontal, 14).padding(.vertical, 8).background(Color.white.opacity(0.1), in: Capsule())
        }
        .buttonStyle(.plain)
    }
}

private struct RecLockScreen: View {
    let since: Date
    var body: some View {
        HStack(spacing: 14) {
            RecDot(size: 12)
            VStack(alignment: .leading, spacing: 2) {
                Text("正在录音").font(serif(26)).foregroundStyle(Night.ink)
                HStack(spacing: 6) {
                    Text("今天").font(.system(size: 13)).foregroundStyle(Night.muted)
                    Total(since: since, size: 15)
                }
            }
            Spacer(minLength: 8)
            PauseButton()
        }
        .padding(.horizontal, 18).padding(.vertical, 14)
    }
}

struct LifelogLiveActivity: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: LifelogActivityAttributes.self) { context in
            RecLockScreen(since: context.state.since)
                .activityBackgroundTint(Night.bg)
                .activitySystemActionForegroundColor(Night.ink)
        } dynamicIsland: { context in
            let since = context.state.since
            return DynamicIsland {
                DynamicIslandExpandedRegion(.leading) { RecDot(size: 12).padding(.leading, 8) }
                DynamicIslandExpandedRegion(.center) { Text("正在录音").font(serif(24)).foregroundStyle(Night.ink) }
                DynamicIslandExpandedRegion(.trailing) { Total(since: since, size: 16).frame(maxWidth: 80) }
                DynamicIslandExpandedRegion(.bottom) {
                    HStack {
                        Text("今天已录").font(.system(size: 13)).foregroundStyle(Night.muted)
                        Spacer(minLength: 8)
                        PauseButton()
                    }
                }
            } compactLeading: {
                RecDot()
            } compactTrailing: {
                Total(since: since, size: 12).frame(maxWidth: 52)
            } minimal: {
                RecDot()
            }
            .keylineTint(recRed)
        }
    }
}

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
