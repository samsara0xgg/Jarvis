import AppIntents
import SwiftUI

// Ways in that need no screen: Siri, Shortcuts, the Action button (the intent), the lock-screen widget (jarvis://talk).

struct JarvisShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: TalkIntent(),
            phrases: ["跟 \(.applicationName) 说话", "和 \(.applicationName) 说话", "Talk to \(.applicationName)", "Hey \(.applicationName)"],
            shortTitle: "跟 Jarvis 说话", systemImageName: "waveform")
        AppShortcut(
            intent: AskIntent(),
            phrases: ["问 \(.applicationName)", "问问 \(.applicationName)", "Ask \(.applicationName)"],
            shortTitle: "问 Jarvis", systemImageName: "text.bubble")
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
                // `-askTest 明天几点出门` runs the Ask intent once at launch and logs what it would say and show
                let a = ProcessInfo.processInfo.arguments
                if let i = a.firstIndex(of: "-askTest"), i + 1 < a.count {
                    var intent = AskIntent(); intent.text = a[i + 1]
                    do { let r = try await intent.perform(); NSLog("askTest result: %@", String(describing: r)) }
                    catch let e as AskIntent.Failure { NSLog("askTest failure: %@", e.words) }
                    catch { NSLog("askTest error: %@", String(describing: error)) }
                }
                #endif
            }
    }
}
