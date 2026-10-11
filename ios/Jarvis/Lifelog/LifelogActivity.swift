import ActivityKit
import Foundation
import WidgetKit

/// The recording's Live Activity (the Dynamic Island mark): started, updated and ended to match the recorder.
/// The total on it counts up by itself from `since` (LifelogActivityAttributes); it is updated only when that start date moves (a piece closes, midnight).
@MainActor
enum LifelogActivity {
    /// Brings the Live Activity in line with `LifelogRecorder.shared` (recording or not, today's total). Called by the recorder on every change.
    static func sync() {
        ControlCenter.shared.reloadControls(ofKind: LifelogSwitch.controlKind)      // the control shows the switch as it is now
        let all = ActivityKit.Activity<LifelogActivityAttributes>.activities
        let recorder = LifelogRecorder.shared
        guard recorder.recording else {
            for a in all { Task { await a.end(nil, dismissalPolicy: .immediate) } }
            return
        }
        let state = LifelogActivityAttributes.ContentState(since: since(now: Date(), pieceStartedAt: recorder.pieceStartedAt))
        if let a = all.first(where: { $0.activityState == .active }) {
            if abs(a.content.state.since.timeIntervalSince(state.since)) > 2 { Task { await a.update(.init(state: state, staleDate: nil)) } }
            for extra in all where extra.id != a.id { Task { await extra.end(nil, dismissalPolicy: .immediate) } }
        } else if ActivityAuthorizationInfo().areActivitiesEnabled {
            do {
                _ = try ActivityKit.Activity.request(attributes: LifelogActivityAttributes(), content: .init(state: state, staleDate: nil), pushType: nil)
            } catch {
                // iOS ends an activity after about 8 hours and only lets the app start one from the foreground or an intent
                LifelogUploader.log("live activity could not start (\(type(of: error)))")
            }
        }
    }

    /// The date for which `now - since` is today's closed total plus the piece being written.
    nonisolated static func since(now: Date, pieceStartedAt: Date?) -> Date {
        let open = pieceStartedAt.map { max(0, now.timeIntervalSince($0)) } ?? 0
        return now.addingTimeInterval(-(Double(LifelogStore.closedTodayMs) / 1000 + open))
    }
}
