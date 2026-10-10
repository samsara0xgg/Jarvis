import Foundation

/// Her words as the brain writes them: every response row carries `<voice>…</voice><document>…</document>`, a lead-in line only `<voice>`,
/// an untagged answer counts as both (ADR 0040). The Mac companion's `visible()` and `spoken()` (desktop/resonance/src/model.ts, Companion.tsx), as the phone reads them.
enum AnswerText {
    /// What to show: a document is the whole answer and the voice only its spoken form, so once one is there show it alone; drop the tags and any half-streamed one.
    static func visible(_ reply: String) -> String {
        let from = reply.range(of: "<document>").map { String(reply[$0.lowerBound...]) } ?? reply
        return from
            .replacingOccurrences(of: "</voice>", with: "\n")
            .replacingOccurrences(of: "</?(voice|document)>", with: "", options: .regularExpression)
            .replacingOccurrences(of: "</?[a-z]*\\z", with: "", options: .regularExpression)
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// What she says aloud: the `<voice>` region when there is one, else the whole text; plain words, without markdown emphasis and code ticks.
    static func spoken(_ reply: String) -> String {
        var region = reply
        if let open = reply.range(of: "<voice>") {
            let rest = reply[open.upperBound...]
            region = String(rest[..<(rest.range(of: "</voice>")?.lowerBound ?? rest.endIndex)])
        }
        return visible(region).replacingOccurrences(of: "\\*\\*|`", with: "", options: .regularExpression)
    }
}
