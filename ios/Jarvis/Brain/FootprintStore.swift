import Foundation
import Observation

/// What the brain said to a day's footprint: the reply, or that it has no such route (an older brain).
enum FootprintAnswer: Equatable {
    case reply(FootprintReply)
    case missing
}

/// The footprints the brain served, by day (`YYYY-MM-DD`, as `Cal.cur.iso` writes it). A day is asked for when a page wants it and again once the app has been opened since
/// (`BrainLink.openTick`), keeping what it had on show until the new answer comes.
@Observable @MainActor
final class FootprintStore {
    static let shared = FootprintStore()
    private var replies: [String: FootprintReply] = [:]
    private var absent: Set<String> = []
    @ObservationIgnored private var tickOf: [String: Int] = [:]
    @ObservationIgnored private var inFlight: Set<String> = []
    /// the brain's answer for a day, or nil when it could not be asked (it is tried again at the next `ensure`); a test hands in its own
    @ObservationIgnored var fetch: (String) async -> FootprintAnswer? = { day in
        guard let r = try? await BrainLink.shared.call({ try await BrainClient.footprint(day, bases: $0, token: $1) }) else { return nil }
        return r.reply.map { .reply($0) } ?? .missing
    }
    #if DEBUG
    /// made-up days for the simulator (`FootprintFixture`), read before anything the brain served
    var fixture: [String: FootprintReply]?
    #endif

    func reply(_ iso: String) -> FootprintReply? {
        #if DEBUG
        if let f = fixture?[iso] { return f }
        #endif
        return replies[iso]
    }

    /// The brain answered that it has no footprint route.
    func isMissing(_ iso: String) -> Bool { absent.contains(iso) }

    /// Ask for a day unless it is on show, asked since the app was opened, or on its way.
    func ensure(_ iso: String) {
        #if DEBUG
        if fixture?[iso] != nil { return }
        #endif
        let tick = BrainLink.shared.openTick
        if inFlight.contains(iso) || tickOf[iso] == tick { return }
        inFlight.insert(iso)
        Task {
            let answer = await fetch(iso)
            inFlight.remove(iso)
            switch answer {
            case .reply(let r): replies[iso] = r; absent.remove(iso); tickOf[iso] = tick
            case .missing: absent.insert(iso); tickOf[iso] = tick
            case nil: break
            }
        }
    }
}
