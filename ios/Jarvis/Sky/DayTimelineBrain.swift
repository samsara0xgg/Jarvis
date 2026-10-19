import Foundation

// What the timeline asks of the brain when he marks, confirms or names something on a row, in one place: the app's is the brain this phone is paired with (`live`), and a test or a debug
// scene gives another, so that what the screen does with each answer can be seen without a brain. A closure never throws: what could not be asked or understood is `failed` or `refused`.

struct TLBrain {
    var mark: @MainActor (_ day: String, _ id: String, _ on: Bool) async -> HighlightAnswer
    var commit: @MainActor (_ hex: String, _ action: CommitAction) async -> CommitAnswer
    var name: @MainActor (_ name: String, _ lat: Double, _ lng: Double, _ role: String) async -> PlaceAdd
    /// the days are asked for again
    var refetch: @MainActor () -> Void

    static let live = TLBrain(
        mark: { day, id, on in
            do { return try await BrainLink.shared.call { try await BrainClient.markItem(day: day, id: id, on: on, bases: $0, token: $1) } } catch { return .failed }
        },
        commit: { hex, action in
            do { return try await BrainLink.shared.call { try await BrainClient.actOnCommitment(hex: hex, action: action, bases: $0, token: $1) } } catch { return .failed }
        },
        name: { name, lat, lng, role in
            do { return try await BrainLink.shared.call { try await BrainClient.addPlace(name: name, lat: lat, lng: lng, role: role, bases: $0, token: $1) } }
            catch let e as BrainError { return .refused(e.message) }
            catch { return .refused(BrainError.refused.message) }
        },
        refetch: { BrainLink.shared.opened() })
}
