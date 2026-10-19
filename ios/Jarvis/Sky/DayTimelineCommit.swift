import SwiftUI

// What he says of an event his mail made (ADR 0274, 0296): it is his (是我的) or it is not (不是). Either is on the row at once, the brain is told (`POST /inherent/commitments/{hex}`), and the
// days are asked for again. When he says it is his, the amber 等你 fades, the count of what waits goes down and the line says that he confirmed it; when he says it is not, the row collapses out
// and the bar's dashed box fades. When the brain will not, the row is as it was, with a warning in the hand and a line under it.

extension TimelineState {
    /// how long what he said of an event shows as the day's before the brain's own answer is the one that does
    static let commitHolds = 120.0
    static let commitNote = "没成，稍后再试"

    /// 是我的
    func confirm(_ e: TLEntry) { settle(e, .confirm) }
    /// 不是
    func deny(_ e: TLEntry) { settle(e, .cancel) }

    /// he has said it already, and the time for the brain's answer to say the same is not up
    func saidOf(_ id: String, at now: Date = Date()) -> Bool {
        (confirmed[id] ?? .distantPast) > now || (dropped[id] ?? .distantPast) > now
    }

    private func settle(_ e: TLEntry, _ action: CommitAction) {
        guard let hex = e.commitment, !saidOf(e.id) else { return }
        notes[e.id] = nil
        let change = TLMotion.listChange(reduce: TLMotion.reduced)
        switch action {
        case .confirm:
            withAnimation(change) { confirmed[e.id] = Date().addingTimeInterval(Self.commitHolds) }
            Haptic.notify(.success)
        case .cancel:
            withAnimation(change) { dropped[e.id] = Date().addingTimeInterval(Self.commitHolds) }
            Haptic.impact(.light)
        }
        Task { @MainActor [self] in
            switch await brain.commit(hex, action) {
            case .done, .already:
                brain.refetch()                                 // confirmed already is what he said: the day says it too
            case .gone:
                // the brain has no such commitment (dropped on the Mac, or by mail): the row is not one any more, whatever he said of it
                withAnimation(change) { confirmed[e.id] = nil; dropped[e.id] = Date().addingTimeInterval(Self.commitHolds) }
                brain.refetch()
            case .failed:
                withAnimation(change) { confirmed[e.id] = nil; dropped[e.id] = nil }
                Haptic.notify(.warning)
                say(Self.commitNote, on: e.id)
            }
        }
    }
}
