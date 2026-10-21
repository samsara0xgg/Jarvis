import Foundation
import Observation

/// The phone's fixed words, zh and en side by side (ADR 0325): `Words.swift` and its `Words+<area>.swift` siblings hold every string a screen shows,
/// each entry built by `pick(zh:en:)` so a missing language does not compile, a sentence with values a function of them.
/// The language is the one the brain's settings name (`GET /inherent/language`), kept for the next launch; before the first answer it is the phone's own.
/// Screens read it inside `body`, so a change redraws them. Python's canary (`tests/canary/test_canary_cjk_only_in_swift_words_table.py`) keeps Chinese literals out of other files.
@Observable @MainActor
final class Words {
    static let shared = Words()

    enum Lang: String, Sendable {
        case zh, en

        /// the answer of `GET /inherent/language`: `{"language": "zh" | "en"}`, or nil when it is not one
        init?(reply data: Data) {
            guard let o = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any], let s = o["language"] as? String, let l = Lang(rawValue: s) else { return nil }
            self = l
        }
    }

    private(set) var language: Lang
    @ObservationIgnored private let store: UserDefaults
    private static let key = "words.language"

    /// `preferred` stands in for the phone's languages; a test gives it.
    init(store: UserDefaults = .standard, preferred: [String] = Locale.preferredLanguages) {
        self.store = store
        language = store.string(forKey: Self.key).flatMap(Lang.init(rawValue:)) ?? (preferred.first?.hasPrefix("zh") == true ? .zh : .en)
    }

    func set(_ l: Lang) {
        guard l != language else { return }
        language = l
        store.set(l.rawValue, forKey: Self.key)
    }

    /// Ask the brain for its language, when this phone is paired. Any failure keeps the language held.
    func refresh() async {
        guard BrainLink.shared.hasToken else { return }
        if let l = try? await BrainLink.shared.call({ try await BrainClient.language(bases: $0, token: $1) }) { set(l) }
    }

    /// the one choice between two wordings: both are always written
    static func pick(zh: String, en: String) -> String { shared.language == .zh ? zh : en }
}
