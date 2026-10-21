import Foundation

// What the timeline asks of the brain when he confirms, names or says something of a row, in one place: the app's is the brain this phone is paired with (`live`), and a test or a debug
// scene gives another, so that what the screen does with each answer can be seen without a brain. A closure never throws: what could not be asked or understood is `failed` or `refused`.

struct TLBrain {
    var commit: @MainActor (_ hex: String, _ action: CommitAction) async -> CommitAnswer
    var name: @MainActor (_ name: String, _ lat: Double, _ lng: Double, _ role: String) async -> PlaceAdd
    var answer: @MainActor (_ day: String, _ startMs: Int64, _ endMs: Int64, _ text: String) async -> SpanAnswer
    /// his answer to what the model asked of a span it served: one of its options, or words for it (his yes to the words it already has)
    var choose: @MainActor (_ day: String, _ spanId: String, _ choice: String?, _ text: String?) async -> SpanAnswer
    /// 理一下: the brain tidies today (it answers when a model is done, up to about a minute)
    var tidy: @MainActor () async -> TidyAnswer
    /// the days are asked for again
    var refetch: @MainActor () -> Void

    static let live = TLBrain(
        commit: { hex, action in
            do { return try await BrainLink.shared.call { try await BrainClient.actOnCommitment(hex: hex, action: action, bases: $0, token: $1) } } catch { return .failed }
        },
        name: { name, lat, lng, role in
            do { return try await BrainLink.shared.call { try await BrainClient.addPlace(name: name, lat: lat, lng: lng, role: role, bases: $0, token: $1) } }
            catch let e as BrainError { return .refused(e.message) }
            catch { return .refused(BrainError.refused.message) }
        },
        answer: { day, startMs, endMs, text in
            do { return try await BrainLink.shared.call { try await BrainClient.stateSpan(day: day, startMs: startMs, endMs: endMs, text: text, bases: $0, token: $1) } } catch { return .failed }
        },
        choose: { day, spanId, choice, text in
            do { return try await BrainLink.shared.call { try await BrainClient.answerSpan(day: day, spanId: spanId, choice: choice, text: text, bases: $0, token: $1) } } catch { return .failed }
        },
        tidy: {
            do { return try await BrainLink.shared.call { try await BrainClient.tidyDay(bases: $0, token: $1) } } catch { return .failed }
        },
        refetch: { BrainLink.shared.opened() })
}
