import Foundation

extension Words.Footprint {
    // the ▶ replay: its button's labels for VoiceOver, and the caption of a long stay's beat (a stop's is `stopFor`)
    static var replay: String { Words.pick(zh: "回放", en: "Replay") }
    static var replayPause: String { Words.pick(zh: "暂停回放", en: "Pause replay") }
    static func replayStay(place: String, duration: String) -> String { "\(place) · \(duration)" }
}
