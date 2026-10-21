import Foundation
@testable import Jarvis

/// The brain the timeline asks (`TLBrain`), as far as a test cares: what it was asked of each kind, what it answers, and (with `hold`) when. One stub serves commitments, places and his answers.
@MainActor
final class TLStubBrain {
    var committed: [(hex: String, action: CommitAction)] = []
    var named: [(name: String, lat: Double, lng: Double, role: String)] = []
    var answered: [(day: String, startMs: Int64, endMs: Int64, text: String)] = []
    var chosen: [(day: String, spanId: String, choice: String?, text: String?)] = []
    var commitAnswer: CommitAnswer = .done
    var nameAnswer: PlaceAdd = .done(nil, change: nil)
    var spanAnswer: SpanAnswer = .done
    var tidyAnswer: TidyAnswer = .unchanged
    var tidies = 0
    var refetches = 0
    /// when set, an answer waits for `release()`
    var hold = false
    private var waiting: CheckedContinuation<Void, Never>?

    var brain: TLBrain {
        TLBrain(
            commit: { [self] hex, action in committed.append((hex, action)); await pause(); return commitAnswer },
            name: { [self] name, lat, lng, role in named.append((name, lat, lng, role)); await pause(); return nameAnswer },
            answer: { [self] day, startMs, endMs, text in answered.append((day, startMs, endMs, text)); await pause(); return spanAnswer },
            choose: { [self] day, spanId, choice, text in chosen.append((day, spanId, choice, text)); await pause(); return spanAnswer },
            tidy: { [self] in tidies += 1; await pause(); return tidyAnswer },
            refetch: { [self] in refetches += 1 })
    }

    private func pause() async { if hold { await withCheckedContinuation { waiting = $0 } } }
    func release() { waiting?.resume(); waiting = nil }
}
