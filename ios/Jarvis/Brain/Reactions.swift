import Foundation
import Observation

// Message reactions, like iMessage's tapbacks (ADR 0256): one emoji per person per message. They come on the conversation's rows (`reactions`), live on /phone/ws
// (`reaction` frames), and his go up with POST /inherent/reactions. Between one conversation read and the next, the frames are kept in a small overlay.

/// The reactions on one message: his (`allen`) and hers (`jarvis`).
struct RowReactions: Equatable {
    var allen: String?
    var jarvis: String?
    var isEmpty: Bool { allen == nil && jarvis == nil }

    subscript(by: String) -> String? {
        get { by == "allen" ? allen : by == "jarvis" ? jarvis : nil }
        set { if by == "allen" { allen = newValue } else if by == "jarvis" { jarvis = newValue } }
    }

    /// the `reactions` of a conversation row; absent, `{}` and an empty string all mean none
    static func parse(_ row: [String: Any]) -> RowReactions {
        let o = row["reactions"] as? [String: Any] ?? [:]
        func one(_ k: String) -> String? { (o[k] as? String).flatMap { $0.isEmpty ? nil : $0 } }
        return RowReactions(allen: one("allen"), jarvis: one("jarvis"))
    }
}

enum Tapback {
    /// the six iMessage ones, in its order: heart, thumbs up, thumbs down, haha, !!, ?
    static let standard = ["\u{2764}\u{FE0F}", "\u{1F44D}", "\u{1F44E}", "\u{1F602}", "\u{203C}\u{FE0F}", "\u{2753}"]
    /// the program's mark on a message he sent while she was still answering ("seen"); it is not an answer
    static let eyes = "\u{1F440}"

    /// What his reaction becomes when he picks `pick` on a message that has `current` from him: the same one takes it back, another replaces it.
    static func next(current: String?, pick: String) -> String? { current == pick ? nil : pick }

    /// The first thing typed that is an emoji (a whole grapheme: a skin tone or a keycap stays with it); a digit or a letter is not one.
    static func firstEmoji(in text: String) -> String? {
        text.first { c in c.unicodeScalars.contains { $0.properties.isEmojiPresentation || $0.value == 0xFE0F } }.map(String.init)
    }
}

/// The frames that came after the conversation was last read, by message and by person. A mark with no emoji is a reaction taken back.
/// A conversation read drops the marks it started after (`settle`): the rows have them then.
struct ReactionOverlay: Equatable {
    struct Mark: Equatable { var emoji: String?; var stamp: Int }
    private var marks: [String: [String: Mark]] = [:]
    private(set) var stamp = 0
    var isEmpty: Bool { marks.isEmpty }

    mutating func set(_ target: String, by: String, emoji: String?) {
        stamp += 1
        marks[target, default: [:]][by] = Mark(emoji: emoji, stamp: stamp)
    }

    /// A read that started at `stamp` and brought the rows of `targets`: what was marked before it is in those rows. Marks on rows it did not bring stay.
    mutating func settle(upTo stamp: Int, targets: Set<String>) {
        for t in targets {
            guard let m = marks[t] else { continue }
            let kept = m.filter { $0.value.stamp > stamp }
            marks[t] = kept.isEmpty ? nil : kept
        }
    }

    /// `base`, the reactions the row came with, as the frames since then have changed them
    func merged(_ base: RowReactions, target: String) -> RowReactions {
        guard let m = marks[target] else { return base }
        var r = base
        for (by, mark) in m { r[by] = mark.emoji }
        return r
    }

    func emoji(_ target: String, by: String, fallback: String?) -> String? { marks[target]?[by].map(\.emoji) ?? fallback }
}

/// How a typed turn's wait ends, once it has stopped waiting.
enum WaitEnd: Equatable {
    case failed         // the host failed the turn
    case spoke          // she answered in words
    case quiet          // she answered with a reaction alone: nothing more is coming, and nothing is said
    case fallback       // nothing came in time: the line that says her answer will be in the conversation

    static func of(failed: Bool, said: Bool, reacted: Bool) -> WaitEnd { failed ? .failed : said ? .spoke : reacted ? .quiet : .fallback }
}

@MainActor @Observable
final class Reactions {
    static let shared = Reactions()

    private(set) var overlay = ReactionOverlay()
    /// messages of his that she answered with a reaction (not the program's eyes): the wait for a typed answer ends on one, quietly
    @ObservationIgnored private var answers = Set<String>()
    /// his reaction up to the brain; true when it took it (the tests' stand-in replaces this)
    @ObservationIgnored var send: (_ target: String, _ emoji: String?) async -> Bool = { target, emoji in
        ((try? await BrainLink.shared.call { try await BrainClient.react(target: target, emoji: emoji, bases: $0, token: $1) }) ?? 0) == 200
    }

    func apply(_ f: ReactionFrame) {
        overlay.set(f.target, by: f.by, emoji: f.emoji)
        if f.by == "jarvis", let e = f.emoji, e != Tapback.eyes { answers.insert(f.target) }
    }

    /// She reacted to this message of his, as her answer
    func answered(_ target: String?) -> Bool { target.map(answers.contains) ?? false }

    func settle(upTo stamp: Int, targets: Set<String>) {
        guard !targets.isEmpty else { return }
        var o = overlay
        o.settle(upTo: stamp, targets: targets)
        if o != overlay { overlay = o }
    }

    /// His pick on a message: shown at once, and taken back with false when the brain does not take it. `shown` is what he has on it now.
    func pick(_ emoji: String, on target: String, shown: String?) async -> Bool {
        let was = overlay.emoji(target, by: "allen", fallback: shown), next = Tapback.next(current: was, pick: emoji)
        overlay.set(target, by: "allen", emoji: next)
        if await send(target, next) { overlay.set(target, by: "allen", emoji: next); return true }       // marked again: a read that began meanwhile may not have it yet
        overlay.set(target, by: "allen", emoji: was)
        return false
    }
}

extension BrainClient {
    /// `POST /inherent/reactions`: his reaction on the record `target`, or none with a nil `emoji`. 200 the brain took it; 400 a bad emoji or target.
    static func react(target: String, emoji: String?, bases: [URL], token: String) async throws -> Int {
        let body = try JSONSerialization.data(withJSONObject: ["target": target, "emoji": emoji ?? NSNull()] as [String: Any])
        return try await send("POST", "/inherent/reactions", body: body, also: [400], bases: bases, token: token, unauthorized: .revoked).status
    }
}
