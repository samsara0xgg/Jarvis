// words: legacy
import Foundation

/// Where he was, as the dial writes and tints it: a place he keeps has a name and a hue, the phone's own label is only a dimmer name, and a stay with neither is a neutral haze.
enum PlaceTint {
    /// how strongly a stay's wash is painted (the sum of its layers at the ring); the nebula haze behind it is about 0.03
    static let kept = 0.07, plain = 0.032
    /// a stay of at least this many minutes has its name written on the dial
    static let nameMinutes = 45.0

    /// home is warm, school is the blue of work, any other place has a hue of its own that never changes (picked from its id)
    static func colour(role: String?, id: String) -> RGB {
        switch role {
        case "home": return .warm
        case "school": return .work
        default: return others[Int(fnv(id) % UInt32(others.count))]
        }
    }
    private static let others: [RGB] = [.done, .dial, .next, .wait]

    /// The timeline lists the same places in colours of its own (the dial's above stay as they are): home and school are fixed, any other place keeps the hue its id picks,
    /// and a stay in no place he keeps is a neutral haze.
    static func line(role: String?, id: String) -> RGB {
        switch role {
        case "home": return Look.home
        case "school": return Look.school
        default: return lineOthers[Int(fnv(id) % UInt32(lineOthers.count))]
        }
    }
    static let lineNeutral = RGB(196, 204, 238)
    private static let lineOthers = [RGB(240, 132, 218), RGB(132, 152, 255), RGB(172, 124, 255), RGB(111, 224, 180)]
    /// FNV-1a, because `hashValue` is different on every launch
    static func fnv(_ s: String) -> UInt32 { s.utf8.reduce(2166136261) { ($0 ^ UInt32($1)) &* 16777619 } }
}

extension DayItem {
    private func clean(_ s: String?) -> String? { s.map { $0.trimmingCharacters(in: .whitespaces) }.flatMap { $0.isEmpty ? nil : $0 } }

    /// the place as the caption and the star say it: the name he gave it, else what the phone called it
    var placeLabel: String? { clean(kept_place?.name) ?? clean(place) }
    /// the name written on the dial: the name he gave the place, else the first comma-separated part of the phone's label (`kept` false: dimmer)
    var ringName: (name: String, kept: Bool)? {
        if let n = clean(kept_place?.name) { return (n, true) }
        guard let p = clean(place?.split(whereSeparator: { ",，".contains($0) }).first.map(String.init)) else { return nil }
        return (p, false)
    }
}
