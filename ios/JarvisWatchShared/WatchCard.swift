import Foundation

/// A card the watch lays over her (跨端UI标准 §12): her answer, something that waits on him, 该出门了, 早 (how he slept, the first look of the morning)
/// or a stretch she is not sure of; and 标一下's answer. One at a time; done or put away, she is whole again.
/// Here, not in the watch app, so the phone's tests reach it (as `WatchRing`).
enum WatchCard: Equatable {
    case answer(spoken: String, written: String, action: WatchDay.Waiting?)
    case waiting(WatchDay.Waiting)
    case leave(Date)
    case morning(slept: Int, things: Int, first: String?)
    case unsure(WatchDay.Unsure)
    case marked(id: String, day: String, when: String, title: String)   // 标一下 took
    case markMissed(String)                                              // it did not, and why

    /// 该出门了 comes up this long before `leaveAt` and stays this long after it
    // ponytail: fixed window; the day carries no "show it now" of its own, add one to WatchDay if she should choose
    static let leaveBefore: TimeInterval = 600
    static let leaveAfter: TimeInterval = 300
    /// 早 comes up until this hour, once a day
    static var morningUntil = 11                        // the `morning` debug scene moves it to the day's end

    /// The card on top: a talk's answer over all; then 标一下's answer (`mark`), which he just asked for; then the first of `queue` (what he opened, then what came in) that still waits;
    /// then 该出门了 near its time, unless he put that one away; then 早 in the morning, until he has seen it (`morningSeen`, the day it was seen);
    /// then the stretch she is not sure of, unless he answered it or put it away.
    static func top(answer: WatchCard?, mark: WatchCard? = nil, day: WatchDay?, queue: [String], answered: Set<String>, leaveDismissed: Date?, now: Date,
                    morningSeen: String? = nil, putAway: Set<String> = []) -> WatchCard? {
        if let answer { return answer }
        if let mark { return mark }
        guard let day else { return nil }
        for id in queue where !answered.contains(id) {
            if let w = day.waiting.first(where: { $0.id == id }) { return .waiting(w) }
        }
        if let t = day.leaveAt, t != leaveDismissed, now >= t - leaveBefore, now <= t + leaveAfter { return .leave(t) }
        if let m = day.slept, morningSeen != day.date, Calendar.current.component(.hour, from: now) < morningUntil {
            return .morning(slept: m, things: day.next.count, first: day.next.isEmpty ? nil : day.headline?.title)
        }
        if let u = day.unsure, !putAway.contains(u.id) { return .unsure(u) }
        return nil
    }
}
