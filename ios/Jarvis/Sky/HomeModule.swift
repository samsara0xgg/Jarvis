import Foundation
import Observation

/// What the middle of the home screen shows: the whole day as a timeline (Sky/DayTimeline.swift), or the star dial. The phone always shows the timeline; the dial is kept in code, and a
/// debug run asks for it with the launch argument `-homeModule stars`. Everything that depends on it reads `kind` where it draws, so a change applies at once, with the motion between the two.
@Observable
final class HomeModule {
    enum Kind: String { case stars, timeline }

    static let shared = HomeModule()

    var kind: Kind

    init() {
        kind = .timeline
        #if DEBUG
        let a = ProcessInfo.processInfo.arguments
        if let i = a.firstIndex(of: "-homeModule"), i + 1 < a.count, a[i + 1] == Kind.stars.rawValue { kind = .stars }
        #endif
    }

    var isTimeline: Bool { kind == .timeline }
}
