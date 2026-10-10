import Foundation

// The talk panel's small decisions that do not need a screen to be checked (spec-2c).

enum PanelRules {
    /// scrolled up further than this, he is reading history on purpose: new rows do not pull him back down
    static let pinSlack = 80.0

    /// Whether the stream follows its bottom. Only his own finger changes the answer: a new row, a growing answer, the keyboard or the lazy stack's estimate
    /// all move the bottom away without him, and the old answer stands.
    static func follows(was: Bool, byHand: Bool, away: Double) -> Bool { byHand ? away <= pinSlack : was }

    /// the chip over the stream: what waits on him and what she is doing; nil when both are none, and then the chip takes no room
    static func chip(waits: Int, works: Int) -> String? {
        let parts = [waits > 0 ? "等你 \(waits) 件" : nil, works > 0 ? "她在做 \(works)" : nil].compactMap { $0 }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }
}
