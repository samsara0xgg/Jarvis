import Foundation
import Observation

/// The phone's fixed words, zh and en side by side (ADR 0405): `Words.swift` and its `Words+<area>.swift` siblings hold every string a screen shows,
/// each entry built by `pick(zh:en:)` so a missing language does not compile, a sentence with values a function of them.
/// The language is the device's own (ADR 0405): Chinese when its first language is, else English. The watch follows the phone's, which the day it is sent carries.
/// Screens read it inside `body`, so a change redraws them. Python's canary (`tests/canary/test_canary_cjk_only_in_swift_words_table.py`) keeps Chinese literals out of other files.
@Observable @MainActor
final class Words {
    static let shared = Words()

    enum Lang: String, Sendable {
        case zh, en
    }

    private(set) var language: Lang
    @ObservationIgnored private let store: UserDefaults
    private static let key = "words.language"

    /// `preferred` stands in for the device's languages; a test gives it. The watch starts in the phone's language it was last told (`set`), else its own.
    init(store: UserDefaults = .standard, preferred: [String] = Locale.preferredLanguages) {
        self.store = store
        let own: Lang = preferred.first?.hasPrefix("zh") == true ? .zh : .en
        #if os(watchOS)
        language = store.string(forKey: Self.key).flatMap(Lang.init(rawValue:)) ?? own
        #else
        let argument = UserDefaults.standard.volatileDomain(forName: UserDefaults.argumentDomain)[Self.key] as? String          // `-words.language zh` at launch (the UI tests' shots)
        language = argument.flatMap(Lang.init(rawValue:)) ?? own
        #endif
    }

    func set(_ l: Lang) {
        guard l != language else { return }
        language = l
        store.set(l.rawValue, forKey: Self.key)
    }

    /// the one choice between two wordings: both are always written
    static func pick(zh: String, en: String) -> String { shared.language == .zh ? zh : en }

    /// a day as the phone writes it: 「10月23日 周五」, "Fri, Oct 23" (the iPad's `Words.Pad.date` writes the same), on `cal`'s days
    static func date(_ d: Date, in cal: Calendar = .current) -> String {
        let c = cal.dateComponents([.month, .day, .weekday], from: d)
        var en = Date.FormatStyle.dateTime.weekday(.abbreviated).month(.abbreviated).day().locale(Locale(identifier: "en_US"))
        en.calendar = cal; en.timeZone = cal.timeZone
        return pick(zh: "\(c.month ?? 1)月\(c.day ?? 1)日 " + ["周日", "周一", "周二", "周三", "周四", "周五", "周六"][(c.weekday ?? 1) - 1], en: d.formatted(en))
    }
    static var today: String { pick(zh: "今天", en: "Today") }
    static var yesterday: String { pick(zh: "昨天", en: "Yesterday") }
}
