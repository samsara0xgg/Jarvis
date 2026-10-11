import ActivityKit
import SwiftUI
import WidgetKit

// Her state and the next reminder, on the lock screen and in the Dynamic Island, in the app's night sky. The content comes from the brain's pushes
// (JarvisActivityAttributes); a tap opens the app as it is (he may have come to type), and only 「轻点说话」 opens jarvis://talk, which starts listening.

enum Night {
    static let bg = Color(red: 14 / 255, green: 15 / 255, blue: 30 / 255)
    static let ink = Color(red: 238 / 255, green: 240 / 255, blue: 251 / 255)
    static let muted = Color(red: 196 / 255, green: 204 / 255, blue: 238 / 255).opacity(0.62)
    static let pale = Color(red: 238 / 255, green: 241 / 255, blue: 1)
    static let amber = Color(red: 1, green: 217 / 255, blue: 176 / 255)
    static let talk = URL(string: "jarvis://talk")!
}

// the app's faces, bundled in this extension too; the system's own when they did not load
func serif(_ size: CGFloat) -> Font {
    UIFont(name: "Instrument Serif", size: size) != nil ? .custom("Instrument Serif", size: size) : .system(size: size, design: .serif)
}
func mono(_ size: CGFloat) -> Font {
    UIFont(name: "IBMPlexMono-Medium", size: size) != nil ? .custom("IBMPlexMono-Medium", size: size) : .system(size: size, weight: .medium, design: .monospaced)
}

private typealias State = JarvisActivityAttributes.ContentState

/// Her face as a small orb: eyes open and tall while she talks, resting while idle, shut when she is quiet.
private struct Orb: View {
    let state: State
    let size: CGFloat

    private var eyeHeight: CGFloat {
        switch state.state {
        case "conversation": size * 0.3
        case "idle": size * 0.24
        default: size * 0.05
        }
    }

    var body: some View {
        ZStack {
            Circle().fill(RadialGradient(colors: [Color(red: 52 / 255, green: 56 / 255, blue: 104 / 255), Color(red: 18 / 255, green: 19 / 255, blue: 38 / 255)],
                                         center: .topLeading, startRadius: 0, endRadius: size))
            Circle().stroke(state.state == "conversation" ? Night.amber.opacity(0.9) : Night.pale.opacity(0.22), lineWidth: max(1, size * 0.04))
            HStack(spacing: size * 0.17) {
                Capsule().fill(Night.pale).frame(width: size * 0.1, height: eyeHeight)
                Capsule().fill(Night.pale).frame(width: size * 0.1, height: eyeHeight)
            }
        }
        .frame(width: size, height: size)
    }
}

/// the reminder's time as the app writes times, 24-hour "14:30", whatever the phone's locale
private let clock: DateFormatter = {
    let f = DateFormatter()
    f.locale = Locale(identifier: "en_US_POSIX"); f.dateFormat = "HH:mm"
    return f
}()

private struct NextTime: View {
    let at: Date
    let size: CGFloat
    var body: some View { Text(clock.string(from: at)).font(mono(size)).foregroundStyle(Night.amber).lineLimit(1).fixedSize() }
}

private struct LockScreen: View {
    let state: State

    var body: some View {
        HStack(spacing: 14) {
            Orb(state: state, size: 46)
            VStack(alignment: .leading, spacing: 4) {
                Text(state.stateWord).font(serif(26)).foregroundStyle(Night.ink)
                if let at = state.nextAt {
                    HStack(spacing: 8) {
                        NextTime(at: at, size: 14)
                        if let text = state.nextText { Text(text).font(.system(size: 14)).foregroundStyle(Night.ink.opacity(0.84)).lineLimit(1) }
                    }
                } else {
                    Text("没有等着的提醒").font(.system(size: 13)).foregroundStyle(Night.muted)
                }
            }
            Spacer(minLength: 8)
            Image(systemName: "waveform").font(.system(size: 17, weight: .medium)).foregroundStyle(Night.pale)
                .frame(width: 40, height: 40).background(Color.white.opacity(0.1), in: Circle())
        }
        .padding(.horizontal, 18).padding(.vertical, 14)
    }
}

struct JarvisLiveActivity: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: JarvisActivityAttributes.self) { context in
            LockScreen(state: context.state)
                .activityBackgroundTint(Night.bg)
                .activitySystemActionForegroundColor(Night.ink)
        } dynamicIsland: { context in
            let s = context.state
            return DynamicIsland {
                DynamicIslandExpandedRegion(.leading) { Orb(state: s, size: 40).padding(.leading, 4) }
                DynamicIslandExpandedRegion(.center) { Text(s.stateWord).font(serif(24)).foregroundStyle(Night.ink) }
                DynamicIslandExpandedRegion(.trailing) {
                    if let at = s.nextAt { NextTime(at: at, size: 18) }
                }
                DynamicIslandExpandedRegion(.bottom) {
                    HStack {
                        Text(s.nextText ?? "没有等着的提醒").font(.system(size: 14)).foregroundStyle(s.nextText == nil ? Night.muted : Night.ink.opacity(0.84)).lineLimit(1)
                        Spacer(minLength: 8)
                        Link(destination: Night.talk) {
                            Label("轻点说话", systemImage: "waveform").font(.system(size: 13, weight: .medium)).foregroundStyle(Night.pale)
                                .padding(.horizontal, 12).padding(.vertical, 6).background(Color.white.opacity(0.1), in: Capsule())
                        }
                    }
                }
            } compactLeading: {
                Orb(state: s, size: 22)
            } compactTrailing: {
                if let at = s.nextAt { NextTime(at: at, size: 12) }
            } minimal: {
                Orb(state: s, size: 22)
            }
            .keylineTint(Night.amber)
        }
    }
}
