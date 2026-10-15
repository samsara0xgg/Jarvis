import ActivityKit
import Foundation

/// The ride Live Activity is started and updated by the brain's pushes; the app only tells the brain how to reach it: the push-to-start token
/// (iOS issues it again at each launch) and, once an activity runs, its own update token. Watching starts at every launch, background ones too:
/// a push-to-start wakes the app to hear the new activity's token.
@MainActor
// the app has its own `Activity` (the reported motion), so ActivityKit's is named in full
final class RideLiveActivity {
    static let shared = RideLiveActivity()
    private var watching: Set<String> = []

    func observe() {
        Task { for await token in ActivityKit.Activity<RideAttributes>.pushToStartTokenUpdates { PushRegistry.shared.setRideStartToken(.set(PushWire.hex(token))) } }
        Task { for await a in ActivityKit.Activity<RideAttributes>.activityUpdates { watch(a) } }
        for a in ActivityKit.Activity<RideAttributes>.activities { watch(a) }
    }

    /// Unpairing ends the ride's activity too.
    func end() async {
        for a in ActivityKit.Activity<RideAttributes>.activities { await a.end(nil, dismissalPolicy: .immediate) }
    }

    private func watch(_ a: ActivityKit.Activity<RideAttributes>) {
        guard watching.insert(a.id).inserted else { return }
        Task { for await token in a.pushTokenUpdates { PushRegistry.shared.setRideActivityToken(.set(PushWire.hex(token))) } }
        Task {
            for await s in a.activityStateUpdates where s == .ended || s == .dismissed {
                watching.remove(a.id)
                PushRegistry.shared.setRideActivityToken(.clear)
                break
            }
        }
    }

    #if DEBUG
    /// `-rideActivity riding|arriving|timetable|nolive` shows a ride on the lock screen without the brain (screenshots of the layouts)
    func showDebug() {
        guard let name = UserDefaults.standard.string(forKey: "rideActivity"), ActivityAuthorizationInfo().areActivitiesEnabled else { return }
        let live = name != "nolive"
        let state = RideAttributes.ContentState(route: "32", nextStop: name == "arriving" ? "国贸" : "光华路", stopsLeft: name == "arriving" ? 1 : 4, minutesToAlight: name == "arriving" ? 1 : 6,
                                                alightStop: "国贸", positionLive: live, arrivalLive: name == "riding" || name == "arriving", stale: nil, phase: name == "arriving" ? "arriving" : "riding")
        let attributes = RideAttributes(rideId: "Rdebug01", route: "32", boardStop: "西直门", startedAtMs: Int64(Date().timeIntervalSince1970 * 1000))
        Task {
            await end()
            do { _ = try ActivityKit.Activity.request(attributes: attributes, content: .init(state: state, staleDate: nil), pushType: nil) } catch {
                NSLog("ride activity: could not start (%@)", String(describing: type(of: error)))
            }
        }
    }
    #else
    func showDebug() {}
    #endif
}
