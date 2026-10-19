import Foundation
@testable import Jarvis

/// The brain the timeline asks (`TLBrain`), as far as a test cares: what it was asked of each kind, what it answers, and (with `hold`) when. One stub serves marks and commitments.
@MainActor
final class TLStubBrain {
    var marked: [(day: String, id: String, on: Bool)] = []
    var committed: [(hex: String, action: CommitAction)] = []
    var markAnswer: HighlightAnswer = .done(nil)
    var commitAnswer: CommitAnswer = .done
    var refetches = 0
    /// when set, an answer waits for `release()`
    var hold = false
    private var waiting: CheckedContinuation<Void, Never>?

    var brain: TLBrain {
        TLBrain(
            mark: { [self] day, id, on in marked.append((day, id, on)); await pause(); return markAnswer },
            commit: { [self] hex, action in committed.append((hex, action)); await pause(); return commitAnswer },
            refetch: { [self] in refetches += 1 })
    }

    private func pause() async { if hold { await withCheckedContinuation { waiting = $0 } } }
    func release() { waiting?.resume(); waiting = nil }
}
