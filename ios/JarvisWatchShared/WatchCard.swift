import Foundation

/// A card the watch lays over her (跨端UI标准 §12): her answer, something that waits on him, or 该出门了. One at a time; done or put away, she is whole again.
/// Here, not in the watch app, so the phone's tests reach it (as `WatchRing`).
enum WatchCard: Equatable {
    case answer(spoken: String, written: String, action: WatchDay.Waiting?)
    case waiting(WatchDay.Waiting)
    case leave(Date)

    /// 该出门了 comes up this long before `leaveAt` and stays this long after it
    // ponytail: fixed window; the day carries no "show it now" of its own, add one to WatchDay if she should choose
    static let leaveBefore: TimeInterval = 600
    static let leaveAfter: TimeInterval = 300

    /// The card on top: a talk's answer over all; then the first of `queue` (what he opened, then what came in) that still waits;
    /// then 该出门了 near its time, unless he put that one away.
    static func top(answer: WatchCard?, day: WatchDay?, queue: [String], answered: Set<String>, leaveDismissed: Date?, now: Date) -> WatchCard? {
        if let answer { return answer }
        guard let day else { return nil }
        for id in queue where !answered.contains(id) {
            if let w = day.waiting.first(where: { $0.id == id }) { return .waiting(w) }
        }
        if let t = day.leaveAt, t != leaveDismissed, now >= t - leaveBefore, now <= t + leaveAfter { return .leave(t) }
        return nil
    }
}
