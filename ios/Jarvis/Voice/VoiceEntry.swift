import AppIntents
import SwiftUI

// Ways in that need no screen: Siri, Shortcuts, the Action button (the intent), the lock-screen widget (jarvis://talk).

struct JarvisShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: TalkIntent(),
            phrases: ["跟 \(.applicationName) 说话", "和 \(.applicationName) 说话", "Talk to \(.applicationName)", "Hey \(.applicationName)"],
            shortTitle: "跟 Jarvis 说话", systemImageName: "waveform")
    }
}

extension View {
    /// Attach once to the root view: the talk deep link, and (DEBUG) `-voiceTest` to start a session at launch.
    func voiceEntry() -> some View {
        modifier(VoiceEntry())
    }
}

private struct VoiceEntry: ViewModifier {
    func body(content: Content) -> some View {
        content
            .onOpenURL { url in
                if url.scheme == "jarvis", url.host == "talk" { VoiceSession.shared.requestTalk() }
            }
            .task {
                let session = VoiceSession.shared    // created here so an intent that fired during launch is picked up
                session.endpoint = { BrainLink.shared.voiceEndpoint }    // nil until the phone is paired: the local stub answers
                if UserDefaults.standard.bool(forKey: SettingsModel.wakeKey) { session.wakeWordInApp = true }
                #if DEBUG
                if ProcessInfo.processInfo.arguments.contains("-voiceTest") { session.start() }
                #endif
            }
    }
}
