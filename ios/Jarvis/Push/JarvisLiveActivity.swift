import ActivityKit
import Foundation

/// The Live Activity while the phone is paired: started from the app (iOS only lets the foreground start one), its push token told to the brain
/// each time it changes and cleared when the activity ends. What it shows afterwards comes from the brain's pushes.
@MainActor
// the app has its own `Activity` (the reported motion), so ActivityKit's is named in full
final class JarvisActivity {
    static let shared = JarvisActivity()
    private var watching: Set<String> = []

    /// One activity runs: the one still running after a relaunch is picked up, otherwise one starts. iOS ends an activity after about 8 hours, so the next foreground starts another.
    func start() {
        guard BrainLink.shared.hasToken, ActivityAuthorizationInfo().areActivitiesEnabled else { return }
        let running = ActivityKit.Activity<JarvisActivityAttributes>.activities.first { $0.activityState == .active }
        if let a = running ?? request() { watch(a); showDebugState(on: a) }
    }

    func end() async {
        for a in ActivityKit.Activity<JarvisActivityAttributes>.activities { await a.end(nil, dismissalPolicy: .immediate) }
    }

    private func request() -> ActivityKit.Activity<JarvisActivityAttributes>? {
        do {
            return try ActivityKit.Activity.request(attributes: JarvisActivityAttributes(), content: .init(state: debugState ?? .idle, staleDate: nil), pushType: .token)
        } catch {
            NSLog("activity: could not start (%@)", String(describing: type(of: error)))
            return nil
        }
    }

    private func watch(_ a: ActivityKit.Activity<JarvisActivityAttributes>) {
        guard watching.insert(a.id).inserted else { return }
        Task { for await token in a.pushTokenUpdates { PushRegistry.shared.setActivityToken(.set(PushWire.hex(token))) } }
        Task {
            for await s in a.activityStateUpdates where s == .ended || s == .dismissed {
                watching.remove(a.id)
                PushRegistry.shared.setActivityToken(.clear)
                break
            }
        }
    }

    #if DEBUG
    private func showDebugState(on a: ActivityKit.Activity<JarvisActivityAttributes>) {
        if let s = debugState { Task { await a.update(.init(state: s, staleDate: nil)) } }
    }
    /// `-activityState conversation -activityNext 取快递 -activityIn 40` shows a content-state without the brain (screenshots of the layouts)
    private var debugState: JarvisActivityAttributes.ContentState? {
        let d = UserDefaults.standard
        guard let state = d.string(forKey: "activityState") else { return nil }
        let text = d.string(forKey: "activityNext")
        return .init(state: state, nextText: text, nextAtMs: text == nil ? nil : Int64((Date().timeIntervalSince1970 + max(d.double(forKey: "activityIn"), 1) * 60) * 1000))
    }
    #else
    private func showDebugState(on a: ActivityKit.Activity<JarvisActivityAttributes>) {}
    private var debugState: JarvisActivityAttributes.ContentState? { nil }
    #endif
}
