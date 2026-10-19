import Foundation

// What the timeline asks of the brain when he marks, confirms or names something on a row, in one place: the app's is the brain this phone is paired with (`live`), and a test or a debug
// scene gives another, so that what the screen does with each answer can be seen without a brain. A closure never throws: what could not be asked or understood is `failed`.

struct TLBrain {
    var mark: @MainActor (_ day: String, _ id: String, _ on: Bool) async -> HighlightAnswer
    /// the days are asked for again
    var refetch: @MainActor () -> Void

    static let live = TLBrain(
        mark: { day, id, on in
            do { return try await BrainLink.shared.call { try await BrainClient.markItem(day: day, id: id, on: on, bases: $0, token: $1) } } catch { return .failed }
        },
        refetch: { BrainLink.shared.opened() })
}
