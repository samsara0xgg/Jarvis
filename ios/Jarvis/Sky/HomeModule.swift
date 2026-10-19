import Foundation
import Observation

/// What the middle of the home screen shows: the star dial, or the whole day as a timeline (Sky/DayTimeline.swift). He picks it in Settings, 首页 › 中间显示.
/// Everything that depends on it reads `kind` where it draws, so a change applies at once, with the motion between the two.
@Observable
final class HomeModule {
    enum Kind: String { case stars, timeline }

    static let shared = HomeModule(.standard)
    /// the key in UserDefaults (also the launch argument `-homeModule stars` in a debug run)
    static let key = "homeModule"

    @ObservationIgnored private let defaults: UserDefaults

    /// 时间线 until he chooses 星盘; an unknown stored word is the default as well
    var kind: Kind {
        didSet { if kind != oldValue { defaults.set(kind.rawValue, forKey: Self.key) } }
    }

    init(_ defaults: UserDefaults) {
        self.defaults = defaults
        kind = defaults.string(forKey: Self.key).flatMap(Kind.init(rawValue:)) ?? .timeline
    }

    var isTimeline: Bool { kind == .timeline }
}
